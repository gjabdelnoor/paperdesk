# SPDX-License-Identifier: AGPL-3.0-or-later
# Adapted from remarkable-linux-desktop, by Gabriel Abdelnoor.
# PaperDesk adaptation 2026-10-12: pure event transforms only; no deployment,
# app launching, implicit font-cache modification, or SSH subprocesses.
"""Pressure, tilt, rotation, palm rejection and gestures from the original art bridge."""
import math
import struct
import time
PANEL = (2160, 1620)
RECORD = struct.Struct('<qqHHi')
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
ABS_X, ABS_Y, ABS_PRESSURE, ABS_DISTANCE, ABS_TILT_X, ABS_TILT_Y = 0, 1, 24, 25, 26, 27
BTN_TOOL_PEN, BTN_TOOL_RUBBER, BTN_TOUCH, BTN_STYLUS, BTN_STYLUS2 = 320, 321, 330, 331, 332
KEYS = (BTN_TOOL_PEN, BTN_TOOL_RUBBER, BTN_TOUCH, BTN_STYLUS, BTN_STYLUS2)
X_MAX, Y_MAX, PRESSURE_MAX, DISTANCE_MAX = 11180, 15340, 4096, 65535
TOUCH_X_MAX, TOUCH_Y_MAX, SLOTS = 2064, 2832, 10
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 47, 53, 54, 57
ABS_MT_TOUCH_MAJOR, ABS_MT_TOOL_TYPE, ABS_MT_PRESSURE, MT_TOOL_PALM = 48, 55, 58, 2
SHORT_MM, LONG_MM = 179.6, 239.5
ROTATIONS = ('left', 'right', 'portrait', 'inverted')
class Mapper:
    """Rotates sensor coordinates and crops them to an area matching the screen's shape."""
    def __init__(self, rotate='left', aspect=None, x_max=X_MAX, y_max=Y_MAX):
        if rotate not in ROTATIONS: raise ValueError(rotate)
        self.rotate = rotate; landscape = rotate in ('left', 'right'); self.x_max = x_max; self.y_max = y_max
        self.units = (y_max, x_max) if landscape else (x_max, y_max)
        mm = (LONG_MM, SHORT_MM) if landscape else (SHORT_MM, LONG_MM)
        self.res = (self.units[0]/mm[0], self.units[1]/mm[1])   # units per mm
        w, h = mm
        if aspect:
            if w/h > aspect: w = h*aspect
            else: h = w/aspect
        self.area_mm = (w, h)
        self.span = (round(w*self.res[0]), round(h*self.res[1]))
        self.offset = ((self.units[0]-self.span[0])//2, (self.units[1]-self.span[1])//2)
        self.full = (x_max, y_max)
        self.region = None   # art mode: (x, y, w, h) of the screen, as fractions, that the whole sensor covers
    def face(self, rotate):
        """Art mode follows the tablet as it is turned; the output range stays as created."""
        self.rotate = rotate; self.units = (self.y_max, self.x_max) if rotate in ('left', 'right') else (self.x_max, self.y_max)
    def turn(self, x, y, x_max, y_max):
        """Rotate a point given in portrait axes; pass 0 maxima to rotate a vector such as tilt."""
        if self.rotate == 'left': return y, x_max-x        # top of the tablet points left
        if self.rotate == 'right': return y_max-y, x       # top of the tablet points right
        if self.rotate == 'inverted': return x_max-x, y_max-y
        return x, y
    def point(self, x, y):
        px, py = self.turn(x, y, self.x_max, self.y_max)
        if self.region:
            rx, ry, rw, rh = self.region
            return (max(0, min(self.span[0], round((rx+px/self.units[0]*rw)*self.span[0]))), max(0, min(self.span[1], round((ry+py/self.units[1]*rh)*self.span[1]))))
        return (max(0, min(self.span[0], px-self.offset[0])), max(0, min(self.span[1], py-self.offset[1])))
    def tilt(self, tx, ty):
        """Sensor centidegrees -> whole degrees in the rotated frame."""
        rx, ry = self.turn(tx, ty, 0, 0)
        return max(-90, min(90, round(rx/100))), max(-90, min(90, round(ry/100)))

class Frames:
    """Folds a raw event stream into complete, transformed frames (one per SYN_REPORT)."""
    def __init__(self, mapper):
        self.mapper = mapper; self.x = self.y = self.tx = self.ty = 0
        self.keys = {}; self.axes = {}; self.moved = self.tilted = False; self.pressure = None; self.seen = []; self.leans = []
    def feed(self, kind, code, value):
        """Returns a list of (type, code, value) to emit when a frame completes, else None."""
        if kind == EV_KEY and code in KEYS: self.keys[code] = value
        elif kind == EV_ABS:
            if code == ABS_X: self.x = value; self.moved = True
            elif code == ABS_Y: self.y = value; self.moved = True
            elif code == ABS_TILT_X: self.tx = value; self.tilted = True
            elif code == ABS_TILT_Y: self.ty = value; self.tilted = True
            elif code == ABS_PRESSURE:
                lean = min(90.0, math.hypot(self.tx, self.ty)/100)   # degrees the Marker leans away from upright
                if value and len(self.seen) < 200000: self.seen.append(value); self.leans.append(lean)
                self.axes[code] = self.pressure(value, lean) if self.pressure else value
            elif code == ABS_DISTANCE: self.axes[code] = value
        elif kind == EV_SYN and code == 0:
            out = []
            # Tool/proximity first, so the compositor knows which tool the axes belong to.
            out += [(EV_KEY, c, v) for c, v in self.keys.items() if c in (BTN_TOOL_PEN, BTN_TOOL_RUBBER) and v]
            if self.moved: px, py = self.mapper.point(self.x, self.y); out += [(EV_ABS, ABS_X, px), (EV_ABS, ABS_Y, py)]
            if self.tilted: tx, ty = self.mapper.tilt(self.tx, self.ty); out += [(EV_ABS, ABS_TILT_X, tx), (EV_ABS, ABS_TILT_Y, ty)]
            out += [(EV_ABS, c, v) for c, v in self.axes.items()]
            out += [(EV_KEY, c, v) for c, v in self.keys.items() if not (c in (BTN_TOOL_PEN, BTN_TOOL_RUBBER) and v)]
            self.keys = {}; self.axes = {}; self.moved = self.tilted = False
            return out
        return None

class Gestures:
    """Type-B multi-touch filter: contacts are forwarded only once two fingers are down, and
    never while the pen is in range. A started gesture runs until every finger has lifted.

    Palm rejection. A contact is a palm, and stays one until it lifts, when any of these holds:
    - it was down while the pen was in range (the hand holding the pen), even after the pen leaves;
    - it landed within GRACE seconds of the pen leaving (the hand shifting between strokes);
    - the panel itself calls it a palm (MT_TOOL_PALM);
    - it is wider than `palm_size` (ABS_MT_TOUCH_MAJOR units; off until set, see --palm-size);
    - it landed within NEAR of a contact already known to be a palm (a palm seen as several blobs).
    `fingers` is what is left; for a two-finger gesture a contact also has to be SETTLE seconds old
    (`settled`), so the hand that lands a moment before the pen comes into range starts nothing."""
    GRACE, NEAR, SETTLE = 0.4, 0.18, 0.08
    def __init__(self, mapper, clock=time.monotonic):
        self.mapper = mapper; self.slot = 0; self.raw = {}; self.sent = {}; self.active = False; self.next_id = 1
        self.clock = clock; self.meta = {}; self.near = False; self.pen_left = -1e9; self.palm_size = None; self.rejected = 0; self.log = None; self.widths = {}
    @property
    def pen_near(self): return self.near
    @pen_near.setter
    def pen_near(self, value):
        value = bool(value)
        if value:
            for meta in self.meta.values(): self.palm(meta, 'pen')
        elif self.near: self.pen_left = self.clock()
        self.near = value
    def palm(self, meta, why):
        if not meta['palm']: meta['palm'] = why; self.rejected += 1
    def width_report(self):
        """One line on how wide this session's touches were, for choosing --palm-size, or ''."""
        middle = lambda values: sorted(values)[len(values)//2]
        parts = [f"{label} typical {middle(values)}, {min(values)}-{max(values)} ({len(values)})" for label, values in (('fingers', self.widths.get(False)), ('palms', self.widths.get(True))) if values]
        return 'Touch widths: '+' · '.join(parts) if parts else ''

    def lift(self, slot):
        meta = self.meta.pop(slot, None); self.raw.pop(slot, None)
        if meta and meta['born'] is not None and meta['major']: self.widths.setdefault(bool(meta['palm']), []).append(meta['major'])
        if meta and self.log and meta['born'] is not None:
            self.log.write(json.dumps({'seconds': round(self.clock()-meta['born'], 3), 'major': meta['major'], 'pressure': meta['pressure'], 'tool': meta['tool'], 'palm': meta['palm'] or None})+'\n'); self.log.flush()
    def judge(self):
        now = self.clock(); span = self.NEAR*max(self.mapper.x_max, self.mapper.y_max)
        for slot, meta in self.meta.items():
            if meta['born'] is not None: continue
            meta['born'] = now
            if self.near or now-self.pen_left < self.GRACE: self.palm(meta, 'pen')
            elif any(other['palm'] and math.hypot(self.raw[slot][0]-self.raw[k][0], self.raw[slot][1]-self.raw[k][1]) < span for k, other in self.meta.items() if k != slot and k in self.raw):
                self.palm(meta, 'beside a palm')
    @property
    def fingers(self):
        """Every contact that is not a palm (taps use this: a quick tap is over before SETTLE)."""
        return {slot: at for slot, at in self.raw.items() if slot in self.meta and not self.meta[slot]['palm'] and self.meta[slot]['born'] is not None}
    @property
    def settled(self):
        """The fingers old enough to start or continue a two-finger gesture."""
        now = self.clock(); return {slot: at for slot, at in self.fingers.items() if slot in self.sent or now-self.meta[slot]['born'] >= self.SETTLE}
    def clear(self): self.raw.clear(); self.meta.clear()
    def feed(self, kind, code, value):
        if kind == EV_ABS:
            if code == ABS_MT_SLOT: self.slot = value
            elif code == ABS_MT_TRACKING_ID:
                if value < 0: self.lift(self.slot)
                else:
                    if self.meta.get(self.slot, {}).get('id') != value:
                        if self.slot in self.meta: self.lift(self.slot)
                        self.meta[self.slot] = {'id': value, 'born': None, 'palm': False, 'major': 0, 'pressure': 0, 'tool': 0}
                    self.raw.setdefault(self.slot, [0, 0])
            elif code == ABS_MT_POSITION_X and self.slot in self.raw: self.raw[self.slot][0] = value
            elif code == ABS_MT_POSITION_Y and self.slot in self.raw: self.raw[self.slot][1] = value
            elif self.slot in self.meta:
                meta = self.meta[self.slot]
                if code == ABS_MT_TOUCH_MAJOR:
                    meta['major'] = max(meta['major'], value)
                    if self.palm_size and value >= self.palm_size: self.palm(meta, 'size')
                elif code == ABS_MT_PRESSURE: meta['pressure'] = max(meta['pressure'], value)
                elif code == ABS_MT_TOOL_TYPE:
                    meta['tool'] = max(meta['tool'], value)
                    if value == MT_TOOL_PALM: self.palm(meta, 'panel')
            return None
        if not (kind == EV_SYN and code == 0): return None
        self.judge(); raw = self.settled
        return self.forward(raw)
    def forward(self, raw):
        if not self.active and len(raw) >= 2 and not self.pen_near: self.active = True
        if self.active and self.pen_near: return self.cancel()
        if not self.active: return None
        out = []
        for slot in [s for s in self.sent if s not in raw]:
            out += [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_TRACKING_ID, -1)]; del self.sent[slot]
        for slot, (x, y) in raw.items():
            point = self.mapper.point(x, y)
            if slot not in self.sent:
                out += [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_TRACKING_ID, self.next_id)]; self.next_id = self.next_id % 60000 + 1; self.sent[slot] = None
            if self.sent[slot] != point:
                out += [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_POSITION_X, point[0]), (EV_ABS, ABS_MT_POSITION_Y, point[1])]; self.sent[slot] = point
        if not self.sent: self.active = False; out.append((EV_KEY, BTN_TOUCH, 0))
        elif not self.touching: out.append((EV_KEY, BTN_TOUCH, 1))
        self.touching = bool(self.sent)
        return out
    touching = False
    def cancel(self):
        """Lift everything (pen arrived or link dropped)."""
        out = []
        for slot in list(self.sent): out += [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_TRACKING_ID, -1)]
        if self.touching: out.append((EV_KEY, BTN_TOUCH, 0))
        self.sent = {}; self.active = False; self.touching = False
        return out

class Pinch:
    """Two-finger pinch and drag as (scale, anchor, movement) steps, in fractions of the tablet."""
    def __init__(self, mapper): self.mapper = mapper; self.last = None
    def step(self, raw, pen_near):
        if len(raw) < 2 or pen_near: self.last = None; return None
        m = self.mapper; (a, b) = [m.turn(x, y, m.x_max, m.y_max) for x, y in list(raw.values())[:2]]
        a, b = [(px/m.units[0], py/m.units[1]) for px, py in (a, b)]
        centre = ((a[0]+b[0])/2, (a[1]+b[1])/2)
        spread = math.hypot((a[0]-b[0])*m.units[0], (a[1]-b[1])*m.units[1])/m.units[0]
        now = (centre, spread, tuple(list(raw)[:2])); last, self.last = self.last, now
        if not last or last[2] != now[2] or last[1] < 0.02 or spread < 0.02: return None
        return {'scale': spread/last[1], 'at': last[0], 'move': (centre[0]-last[0][0], centre[1]-last[0][1])}

class Taps:
    """One-finger taps on the tablet: (u, v, double) when a short, still, lone touch lifts.
    u and v are fractions of the picture as it is being held; resting palms are too long or
    too wide to count, and nothing counts while the pen is in range."""
    def __init__(self, mapper): self.mapper = mapper; self.start = None; self.last = None
    def step(self, raw, pen_near, now):
        m = self.mapper
        if raw:
            x, y = next(iter(raw.values())); px, py = m.turn(x, y, m.x_max, m.y_max); at = (px/m.units[0], py/m.units[1])
            if self.start is None: self.start = [now, at, True]
            start = self.start
            if len(raw) > 1 or pen_near or math.hypot(at[0]-start[1][0], at[1]-start[1][1]) > 0.03: start[2] = False
            return None
        start, self.start = self.start, None
        if not start or not start[2] or pen_near or now-start[0] > 0.3: return None
        double = bool(self.last and now-self.last[0] < 0.45 and math.hypot(start[1][0]-self.last[1][0], start[1][1]-self.last[1][1]) < 0.08)
        self.last = None if double else (now, start[1])
        return start[1][0], start[1][1], double

ART_FILES = ('.kra', '.ora', '.psd', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp')
def orientation(raw, matrix, current, flat=0.45):
    """Which way the tablet is held, from one accelerometer sample; `current` while it lies flat.

    The mount matrix puts the sample in device axes (x toward the right edge, y toward the
    top edge, z out of the screen); the axis pointing up reads positive.
    """
    x, y, z = (sum(m*v for m, v in zip(row, raw)) for row in matrix)
    size = math.sqrt(x*x+y*y+z*z) or 1
    if max(abs(x), abs(y)) < flat*size or min(abs(x), abs(y))*1.5 > max(abs(x), abs(y)): return current
    if abs(x) > abs(y): return 'left' if x > 0 else 'right'
    return 'portrait' if y > 0 else 'inverted'

TILT_FROM, TILT_FULL = 50.0, 75.0
def pressure_curve(full, gamma, tilt=0.0):
    """Raw marker pressure -> reported pressure: full strength at `full` of the hardware range,
    bent by `gamma` (below 1 makes light strokes stronger). The pen's lean counts too, as with a
    pencil used on its side: upright or at a writing angle (up to TILT_FROM degrees from upright)
    changes nothing; laid over further, the same touch counts for up to 1+`tilt` times as much,
    reached at TILT_FULL degrees. Off (0) unless --tilt is given."""
    top = max(1, full*PRESSURE_MAX)
    def curve(raw, lean=0.0):
        if raw <= 0: return 0
        gain = 1+tilt*max(0.0, min(1.0, (lean-TILT_FROM)/(TILT_FULL-TILT_FROM)))
        return max(1, min(PRESSURE_MAX, round(PRESSURE_MAX*min(1.0, gain*min(1.0, raw/top)**gamma))))
    return curve

