# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit, cancellable art sessions over an already authenticated transport."""
from __future__ import annotations

import threading
import ctypes
import math
from pathlib import Path

from .native_pointer import MacPointer, ScreenRect, WindowsPointer
from .vendor.art import transforms as t
from .vendor.art.canvas import CanvasMirror

HELPER_PATH = "/home/root/.local/lib/paperdesk/current/bin/pen-bridge"
RUNTIME_ROOT = "/tmp/paperdesk-viewport"


def canvas_rectangle(platform, rect, desktop, metadata, api=None):
    """Map Krita's logical canvas rectangle to the native pointer coordinate space."""
    if platform != "win32": return ScreenRect(*rect)
    # Qt supplies the actual HWND and canvas offset inside its client area.
    # ClientToScreen gives physical native coordinates even on mixed-DPI monitors.
    window = metadata.get("native_window")
    offset = metadata.get("canvas_offset")
    ratio = metadata.get("pixel_ratio")
    if (api is not None and hasattr(api, "ClientToScreen") and isinstance(window, int) and window > 0
            and isinstance(offset, (list, tuple)) and len(offset) == 2
            and all(isinstance(n, (int, float)) and math.isfinite(n) for n in offset)
            and isinstance(ratio, (int, float)) and math.isfinite(ratio) and 0.1 <= ratio <= 16):
        from .windows_ink import POINT
        api.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(POINT)]
        api.ClientToScreen.restype = ctypes.c_int32
        origin = POINT(0, 0)
        if not api.ClientToScreen(window, ctypes.byref(origin)):
            raise RuntimeError("Could not map Krita's native canvas window. Restart the art session after checking the plugin.")
        return ScreenRect(origin.x+offset[0]*ratio, origin.y+offset[1]*ratio, rect[2]*ratio, rect[3]*ratio)
    # Protocol fixtures/older callers can provide a uniform-DPI virtual desktop.
    # The current plugin always provides native window metadata on Windows.
    if api is not None and hasattr(api, "GetSystemMetrics"):
        native = ScreenRect(*(api.GetSystemMetrics(n) for n in (76, 77, 78, 79)))
        sx, sy = native.width/desktop[2], native.height/desktop[3]
        return ScreenRect(native.x+(rect[0]-desktop[0])*sx, native.y+(rect[1]-desktop[1])*sy, rect[2]*sx, rect[3]*sy)
    return ScreenRect(*rect)


def canvas_uv(mapper, u, v, target):
    if target is not None and mapper.region:
        x, y, width, height = mapper.region
        return (u-x)/width, (v-y)/height
    return u, v


class StreamProcess:
    """Small stream wrapper for the reused canvas protocol; never spawns a shell locally."""
    def __init__(self, remote):
        self.remote = remote
        self.stdin = self.stdout = self
        self.closed = False
        self.io_lock = threading.RLock()

    def read(self, size=-1):
        return self.remote.read(size)

    def readline(self):
        return self.remote.readline()

    def write(self, data):
        if self.closed:
            raise OSError("Art transport stream closed.")
        with self.io_lock:
            return self.remote.write(data)

    def flush(self):
        return self.remote.flush()

    def poll(self):
        if self.closed:
            return 0
        channel = getattr(self.remote, "channel", None)
        if channel is not None and channel.exit_status_ready():
            return channel.recv_exit_status()
        return None

    def close(self):
        if not self.closed:
            self.closed = True
            self.remote.close()


class LinuxOutput:
    backend = "linux-uinput"
    """Linux kernel tablet/touch devices, created only for an explicit art session."""
    def __init__(self, mapper, touch_mapper, touch_enabled):
        from evdev import AbsInfo, UInput
        self.closed = False
        self.lock = threading.RLock()
        pen_caps = {t.EV_KEY: list(t.KEYS), t.EV_ABS: [
            (t.ABS_X, AbsInfo(0, 0, mapper.span[0], 0, 0, round(mapper.res[0]))),
            (t.ABS_Y, AbsInfo(0, 0, mapper.span[1], 0, 0, round(mapper.res[1]))),
            (t.ABS_PRESSURE, AbsInfo(0, 0, t.PRESSURE_MAX, 0, 0, 0)),
            (t.ABS_DISTANCE, AbsInfo(0, 0, t.DISTANCE_MAX, 0, 0, 0)),
            (t.ABS_TILT_X, AbsInfo(0, -90, 90, 0, 0, 57)),
            (t.ABS_TILT_Y, AbsInfo(0, -90, 90, 0, 0, 57)),
        ]}
        self.pen = UInput(pen_caps, name="PaperDesk Paper Pro Marker", vendor=0x2edd, product=1, version=1, bustype=6, input_props=[0])
        self.touch = None
        try:
            if touch_enabled:
                caps = {t.EV_KEY: [t.BTN_TOUCH], t.EV_ABS: [
                    (t.ABS_MT_SLOT, AbsInfo(0, 0, t.SLOTS - 1, 0, 0, 0)),
                    (t.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
                    (t.ABS_MT_POSITION_X, AbsInfo(0, 0, touch_mapper.span[0], 0, 0, max(1, round(touch_mapper.res[0])))),
                    (t.ABS_MT_POSITION_Y, AbsInfo(0, 0, touch_mapper.span[1], 0, 0, max(1, round(touch_mapper.res[1])))),
                ]}
                self.touch = UInput(caps, name="PaperDesk Paper Pro Touch", vendor=0x2edd, product=2, version=1, bustype=6, input_props=[1])
        except BaseException:
            self.pen.close()
            raise

    def emit(self, events, *, touch=False):
        with self.lock:
            device = self.touch if touch else self.pen
            if not self.closed and device and events:
                for event in events:
                    device.write(*event)
                device.syn()

    def release(self):
        self.emit([(t.EV_KEY, code, 0) for code in t.KEYS] + [(t.EV_ABS, t.ABS_PRESSURE, 0)])

    def close(self):
        with self.lock:
            if not self.closed:
                try:
                    self.release()
                finally:
                    self.closed = True
                    for device in (self.pen, self.touch):
                        if device:
                            device.close()


class MouseOutput:
    backend = "native-mouse"
    """Translate transformed pen frames into honest pointer-only native input."""
    def __init__(self, platform, mapper, screen=None):
        self.lock = threading.RLock()
        self.pointer = MacPointer(screen) if platform == "darwin" else WindowsPointer(screen)
        self.mapper = mapper
        self.canvas_target = None
        self.x = self.y = 0
        self.down = False
        self.closed = False

    def emit(self, events, *, touch=False):
        if touch or not events:
            return
        with self.lock:
            if self.closed:
                return
            dirty = False
            for kind, code, value in events:
                if kind == t.EV_ABS and code == t.ABS_X:
                    self.x, dirty = value, True
                elif kind == t.EV_ABS and code == t.ABS_Y:
                    self.y, dirty = value, True
                elif kind == t.EV_KEY and code == t.BTN_TOUCH:
                    self.down, dirty = bool(value), True
                elif kind == t.EV_KEY and code in (t.BTN_TOOL_PEN, t.BTN_TOOL_RUBBER) and not value:
                    self.down, dirty = False, True
            if dirty:
                u, v = canvas_uv(self.mapper, self.x/self.mapper.span[0], self.y/self.mapper.span[1], self.canvas_target)
                self.pointer.send(u, v, self.down)

    def release(self):
        with self.lock:
            if not self.closed:
                self.down = False
                self.pointer.release()

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.pointer.close()


class WindowsInkOutput:
    """Replay transformed pen and palm-filtered touch frames through Windows Ink."""
    backend = "windows-ink"
    def __init__(self, mapper, touch_mapper, touch_enabled, screen=None, *, pen_factory=None):
        from .windows_ink import WindowsInkPointer, WindowsInkTouch
        self.mapper, self.touch_mapper = mapper, touch_mapper
        self.lock = threading.RLock()
        self.pen = (pen_factory or WindowsInkPointer)(screen)
        self.touch = None
        self.closed = False
        self.canvas_target = None
        self.x = self.y = self.pressure = self.tx = self.ty = 0
        self.down = self.pen_near = self.rubber_near = self.barrel = False
        self.slot, self.contacts = 0, {}
        try:
            if touch_enabled: self.touch = WindowsInkTouch(screen)
        except BaseException:
            self.pen.close(); raise

    def emit(self, events, *, touch=False):
        with self.lock:
            if self.closed or not events: return
            if touch:
                if not self.touch: return
                for kind, code, value in events:
                    if kind != t.EV_ABS: continue
                    if code == t.ABS_MT_SLOT: self.slot = value
                    elif code == t.ABS_MT_TRACKING_ID:
                        if value < 0: self.contacts.pop(self.slot, None)
                        else: self.contacts.setdefault(self.slot, [0, 0])
                    elif code == t.ABS_MT_POSITION_X and self.slot in self.contacts: self.contacts[self.slot][0] = value
                    elif code == t.ABS_MT_POSITION_Y and self.slot in self.contacts: self.contacts[self.slot][1] = value
                self.touch.send({slot: (x/self.touch_mapper.span[0], y/self.touch_mapper.span[1]) for slot, (x,y) in self.contacts.items()})
                return
            dirty = False
            axes = {t.ABS_X: 'x', t.ABS_Y: 'y', t.ABS_PRESSURE: 'pressure', t.ABS_TILT_X: 'tx', t.ABS_TILT_Y: 'ty'}
            keys = {t.BTN_TOUCH: 'down', t.BTN_TOOL_PEN: 'pen_near', t.BTN_TOOL_RUBBER: 'rubber_near', t.BTN_STYLUS: 'barrel'}
            for kind, code, value in events:
                if kind == t.EV_ABS and code in axes: setattr(self, axes[code], value); dirty = True
                elif kind == t.EV_KEY and code in keys: setattr(self, keys[code], bool(value)); dirty = True
            if dirty:
                u, v = canvas_uv(self.mapper, self.x/self.mapper.span[0], self.y/self.mapper.span[1], self.canvas_target)
                self.pen.send(u, v, self.down,
                              pressure=self.pressure, tilt_x=self.tx, tilt_y=self.ty,
                              eraser=self.rubber_near, barrel=self.barrel,
                              near=self.pen_near or self.rubber_near or self.down)

    def release(self):
        with self.lock:
            if not self.closed: self.pen.release()

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                try:
                    if self.touch: self.touch.close()
                finally: self.pen.close()


class ArtSession:
    """No auto-deploy, reconnect loop or idle threads outside an active session."""
    def __init__(self, transport, platform, options, socket_path: Path, notify=None):
        self.transport, self.platform, self.options = transport, platform, options
        self.socket_path = socket_path
        self.notify = notify or (lambda message: None)
        self.stop_event = threading.Event()
        self.stream_lock = threading.Lock()
        self.cleanup_lock = threading.RLock()
        self.streams = []
        self.threads = []
        self.output = None
        self.mirror = None
        self.error = ""
        self.started = False
        self.cleaned = False
        rotate = options["rotation"]
        self.auto = rotate == "auto"
        rotate = "left" if self.auto else rotate
        self.mapper = t.Mapper(rotate, options["aspect"])
        self.frames = t.Frames(self.mapper)
        self.frames.pressure = t.pressure_curve(options["pressure"], options["curve"], options["tilt"])
        self.gestures = t.Gestures(t.Mapper(rotate, options["aspect"], t.TOUCH_X_MAX, t.TOUCH_Y_MAX))
        self.gestures.palm_size = options.get("palm_size")
        self.pinch = t.Pinch(self.gestures.mapper)
        self.taps = t.Taps(self.gestures.mapper)
        self.near = set()
        self.pen_down = False
        self.picking = False
        self.picked_at = 0.0
        self.counts = {"pen": 0, "touch": 0}

    def _stream(self, script):
        with self.stream_lock:
            if self.stop_event.is_set():
                raise OSError("Art session has stopped.")
            process = StreamProcess(self.transport.stream(script))
            self.streams.append(process)
            return process

    def start(self):
        try:
            if self.platform.startswith("linux"):
                self.output = LinuxOutput(self.mapper, self.gestures.mapper, self.options["touch"] and not self.options["display"])
            else:
                screen = ScreenRect(*self.options["screen_rect"]) if self.options.get("screen_rect") else None
                from .windows_ink import windows_ink_available
                ink = self.platform == "win32" and self.options["mode"] != "mouse" and windows_ink_available()
                if ink:
                    self.output = WindowsInkOutput(self.mapper, self.gestures.mapper, self.options["touch"] and not self.options["display"], screen)
                elif self.platform == "darwin" and self.options["mode"] == "pen":
                    from .quartz_pen import MacTabletPointer
                    self.output = WindowsInkOutput(self.mapper, self.gestures.mapper, False, screen, pen_factory=MacTabletPointer)
                    self.output.backend = "quartz-pen-experimental"
                elif self.options["mode"] == "pen":
                    raise RuntimeError("Native pen mode is unavailable on this system. Choose mouse mode for pointer-only input.")
                else:
                    if self.options["touch"] and not self.options["display"]: raise RuntimeError("Native touch needs Windows Ink. Turn touch off for mouse mode, or enable Krita canvas display gestures.")
                    self.output = MouseOutput(self.platform, self.mapper, screen)
            if self.options["display"]:
                self.mirror = CanvasMirror(self._stream, self.socket_path, self.stop_event, self.mapper.rotate,
                                           (self.mapper, self.gestures.mapper), lambda: self.pen_down,
                                           auto=self.auto, refresh=self.options["refresh"], notify=self.notify,
                                           on_geometry=self._canvas_geometry, on_error=self._mirror_error)
                self.mirror.jpeg = self.options["jpeg"]
                self.mirror.eraser = self.options.get("eraser")
                size = [4320, 3240] if self.mapper.rotate in ("left", "right") else [3240, 4320]
                self.mirror.start = {"mode": "new" if self.options["new_canvas"] else "auto", "size": size}
                if self.options.get("open_path"):
                    self.mirror.start = {"mode": "open", "path": self.options["open_path"]}
            # No stale-process killing: another program's device grab is never stolen.
            self._sensor("pen", "/dev/input/event2", self._pen, self._pen_drop)
            if self.options["touch"]:
                self._sensor("touch", "/dev/input/event3", self._touch, self._touch_drop)
            self.started = True
        except BaseException:
            self.stop()
            raise

    def _canvas_geometry(self, rect, desktop, metadata):
        with self.output.lock:
            self.mapper.region = ((rect[0]-desktop[0])/desktop[2], (rect[1]-desktop[1])/desktop[3], rect[2]/desktop[2], rect[3]/desktop[3])
            if not self.platform.startswith("linux"):
                pointer = getattr(self.output, "pointer", None) or getattr(self.output, "pen", None)
                if pointer:
                    target = canvas_rectangle(self.platform, rect, desktop, metadata, pointer.api)
                    pointer.screen = self.output.canvas_target = target

    def _mirror_error(self, message):
        self.error = message
        self.stop()

    def _sensor(self, label, node, handle, release):
        grab = "-" if self.options["echo"] else "g"
        tee = f" {RUNTIME_ROOT}/pen" if label == "pen" and self.mirror else ""
        process = self._stream(f"exec {HELPER_PATH} {grab} {node}{tee}")
        worker = threading.Thread(target=self._read_sensor, args=(label, process, handle, release), name=f"PaperDesk-{label}", daemon=True)
        self.threads.append(worker)
        worker.start()

    def _read_sensor(self, label, process, handle, release):
        pending = b""
        try:
            while not self.stop_event.is_set():
                chunk = process.read(65536)
                if not chunk:
                    if not self.stop_event.is_set():
                        self.error = f"The {label} stream ended. Stop and start art mode after checking the tablet connection."
                    break
                pending += chunk
                whole = len(pending) - len(pending) % t.RECORD.size
                for _, _, kind, code, value in t.RECORD.iter_unpack(pending[:whole]):
                    if self.stop_event.is_set():
                        break
                    handle(kind, code, value)
                pending = pending[whole:]
        except Exception as error:
            if not self.stop_event.is_set():
                self.error = f"Art input stopped: {type(error).__name__}. Check tablet and native input permissions."
        finally:
            process.close()
            try:
                release()
            except (OSError, RuntimeError):
                pass
            if not self.stop_event.is_set():
                self.notify(self.error)
                self.stop()

    def _pen(self, kind, code, value):
        import time
        if kind == t.EV_KEY and code in (t.BTN_TOOL_PEN, t.BTN_TOOL_RUBBER):
            (self.near.add if value else self.near.discard)(code)
            self.gestures.pen_near = bool(self.near)
            if code == t.BTN_TOOL_RUBBER and self.mirror:
                self.mirror.erase = bool(value)
                self.mirror.wake.set()
                if not self.options["eraser_device"]:
                    code = t.BTN_TOOL_PEN
        if kind == t.EV_KEY and code == t.BTN_TOUCH:
            if value and self.mirror and self.mapper.region and self.mirror.on_palette(*self._spot()):
                self.picking, self.picked_at = True, 0.0
            elif not value and self.picking:
                self.picking = False
                self.mirror.tap(*self._spot(), False, True)
                self.frames.feed(kind, code, value)
                self.frames.keys.pop(t.BTN_TOUCH, None)
                return
            else:
                self.pen_down = bool(value)
        events = self.frames.feed(kind, code, value)
        if events is None:
            return
        if self.picking:
            now = time.monotonic()
            if now - self.picked_at >= 0.08:
                self.mirror.tap(*self._spot(), False, self.picked_at > 0)
                self.picked_at = now
            return
        self.counts["pen"] += 1
        self.output.emit(events)

    def _spot(self):
        x, y = self.mapper.turn(self.frames.x, self.frames.y, self.mapper.x_max, self.mapper.y_max)
        return x / self.mapper.units[0], y / self.mapper.units[1]

    def _touch(self, kind, code, value):
        import time
        if self.mirror and self.mapper.region and kind == t.EV_SYN and code == 0:
            self.gestures.judge()
            self.output.emit(self.gestures.cancel(), touch=True)
            step = self.pinch.step(self.gestures.settled, self.gestures.pen_near)
            tap = self.taps.step(self.gestures.fingers, self.gestures.pen_near, time.monotonic())
            if tap:
                self.mirror.tap(*tap)
            if step:
                self.counts["touch"] += 1
                self.mirror.view(step)
            return
        events = self.gestures.feed(kind, code, value)
        if events:
            self.counts["touch"] += 1
            self.output.emit(events, touch=True)

    def _pen_drop(self):
        self.near.clear()
        self.gestures.pen_near = False
        self.pen_down = self.picking = False
        if self.output:
            self.output.release()
        if self.mirror:
            self.mirror.erase = False
            self.mirror.wake.set()

    def _touch_drop(self):
        self.gestures.clear()
        if self.output:
            self.output.emit(self.gestures.cancel(), touch=True)

    def stop(self):
        # Mark/cancel first, then join outside the lock. Two failing streams may finish together.
        with self.cleanup_lock:
            if self.cleaned:
                return
            self.cleaned = True
            self.stop_event.set()
            with self.stream_lock:
                processes = list(self.streams)
            if self.mirror:
                self.mirror.close()
            for process in processes:
                process.close()
        for worker in self.threads:
            if worker is not threading.current_thread():
                worker.join(3)
        try:
            self._touch_drop()
        finally:
            if self.output:
                self.output.close()

    def status(self):
        return {"active": self.started and not self.stop_event.is_set(), "error": self.error,
                "frames": dict(self.counts), "backend": getattr(self.output, "backend", ""),
                "message": self.mirror.state if self.mirror else "Native pen input"}
