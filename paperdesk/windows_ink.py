# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native Windows Ink pen/touch injection (Windows 10 1809 or newer).

Original adapter written from Microsoft's public Win32 API definitions, not from
third-party tablet projects. No installed kernel driver or elevated process.
"""
from __future__ import annotations

import ctypes
import sys
import time

from .native_pointer import PointerPermissionError, ScreenRect

UINT = ctypes.c_uint32
LONG = ctypes.c_int32
HANDLE = ctypes.c_void_p


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class POINTER_INFO(ctypes.Structure):
    _fields_ = [("pointerType", UINT), ("pointerId", UINT), ("frameId", UINT), ("pointerFlags", UINT),
                ("sourceDevice", HANDLE), ("hwndTarget", HANDLE),
                ("ptPixelLocation", POINT), ("ptHimetricLocation", POINT),
                ("ptPixelLocationRaw", POINT), ("ptHimetricLocationRaw", POINT),
                ("dwTime", UINT), ("historyCount", UINT), ("InputData", LONG), ("dwKeyStates", UINT),
                ("PerformanceCount", ctypes.c_uint64), ("ButtonChangeType", UINT)]


class POINTER_PEN_INFO(ctypes.Structure):
    _fields_ = [("pointerInfo", POINTER_INFO), ("penFlags", UINT), ("penMask", UINT),
                ("pressure", UINT), ("rotation", UINT), ("tiltX", LONG), ("tiltY", LONG)]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [("pointerInfo", POINTER_INFO), ("touchFlags", UINT), ("touchMask", UINT),
                ("rcContact", RECT), ("rcContactRaw", RECT), ("orientation", UINT), ("pressure", UINT)]


class _POINTER_UNION(ctypes.Union):
    _fields_ = [("pointerInfo", POINTER_INFO), ("touchInfo", POINTER_TOUCH_INFO), ("penInfo", POINTER_PEN_INFO)]


class POINTER_TYPE_INFO(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", UINT), ("data", _POINTER_UNION)]


PT_TOUCH, PT_PEN = 2, 3
NEW, INRANGE, INCONTACT, FIRSTBUTTON, SECONDBUTTON = 1, 2, 4, 0x10, 0x20
PRIMARY, CONFIDENCE = 0x2000, 0x4000
DOWN, UPDATE, UP = 0x10000, 0x20000, 0x40000
PEN_BARREL, PEN_INVERTED, PEN_ERASER = 1, 2, 4
PEN_PRESSURE, PEN_TILT_X, PEN_TILT_Y = 1, 4, 8


class WindowsInkUnavailable(RuntimeError):
    """Windows Ink cannot be created; the user may explicitly choose mouse mode."""


def _load_api():
    if sys.platform != "win32":
        raise WindowsInkUnavailable("Windows Ink is available on Windows only.")
    return ctypes.WinDLL("user32", use_last_error=True)


def windows_ink_available(api=None) -> bool:
    """Checks exports only; does not create a device or inject input while idle."""
    try:
        api = api if api is not None else _load_api()
        return all(hasattr(api, name) for name in ("CreateSyntheticPointerDevice", "InjectSyntheticPointerInput", "DestroySyntheticPointerDevice"))
    except (OSError, WindowsInkUnavailable):
        return False


def _configure(api):
    if not windows_ink_available(api):
        raise WindowsInkUnavailable("Windows Ink requires Windows 10 version 1809 or newer. Choose mouse mode on an older system.")
    api.CreateSyntheticPointerDevice.argtypes = [UINT, UINT, UINT]
    api.CreateSyntheticPointerDevice.restype = HANDLE
    api.InjectSyntheticPointerInput.argtypes = [HANDLE, ctypes.POINTER(POINTER_TYPE_INFO), UINT]
    api.InjectSyntheticPointerInput.restype = ctypes.c_int32
    api.DestroySyntheticPointerDevice.argtypes = [HANDLE]
    api.DestroySyntheticPointerDevice.restype = None
    api.GetSystemMetrics.argtypes = [ctypes.c_int]
    api.GetSystemMetrics.restype = ctypes.c_int


def _virtual_screen(api):
    return ScreenRect(*(api.GetSystemMetrics(n) for n in (76, 77, 78, 79)))


def _last_error():
    return getattr(ctypes, "get_last_error", lambda: 0)()


class SyntheticDevice:
    def __init__(self, kind, count, screen=None, api=None):
        self.api = api if api is not None else _load_api()
        _configure(self.api)
        self.screen = screen or _virtual_screen(self.api)
        self.screen.point(0, 0)
        # POINTER_FEEDBACK_NONE=3 suppresses contact visualization overlays.
        self.handle = self.api.CreateSyntheticPointerDevice(kind, count, 3)
        if not self.handle:
            raise WindowsInkUnavailable("Windows could not create the native pen/touch device. Check the interactive desktop and application privileges, or explicitly choose mouse mode.")
        self.closed = False

    def inject(self, events):
        if self.closed:
            raise RuntimeError("The Windows Ink device is closed.")
        array = (POINTER_TYPE_INFO * len(events))(*events)
        if self.api.InjectSyntheticPointerInput(self.handle, array, len(events)):
            return
        error = _last_error()
        # MS documents ERROR_NOT_READY as a frame that was *not* injected because
        # calls were under 0.1ms apart. Only this known no-effect error may retry.
        if error == 21:
            time.sleep(0.0005)
            if self.api.InjectSyntheticPointerInput(self.handle, array, len(events)):
                return
        raise PointerPermissionError(f"Windows rejected native pen/touch input (error {error}). Run PaperDesk and the art application at the same privilege level. Check Windows Ink input settings; the stream was stopped.")

    def destroy(self):
        if not self.closed:
            self.closed = True
            handle, self.handle = self.handle, None
            self.api.DestroySyntheticPointerDevice(handle)


class WindowsInkPointer(SyntheticDevice):
    """Windows Ink pressure/tilt/hover/eraser, with guaranteed device destruction."""
    backend = "windows-ink"

    def __init__(self, screen=None, *, api=None):
        super().__init__(PT_PEN, 1, screen, api)
        self.down = self.near = self.pending_down = False
        self.pointer_id = 1
        self.last = (0.5, 0.5)
        self.last_eraser = False

    def _event(self, u, v, flags, pressure=0, tilt_x=0, tilt_y=0, eraser=False, barrel=False):
        x, y = self.screen.point(u, v)
        info = POINTER_INFO(pointerType=PT_PEN, pointerId=self.pointer_id, pointerFlags=flags,
                            ptPixelLocation=POINT(round(x), round(y)), ptPixelLocationRaw=POINT(round(x), round(y)))
        pen = POINTER_PEN_INFO(pointerInfo=info,
                               penFlags=(PEN_INVERTED | PEN_ERASER if eraser else 0) | (PEN_BARREL if barrel else 0),
                               penMask=PEN_PRESSURE | PEN_TILT_X | PEN_TILT_Y,
                               pressure=min(1024, max(0, round(pressure * 1024 / 4096))),
                               tiltX=min(90, max(-90, round(tilt_x))), tiltY=min(90, max(-90, round(tilt_y))))
        return POINTER_TYPE_INFO(type=PT_PEN, penInfo=pen)

    def send(self, u, v, down, *, pressure=0, tilt_x=0, tilt_y=0, eraser=False, barrel=False, near=True):
        if self.closed:
            raise RuntimeError("The Windows Ink device is closed.")
        near, down = bool(near), bool(down) and bool(near)
        flags = PRIMARY | (INRANGE if near else 0)
        if down:
            flags |= INCONTACT | (SECONDBUTTON if barrel else FIRSTBUTTON)
        if down and not self.down:
            flags |= DOWN
        elif self.down and not down:
            flags |= UP
        else:
            flags |= UPDATE
        if near and not self.near:
            flags |= NEW
        self.last, self.last_eraser = (u, v), bool(eraser)
        self.pending_down = down
        event = self._event(u, v, flags, pressure if down else 0, tilt_x, tilt_y, eraser, barrel)
        self.inject([event])
        self.down, self.near, self.pending_down = down, near, False

    def release(self):
        if not self.closed and (self.near or self.down or self.pending_down):
            # UP without INRANGE ends both contact and proximity. Destroy still
            # runs if injection fails, so no synthetic device survives teardown.
            flags = PRIMARY | (UP if self.down or self.pending_down else UPDATE)
            event = self._event(*self.last, flags, eraser=self.last_eraser)
            self.inject([event])
            self.down = self.near = self.pending_down = False

    def close(self):
        if not self.closed:
            try:
                self.release()
            finally:
                self.destroy()


class WindowsInkTouch(SyntheticDevice):
    """Palm-filtered multi-touch frames. Host art apps perform pinch/pan/rotate."""
    def __init__(self, screen=None, *, api=None):
        super().__init__(PT_TOUCH, 10, screen, api)
        self.contacts = {}

    def send(self, contacts):
        if not isinstance(contacts, dict) or len(contacts) > 10:
            raise ValueError("A Windows touch frame must contain at most ten contacts.")
        events = []
        current = {}
        for slot, (u, v) in sorted(contacts.items()):
            if not isinstance(slot, int) or not 0 <= slot < 10:
                raise ValueError("Touch slot must be between zero and nine.")
            x, y = self.screen.point(u, v)
            current[slot] = (x, y)
            flags = INRANGE | INCONTACT | CONFIDENCE | (UPDATE if slot in self.contacts else DOWN | NEW)
            events.append(self._event(slot, x, y, flags))
        for slot, (x, y) in sorted(self.contacts.items()):
            if slot not in current:
                events.append(self._event(slot, x, y, UP))
        if events:
            self.inject(events)
        self.contacts = current

    def _event(self, slot, x, y, flags):
        info = POINTER_INFO(pointerType=PT_TOUCH, pointerId=slot + 1, pointerFlags=flags,
                            ptPixelLocation=POINT(round(x), round(y)), ptPixelLocationRaw=POINT(round(x), round(y)))
        touch = POINTER_TOUCH_INFO(pointerInfo=info)
        return POINTER_TYPE_INFO(type=PT_TOUCH, touchInfo=touch)

    def release(self):
        if not self.closed and self.contacts:
            self.inject([self._event(slot, x, y, UP) for slot, (x, y) in sorted(self.contacts.items())])
            self.contacts = {}

    def close(self):
        if not self.closed:
            try:
                self.release()
            finally:
                self.destroy()
