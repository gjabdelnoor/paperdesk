# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small native pointer adapters. Mouse adapters never claim tablet pressure."""
from __future__ import annotations

import ctypes
import ctypes.util
import math
import sys
from dataclasses import dataclass


class PointerPermissionError(RuntimeError):
    """The operating system declined native event injection."""


@dataclass(frozen=True)
class ScreenRect:
    x: float
    y: float
    width: float
    height: float

    def point(self, u: float, v: float) -> tuple[float, float]:
        if not all(math.isfinite(n) for n in (self.x, self.y, self.width, self.height, u, v)):
            raise ValueError("Screen coordinates must be finite.")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("The screen has no usable drawing area.")
        return self.x + min(1, max(0, u)) * (self.width - 1), self.y + min(1, max(0, v)) * (self.height - 1)


# Fixed-width Win32 types: ctypes.c_long is 64-bit on Unix test hosts.
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32),
                ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16), ("wParamH", ctypes.c_uint16)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", ctypes.c_uint32), ("data", _INPUTUNION)]


class WindowsPointer:
    """Use SendInput absolute virtual-desktop mouse events; no pressure or tilt."""
    MOVE, DOWN, UP, ABSOLUTE, VIRTUALDESK = 1, 2, 4, 0x8000, 0x4000

    def __init__(self, screen: ScreenRect | None = None, *, user32=None):
        if user32 is None:
            if sys.platform != "win32":
                raise RuntimeError("Windows pointer injection is available on Windows only.")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.api = user32
        self.api.SendInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(INPUT), ctypes.c_int]
        self.api.SendInput.restype = ctypes.c_uint32
        self.api.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.api.GetSystemMetrics.restype = ctypes.c_int
        # SM_X/Y/CX/CYVIRTUALSCREEN; normalized absolute values cover all monitors.
        self.virtual = ScreenRect(*(self.api.GetSystemMetrics(n) for n in (76, 77, 78, 79)))
        self.screen = screen or self.virtual
        self.screen.point(0, 0)
        self.virtual.point(0, 0)
        self.down = False
        self.last = (0.5, 0.5)
        self.closed = False

    def send(self, u: float, v: float, down: bool) -> None:
        if self.closed:
            raise RuntimeError("The pointer session is closed.")
        x, y = self.screen.point(u, v)
        dx = round((x - self.virtual.x) * 65535 / max(1, self.virtual.width - 1))
        dy = round((y - self.virtual.y) * 65535 / max(1, self.virtual.height - 1))
        flags = self.MOVE | self.ABSOLUTE | self.VIRTUALDESK
        if bool(down) != self.down:
            flags |= self.DOWN if down else self.UP
        event = INPUT(type=0, mi=MOUSEINPUT(min(65535, max(0, dx)), min(65535, max(0, dy)), 0, flags, 0, 0))
        if self.api.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
            raise PointerPermissionError("Windows blocked pointer input. Run the art application and PaperDesk at the same privilege level; PaperDesk never elevates itself.")
        self.down, self.last = bool(down), (u, v)

    def release(self) -> None:
        if not self.closed:
            # Send an explicit mouse-up even when our last down event's receipt is uncertain.
            self.down = True
            self.send(*self.last, False)

    def close(self) -> None:
        if not self.closed:
            try:
                self.release()
            finally:
                self.closed = True


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


def mac_input_permission() -> bool:
    """Read-only event-posting permission check; never prompts or emits events."""
    if sys.platform != "darwin": return False
    try:
        api = ctypes.CDLL(ctypes.util.find_library("CoreGraphics") or "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        check = api.CGPreflightPostEventAccess
        check.argtypes = []; check.restype = ctypes.c_bool
        return bool(check())
    except (OSError, AttributeError): return False


class MacPointer:
    """Post Quartz mouse events, with an explicit Accessibility permission gate."""
    def __init__(self, screen: ScreenRect | None = None, *, graphics=None, foundation=None):
        if graphics is None:
            if sys.platform != "darwin":
                raise RuntimeError("Quartz pointer injection is available on macOS only.")
            graphics = ctypes.CDLL(ctypes.util.find_library("CoreGraphics") or "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            foundation = ctypes.CDLL(ctypes.util.find_library("CoreFoundation") or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.api, self.foundation = graphics, foundation
        self.api.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
        self.api.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        self.api.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        self.api.CGEventPost.restype = None
        self.api.CGMainDisplayID.argtypes = []
        self.api.CGMainDisplayID.restype = ctypes.c_uint32
        self.api.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        self.api.CGDisplayBounds.restype = CGRect
        self.foundation.CFRelease.argtypes = [ctypes.c_void_p]
        self.foundation.CFRelease.restype = None
        # Checking does not prompt or change the user's system settings.
        if hasattr(self.api, "CGPreflightPostEventAccess"):
            self.api.CGPreflightPostEventAccess.argtypes = []
            self.api.CGPreflightPostEventAccess.restype = ctypes.c_bool
            if not self.api.CGPreflightPostEventAccess():
                raise PointerPermissionError("Allow PaperDesk in System Settings → Privacy & Security → Accessibility, then start art mode again. This permission lets PaperDesk move and click the pointer only during an art session.")
        bounds = self.api.CGDisplayBounds(self.api.CGMainDisplayID())
        self.screen = screen or ScreenRect(bounds.origin.x, bounds.origin.y, bounds.size.width, bounds.size.height)
        self.screen.point(0, 0)
        self.down = False
        self.last = (0.5, 0.5)
        self.closed = False

    def _post(self, kind: int, u: float, v: float) -> None:
        event = self.api.CGEventCreateMouseEvent(None, kind, CGPoint(*self.screen.point(u, v)), 0)
        if not event:
            raise PointerPermissionError("macOS could not create a pointer event. Check Accessibility permission for PaperDesk.")
        try:
            self.api.CGEventPost(0, event)
        finally:
            self.foundation.CFRelease(event)

    def send(self, u: float, v: float, down: bool) -> None:
        if self.closed:
            raise RuntimeError("The pointer session is closed.")
        if bool(down) != self.down:
            self._post(1 if down else 2, u, v)
        else:
            self._post(6 if down else 5, u, v)
        self.down, self.last = bool(down), (u, v)

    def release(self) -> None:
        if not self.closed:
            self._post(2, *self.last)
            self.down = False

    def close(self) -> None:
        if not self.closed:
            try:
                self.release()
            finally:
                self.closed = True
