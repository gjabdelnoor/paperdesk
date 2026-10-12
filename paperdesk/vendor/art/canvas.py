# SPDX-License-Identifier: AGPL-3.0-or-later
# Adapted from remarkable-linux-desktop by Gabriel Abdelnoor.
# Modified 2026-10-12 for authenticated transport streams, app-owned tmp state,
# explicit prior provisioning, bounded replies and cancellation. No deployment here.
"""Krita canvas display protocol, rotation, palette, eraser and gesture handling."""
import json
import math
import threading
import time
from .transforms import PANEL, orientation
from ...art_ipc import KritaIPC


def valid_frame_header(head):
    try:
        rect, screen = head['rect'], head['screen']
        return (len(rect) == 4 and len(screen) == 2
                and all(isinstance(n, (int, float)) and math.isfinite(n) for n in [*rect, *screen])
                and all(n > 0 for n in [*rect[2:], *screen])
                and isinstance(head.get('hash'), str))
    except (KeyError, TypeError): return False


class CanvasMirror:
    """Art mode: shows the Krita canvas on the tablet and pins the pen to the same rectangle.

    It exists only while this process keeps it alive: frames and a one-second heartbeat
    go down one SSH stream into the tablet's /tmp. When that stream ends for any reason the
    tablet's own screen comes back within five seconds.
    """
    def __init__(self, start_stream, socket_path, stop_event, rotate, mappers, pen_down, auto=False, refresh=30, notify=None, on_geometry=None, on_error=None):
        self.start_stream, self.socket_path, self.stop_event = start_stream, socket_path, stop_event
        self.mappers, self.mapper, self.pen_down = mappers, mappers[0], pen_down
        self.notify = notify or (lambda message: None)
        self.on_geometry = on_geometry
        self.on_error = on_error or (lambda message: None)
        self.lock = threading.Lock(); self.queue = threading.Lock(); self.zones = []; self.zoned = None; self.krita = None; self.feed = None; self.have = None; self.sent = 0; self.state = ''
        self.pending = None; self.wake = threading.Event(); self.refresh = refresh; self.dirty = False; self.active = 0.0; self.erase = self.erased = False; self.eraser = None; self.jpeg = False
        self.taps = []; self.start = None; self.flashed = 0.0; self.framed = 0.0; self.rubbed = self.urgent = False
        self.face(rotate)
        self.sense_thread = threading.Thread(target=self.sense, daemon=True) if auto else None
        if self.sense_thread: self.sense_thread.start()
        self.thread = threading.Thread(target=self.run, daemon=True); self.thread.start()
    def face(self, rotate):
        long_side, short_side = PANEL; self.rotate = rotate
        for mapper in self.mappers: mapper.face(rotate)
        self.size = (long_side, short_side) if rotate in ('left', 'right') else (short_side, long_side)
        self.turn = {'left': 90, 'right': 270, 'portrait': 0, 'inverted': 180}[rotate]; self.have = None
    def sense(self):
        """Auto-rotate: follow the tablet's accelerometer; two agreeing samples turn the picture."""
        script = ('for d in /sys/bus/iio/devices/iio:device*; do [ "$(cat $d/name)" = lis2dw12_accel ] && cd $d; done; cat in_accel_mount_matrix || exit 1; '
                  'while :; do echo $(cat in_accel_x_raw in_accel_y_raw in_accel_z_raw); sleep 0.4; done')
        while not self.stop_event.is_set():
            proc = None
            try:
                proc = self.start_stream(script)
                matrix = [[float(v) for v in row.split(',')] for row in proc.stdout.readline().decode('ascii').split(';')]; seen = None
                for line in iter(proc.stdout.readline, b''):
                    want = orientation([float(v) for v in line.split()], matrix, self.rotate)
                    if want != self.rotate and want == seen: self.face(want); self.wake.set(); print(f'art: tablet turned · {want}', flush=True)
                    seen = want
            except (OSError, ValueError, IndexError): pass
            finally:
                if proc: proc.close()
            self.stop_event.wait(3)
    def ask(self, request):
        with self.lock:
            if self.stop_event.is_set(): return {'idle': 'Art mode stopped'}, b''
            try:
                if not self.krita:
                    self.krita = KritaIPC(self.socket_path, self.stop_event)
                return self.krita.request(request)
            except (OSError, ValueError):
                if self.krita: self.krita.close()
                self.krita = None
                return {'idle': 'Krita is not running with the compatible reMarkable Viewport plugin'}, b''
    def say(self, state):
        if state != self.state: self.state = state; self.notify(state)
    def tablet(self, name, data=b''):
        """One command down the feeder stream; returns False when the tablet is not taking frames."""
        try:
            if not self.feed or self.feed.poll() is not None:
                script = ('test ! -L /tmp/paperdesk-viewport && mkdir -p -m 700 /tmp/paperdesk-viewport && cd /tmp/paperdesk-viewport || exit 1; rm -f f*.png f*.tmp zones; '
                          # A timed wake lock, renewed with every heartbeat: the tablet stays awake while art mode is
                          # alive and may sleep again six seconds after the last heartbeat, whatever happened here.
                          'awake() { echo "paperdesk-art 6000000000" > /sys/power/wake_lock; touch active; }; '
                          'while read name size; do case $name in hb) awake;; rf) touch refresh.request; awake;; bye) break;; '
                          'zn) dd bs=$size count=1 iflag=fullblock of=zones.tmp 2>/dev/null && mv zones.tmp zones;; f*) dd bs=$size count=1 iflag=fullblock of=$name.tmp 2>/dev/null && mv $name.tmp $name && echo $name > current.tmp && mv current.tmp current && awake;; esac; done; rm -f active')
                self.feed = self.start_stream(script); self.have = None; self.zoned = None
            self.feed.stdin.write(f'{name} {len(data)}\n'.encode()+data); self.feed.stdin.flush(); return True
        except (OSError, RuntimeError):
            self.feed = None; return False
    def tap(self, u, v, double, drag=False):
        """Queue a tap for the palette; a pen dragging over the colour wheel keeps only its latest point."""
        with self.queue:
            if drag and self.taps and self.taps[-1][3]: self.taps.pop()
            self.taps.append((u, v, double, drag))
        self.wake.set()
    def on_palette(self, u, v):
        w, h = self.size
        return any(math.hypot(u*w-x, v*h-y) <= r for x, y, r in self.zones)
    def zone_file(self):
        """The palette circles in the tablet's own (portrait) pixels, for its local ink to skip."""
        short_side, long_side = PANEL[1], PANEL[0]
        spot = {'left': lambda x, y: (short_side-y, x), 'right': lambda x, y: (y, long_side-x),
                'inverted': lambda x, y: (short_side-x, long_side-y), 'portrait': lambda x, y: (x, y)}[self.rotate]
        return ''.join('%d %d %d\n' % (*spot(x, y), r) for x, y, r in self.zones).encode()
    def view(self, step):
        """Queue a pinch step; steps arriving faster than Krita answers are merged."""
        with self.queue:
            if self.pending: step = {'scale': self.pending['scale']*step['scale'], 'at': self.pending['at'], 'move': (self.pending['move'][0]+step['move'][0], self.pending['move'][1]+step['move'][1])}
            self.pending = step
        self.wake.set()
    def run(self):
        try:
            self._run()
        except Exception as error:
            self.say(f'Canvas display stopped: {type(error).__name__}. Check the viewport plugin.')
            self.on_error(self.state)
        finally:
            # The Qt local socket belongs to this thread. A stop request only
            # sets a cancellation event; destruction happens here.
            if self.krita:
                self.krita.close(); self.krita = None

    def _run(self):
        beat = 0; due = 0
        while not self.stop_event.is_set():
            self.wake.wait(0.1); self.wake.clear()
            with self.queue: step, self.pending = self.pending, None; taps, self.taps = self.taps, []
            if self.start:   # new canvas / reopen the last piece / full screen, once Krita answers
                head, _ = self.ask({'op': 'start', **self.start})
                if 'started' in head or 'error' in head: print(f"art: Krita · {head.get('started') or head.get('error')}", flush=True); self.start = None; due = 0
                elif 'asking' in head.get('idle', '') and getattr(self, 'told', None) != head['idle']: self.told = head['idle']; print(f"art: waiting · {head['idle']}", flush=True)
            for u, v, double, drag in taps:   # a finger tap works the on-tablet palette; two quick taps elsewhere undo
                head, _ = self.ask({'op': 'tap', 'at': [u, v], 'drag': drag})
                if double and not head.get('hit'): self.ask({'op': 'undo'})
                due = 0
            if self.erase: self.rubbed = True
            if self.erase != self.erased: self.erased = self.erase; self.ask({'op': 'erase', 'on': self.erased, 'size': self.eraser})
            if step: self.ask({'op': 'view', 'aspect': self.size[0]/self.size[1], **step}); due = min(due, time.monotonic()+0.25)
            now = time.monotonic()
            if self.pen_down(): self.active = now
            if self.have and now-beat >= 1: beat = now; self.tablet('hb')
            # A stroke in progress is inked by the tablet itself; Krita is asked again once the pen lifts.
            if self.pen_down(): due = now+0.25; continue
            # E-ink ghosting: the panel blinks at most every `refresh` seconds, and only once the pen
            # has rested two seconds, never mid-stroke. Erasing leaves the worst ghosts, so erased
            # ink gets a blink at the next two-second rest without waiting for the interval.
            if self.refresh and self.dirty and self.have and now-self.active >= 2 and now-self.framed >= 0.7 and (now-self.flashed >= self.refresh or self.urgent):
                self.dirty = self.urgent = False; self.flashed = now; self.tablet('rf')
            if now < due: continue
            due = now+0.3
            head, body = self.ask({'op': 'frame', 'w': self.size[0], 'h': self.size[1], 'turn': self.turn, 'have': self.have, 'format': 'jpg' if self.jpeg else 'png'})
            if not valid_frame_header(head):
                self.mapper.region = None; self.have = None; self.say('waiting · '+str(head.get('idle') or head.get('error'))); due = now+1; continue
            raw_zones = head.get('zones') or []
            if not isinstance(raw_zones, list): raw_zones = []
            self.zones = [tuple(z) for z in raw_zones[:1000] if isinstance(z, (list, tuple)) and len(z) == 3 and all(isinstance(n, (int, float)) and math.isfinite(n) for n in z)]
            if self.have and (self.zones, self.rotate) != self.zoned and self.tablet('zn', self.zone_file() or b'\n'): self.zoned = (self.zones, self.rotate)
            (x, y, w, h), (sw, sh) = head['rect'], head['screen']
            desktop = head.get('desktop', [0, 0, sw, sh])
            if not isinstance(desktop, (list, tuple)) or len(desktop) != 4 or not all(isinstance(n, (int, float)) and math.isfinite(n) for n in desktop) or desktop[2] <= 0 or desktop[3] <= 0:
                raise ValueError('Invalid Krita desktop geometry')
            if self.on_geometry: self.on_geometry(head['rect'], desktop, head)
            else: self.mapper.region = ((x-desktop[0])/desktop[2], (y-desktop[1])/desktop[3], w/desktop[2], h/desktop[3])
            if body:
                self.sent += 1   # frame names are unique across sessions: the tablet reloads only on a new name
                if self.tablet(f"f{int(time.time()*1000) % 10**12}.{'jpg' if head.get('format') == 'jpg' else 'png'}", body): self.have = head['hash']; beat = now; self.dirty = self.dirty or self.sent > 1; self.framed = now; self.urgent = self.urgent or self.rubbed; self.rubbed = self.erase; self.say(f"on · {head.get('name', 'canvas')} · tablet shows the canvas")
    def close(self):
        self.stop_event.set(); self.wake.set()
        if self.thread is not threading.current_thread(): self.thread.join(3)
        if self.feed and self.feed.poll() is None:
            try: self.tablet('bye')
            except (OSError, RuntimeError): pass
            self.feed.close()
        if self.sense_thread and self.sense_thread is not threading.current_thread(): self.sense_thread.join(3)
        self.mapper.region = None
