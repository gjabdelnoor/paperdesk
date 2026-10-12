# SPDX-License-Identifier: AGPL-3.0-or-later
"""Experimental native Quartz tablet events, explicitly selected by the user.

The payload protocol is unit-tested. Genuine receipt by the target macOS art app
must be verified before treating this as a working pressure-sensitive tablet.
"""
from __future__ import annotations

import ctypes

from .native_pointer import CGPoint, MacPointer, PointerPermissionError

# Apple's CGEventField selectors (CoreGraphics/CGEventTypes.h).
MOUSE_PRESSURE, MOUSE_SUBTYPE = 2, 7
TABLET_X, TABLET_Y, TABLET_BUTTONS, TABLET_PRESSURE = 15, 16, 18, 19
TABLET_TILT_X, TABLET_TILT_Y, TABLET_DEVICE_ID = 20, 21, 24
PROX_VENDOR_ID, PROX_TABLET_ID, PROX_POINTER_ID, PROX_DEVICE_ID = 28, 29, 30, 31
PROX_SYSTEM_ID, PROX_VENDOR_TYPE, PROX_SERIAL, PROX_UNIQUE_ID = 32, 33, 34, 35
PROX_CAPABILITIES, PROX_POINTER_TYPE, PROX_ENTER = 36, 37, 38


class MacTabletPointer(MacPointer):
    """Mouse tablet-point subtypes plus a matching tablet-proximity lifecycle."""
    backend = "quartz-pen-experimental"
    experimental = True

    def __init__(self, screen=None, *, graphics=None, foundation=None):
        super().__init__(screen, graphics=graphics, foundation=foundation)
        for name in ("CGEventCreate", "CGEventSetType", "CGEventSetLocation", "CGEventSetIntegerValueField", "CGEventSetDoubleValueField"):
            if not hasattr(self.api, name):
                raise RuntimeError("Quartz tablet event APIs are unavailable. Choose native mouse mode.")
        self.api.CGEventCreate.argtypes = [ctypes.c_void_p]
        self.api.CGEventCreate.restype = ctypes.c_void_p
        self.api.CGEventSetType.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.api.CGEventSetType.restype = None
        self.api.CGEventSetLocation.argtypes = [ctypes.c_void_p, CGPoint]
        self.api.CGEventSetLocation.restype = None
        self.api.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
        self.api.CGEventSetIntegerValueField.restype = None
        self.api.CGEventSetDoubleValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_double]
        self.api.CGEventSetDoubleValueField.restype = None
        self.near = False
        self.eraser = False
        self.pending_down = False

    @property
    def device_id(self):
        return 0x504402 if self.eraser else 0x504401

    def _proximity(self, enter, u, v):
        event = self.api.CGEventCreate(None)
        if not event:
            raise PointerPermissionError("macOS could not create a tablet proximity event. Check Accessibility permissions or choose mouse mode.")
        try:
            self.api.CGEventSetType(event, 24)  # kCGEventTabletProximity
            self.api.CGEventSetLocation(event, CGPoint(*self.screen.point(u, v)))
            fields = {PROX_VENDOR_ID: 0x2EDD, PROX_TABLET_ID: 1, PROX_POINTER_ID: 2 if self.eraser else 1,
                      PROX_DEVICE_ID: self.device_id, PROX_SYSTEM_ID: 0x5044,
                      PROX_VENDOR_TYPE: 2, PROX_SERIAL: 2 if self.eraser else 1,
                      PROX_UNIQUE_ID: 0x5061706572446573 + int(self.eraser),
                      PROX_CAPABILITIES: 0xDB, PROX_POINTER_TYPE: 3 if self.eraser else 1,
                      PROX_ENTER: int(bool(enter))}
            for field, value in fields.items():
                self.api.CGEventSetIntegerValueField(event, field, value)
            self.api.CGEventPost(0, event)
        finally:
            self.foundation.CFRelease(event)

    def _tablet(self, kind, u, v, pressure=0, tilt_x=0, tilt_y=0, barrel=False):
        event = self.api.CGEventCreateMouseEvent(None, kind, CGPoint(*self.screen.point(u, v)), 0)
        if not event:
            raise PointerPermissionError("macOS could not create a tablet-point event. Check Accessibility permissions or choose mouse mode.")
        try:
            integers = {MOUSE_SUBTYPE: 1, TABLET_X: round(min(1, max(0, u)) * 11180),
                        TABLET_Y: round(min(1, max(0, v)) * 15340),
                        TABLET_BUTTONS: (1 if kind in (1, 6) else 0) | (2 if barrel else 0),
                        TABLET_DEVICE_ID: self.device_id}
            for field, value in integers.items():
                self.api.CGEventSetIntegerValueField(event, field, value)
            pressure = min(1, max(0, pressure / 4096))
            doubles = {MOUSE_PRESSURE: pressure, TABLET_PRESSURE: pressure,
                       # Cocoa tilt is normalized; Qt's Cocoa path converts it
                       # to degrees with x*60, y*-60. Saturation is explicit.
                       TABLET_TILT_X: min(1, max(-1, tilt_x / 60)),
                       TABLET_TILT_Y: min(1, max(-1, -tilt_y / 60))}
            for field, value in doubles.items():
                self.api.CGEventSetDoubleValueField(event, field, value)
            self.api.CGEventPost(0, event)
        finally:
            self.foundation.CFRelease(event)

    def send(self, u, v, down, *, pressure=0, tilt_x=0, tilt_y=0, eraser=False, barrel=False, near=True):
        if self.closed:
            raise RuntimeError("The Quartz tablet session is closed.")
        down, near = bool(down) and bool(near), bool(near)
        self.last = (u, v)
        if self.near and bool(eraser) != self.eraser:
            if self.down:
                self._tablet(2, u, v)
                self.down = False
            self._proximity(False, u, v)
            self.near = False
        self.eraser = bool(eraser)
        if near and not self.near:
            self._proximity(True, u, v)
            self.near = True
        if near or self.down:
            kind = (1 if down else 2) if down != self.down else (6 if down else 5)
            self.pending_down = down
            self._tablet(kind, u, v, pressure if down else 0, tilt_x, tilt_y, barrel)
            self.down, self.pending_down = down, False
        if self.near and not near:
            self._proximity(False, u, v)
            self.near = False

    def release(self):
        if not self.closed:
            try:
                self._tablet(2, *self.last)
            finally:
                if self.near:
                    self._proximity(False, *self.last)
                self.down = self.near = self.pending_down = False
