# SPDX-License-Identifier: AGPL-3.0-or-later
"""Observe a short native pen sequence in an isolated Windows CI Qt window.

This is an explicit runtime probe, never an ordinary pytest test or app startup
hook. It refuses Linux, macOS, local Windows and self-hosted runners before loading
user32 or Qt. A mock API is useful for testing this probe, but is not runtime proof.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import sys

from paperdesk.native_pointer import PointerPermissionError, ScreenRect
from paperdesk import windows_ink as ink


WM_POINTERUPDATE, WM_POINTERDOWN, WM_POINTERUP = 0x245, 0x246, 0x247
POINTER_MESSAGES = {
    WM_POINTERUPDATE: "WM_POINTERUPDATE", WM_POINTERDOWN: "WM_POINTERDOWN",
    WM_POINTERUP: "WM_POINTERUP", 0x249: "WM_POINTERENTER", 0x24A: "WM_POINTERLEAVE",
}
# Raw adapter pressure (0..4096), x tilt, y tilt. The expected Windows pressure is
# deliberately specified independently instead of calling the adapter's mapper.
SAMPLES = ((1024, -30, 20), (2048, 0, -15), (3072, 35, 30))
EXPECTED_NATIVE = ((256, -30, 20), (512, 0, -15), (768, 35, 30))


class MSG(ctypes.Structure):
    """Win32 MSG: DWORD/LONG stay 32-bit even on a Unix test host."""
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint32), ("pt", ink.POINT),
                ("lPrivate", ctypes.c_uint32)]


class USEROBJECTFLAGS(ctypes.Structure):
    _fields_ = [("fInherit", ctypes.c_int32), ("fReserved", ctypes.c_int32),
                ("dwFlags", ctypes.c_uint32)]


class ProbeUnsupported(RuntimeError):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code


def runtime_guard(*, requested=False, current_platform=None, environ=None):
    """Return a refusal before *any* desktop/input API is loaded."""
    current_platform = sys.platform if current_platform is None else current_platform
    environ = os.environ if environ is None else environ
    if not requested:
        return "explicit_ci_probe_required", "Pass --run-isolated-ci to request this runtime check."
    if current_platform != "win32":
        return "not_windows", "Native Windows Ink runtime verification requires Windows."
    if environ.get("CI", "").lower() != "true":
        return "not_ci", "This probe runs only in isolated CI, never on a user's desktop."
    if (environ.get("GITHUB_ACTIONS", "").lower() != "true"
            or environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or environ.get("RUNNER_OS") != "Windows"):
        return "not_isolated_hosted_runner", "This probe requires a GitHub-hosted Windows VM; self-hosted and local runners are refused."
    return None


def _last_error():
    return getattr(ctypes, "get_last_error", lambda: 0)()


def pen_snapshot(pen):
    info = pen.pointerInfo
    return {"pointer_type": int(info.pointerType), "pointer_id": int(info.pointerId),
            "flags": int(info.pointerFlags), "pen_mask": int(pen.penMask),
            "pressure": int(pen.pressure), "tilt_x": int(pen.tiltX),
            "tilt_y": int(pen.tiltY),
            "x": int(info.ptPixelLocation.x), "y": int(info.ptPixelLocation.y)}


class _ForwardFunction:
    """Keep ctypes signatures on the real DLL function while auditing calls."""
    def __init__(self, function, callback):
        self.function, self.callback = function, callback

    @property
    def argtypes(self):
        return self.function.argtypes

    @argtypes.setter
    def argtypes(self, value):
        self.function.argtypes = value

    @property
    def restype(self):
        return self.function.restype

    @restype.setter
    def restype(self, value):
        self.function.restype = value

    def __call__(self, *args):
        return self.callback(self.function, *args)


class AuditedAPI:
    """Delegate to actual user32; counters never replace the OS receiver."""
    def __init__(self, api):
        self.api = api
        self.created, self.destroyed, self.injections = [], [], []
        self.CreateSyntheticPointerDevice = _ForwardFunction(api.CreateSyntheticPointerDevice, self._create)
        self.InjectSyntheticPointerInput = _ForwardFunction(api.InjectSyntheticPointerInput, self._inject)
        self.DestroySyntheticPointerDevice = _ForwardFunction(api.DestroySyntheticPointerDevice, self._destroy)

    def __getattr__(self, name):
        return getattr(self.api, name)

    def _create(self, function, *args):
        result = function(*args)
        if result:
            self.created.append(int(result))
        return result

    def _inject(self, function, handle, events, count):
        packet = [pen_snapshot(events[n].penInfo) for n in range(count)]
        result = function(handle, events, count)
        self.injections.append({"accepted": bool(result), "error": 0 if result else _last_error(),
                                "events": packet})
        return result

    def _destroy(self, function, handle):
        result = function(handle)
        self.destroyed.append(int(handle))
        return result

    def summary(self):
        # Handles themselves are deliberately omitted from saved artifacts.
        remaining = [handle for handle in self.created if handle not in self.destroyed]
        return {"created": len(self.created), "destroyed": len(self.destroyed),
                "handles_remaining": len(remaining), "attempts": self.injections,
                "all_devices_destroyed": not remaining and len(self.created) == len(self.destroyed)}


def configure_receiver_api(api):
    signatures = {
        "GetPointerPenInfo": ([ink.UINT, ctypes.POINTER(ink.POINTER_PEN_INFO)], ctypes.c_int32),
        "GetPointerDeviceRects": ([ink.HANDLE, ctypes.POINTER(ink.RECT), ctypes.POINTER(ink.RECT)], ctypes.c_int32),
        "GetClientRect": ([ink.HANDLE, ctypes.POINTER(ink.RECT)], ctypes.c_int32),
        "ClientToScreen": ([ink.HANDLE, ctypes.POINTER(ink.POINT)], ctypes.c_int32),
        "WindowFromPoint": ([ink.POINT], ink.HANDLE),
        "IsWindowVisible": ([ink.HANDLE], ctypes.c_int32),
        "GetProcessWindowStation": ([], ink.HANDLE),
        "GetUserObjectInformationW": ([ink.HANDLE, ctypes.c_int32, ctypes.c_void_p, ink.UINT,
                                       ctypes.POINTER(ink.UINT)], ctypes.c_int32),
        "OpenInputDesktop": ([ink.UINT, ctypes.c_int32, ink.UINT], ink.HANDLE),
        "CloseDesktop": ([ink.HANDLE], ctypes.c_int32),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result


def check_input_desktop(api):
    """Read desktop access only; never switch desktop, elevate, or change ACLs."""
    station = api.GetProcessWindowStation()
    flags, needed = USEROBJECTFLAGS(), ink.UINT()
    if not station or not api.GetUserObjectInformationW(station, 1, ctypes.byref(flags),
                                                       ctypes.sizeof(flags), ctypes.byref(needed)):
        raise ProbeUnsupported("window_station_unavailable", f"The CI window station could not be inspected (error {_last_error()}).")
    if not flags.dwFlags & 1:  # WSF_VISIBLE
        raise ProbeUnsupported("noninteractive_window_station", "The hosted runner has no visible interactive window station.")
    desktop = api.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS, no write/switch rights
    if not desktop:
        raise ProbeUnsupported("input_desktop_unavailable", f"Windows refused access to the input desktop (error {_last_error()}).")
    if not api.CloseDesktop(desktop):
        raise RuntimeError(f"Could not close the probe's desktop handle (error {_last_error()}).")


def decode_pointer_message(message, api, target_hwnd):
    """Read pen data while its genuine Win32 message is current on the UI thread."""
    if int(message.hwnd or 0) != target_hwnd or message.message not in POINTER_MESSAGES:
        return None
    pointer_id = int(message.wParam) & 0xFFFF  # GET_POINTERID_WPARAM
    event = {"message": POINTER_MESSAGES[message.message], "pointer_id": pointer_id,
             "source": "native-window-message"}
    pen = ink.POINTER_PEN_INFO()
    if not api.GetPointerPenInfo(pointer_id, ctypes.byref(pen)):
        event.update({"read_ok": False, "error": _last_error()})
        return event
    event.update(pen_snapshot(pen), read_ok=True)
    # Qt's Windows pen translator uses this API. Preserve a useful diagnosis if
    # the OS accepts the injected pen but Qt cannot translate that device.
    pointer_rect, display_rect = ink.RECT(), ink.RECT()
    event["device_rects_available"] = bool(api.GetPointerDeviceRects(
        pen.pointerInfo.sourceDevice, ctypes.byref(pointer_rect), ctypes.byref(display_rect)))
    return event


def own_target_points(api, hwnd):
    rect = ink.RECT()
    if not api.GetClientRect(hwnd, ctypes.byref(rect)) or rect.right < 200 or rect.bottom < 150:
        raise ProbeUnsupported("target_geometry_unavailable", "The CI receiver has no usable native client area.")
    result = []
    for offset in (-40, 0, 40):
        point = ink.POINT(rect.right // 2 + offset, rect.bottom // 2)
        if not api.ClientToScreen(hwnd, ctypes.byref(point)):
            raise ProbeUnsupported("target_geometry_unavailable", "Windows could not locate the receiver on screen.")
        result.append((int(point.x), int(point.y)))
    return result


def require_own_target(api, hwnd, point):
    if not api.IsWindowVisible(hwnd) or int(api.WindowFromPoint(ink.POINT(*point)) or 0) != hwnd:
        raise ProbeUnsupported("target_not_visible", "The probe's own window is not the visible input target; no pen sequence can be verified.")


def run_sequence(pointer, points, pump, require_target):
    """Always attempt final up and destruction, even if a send/pump fails."""
    try:
        require_target(points[0])
        pointer.send(*points[0], False, near=True)
        pump(120)
        for point, (pressure, tilt_x, tilt_y) in zip(points, SAMPLES, strict=True):
            require_target(point)
            pointer.send(*point, True, pressure=pressure, tilt_x=tilt_x, tilt_y=tilt_y)
            pump(120)
        # Consume the final WM_POINTERUP/GetPointerPenInfo while the injection
        # device is still alive. close() is the destruction/fallback-release
        # guarantee, and is never relied on as proof of event receipt.
        pointer.release()
        pump(200)
    finally:
        try:
            pointer.close()
        finally:
            pump(100)


def validate_observations(native, qt, audit):
    good = [event for event in native if event.get("read_ok") and event.get("pointer_type") == ink.PT_PEN]
    contact = [event for event in good if event["flags"] & ink.INCONTACT]
    native_values = {(event["pressure"], event["tilt_x"], event["tilt_y"]) for event in contact}
    qt_contact = [event for event in qt if event["type"] in ("TabletPress", "TabletMove") and event["pressure"] > 0]
    qt_values = {(round(event["pressure"] * 1024), event["tilt_x"], event["tilt_y"])
                 for event in qt_contact if event["spontaneous"]}
    native_up = [event for event in good if event["message"] == "WM_POINTERUP"]
    qt_up = [event for event in qt if event["type"] == "TabletRelease" and event["spontaneous"]]
    accepted = [event for attempt in audit["attempts"] if attempt["accepted"] for event in attempt["events"]]
    checks = {
        "native_pen_down_received": any(event["message"] == "WM_POINTERDOWN" for event in contact),
        "native_pressure_tilt_preserved": set(EXPECTED_NATIVE) <= native_values,
        "native_final_up_received": bool(native_up) and bool(native_up[-1]["flags"] & ink.UP)
                                    and not bool(native_up[-1]["flags"] & ink.INCONTACT)
                                    and native_up[-1]["pressure"] == 0,
        "qt_tablet_press_received": any(event["type"] == "TabletPress" and event["spontaneous"] for event in qt_contact),
        "qt_pressure_tilt_preserved": set(EXPECTED_NATIVE) <= qt_values,
        "qt_final_up_received": bool(qt_up) and qt_up[-1]["pressure"] == 0,
        "final_up_injected": bool(accepted) and bool(accepted[-1]["flags"] & ink.UP)
                             and not bool(accepted[-1]["flags"] & ink.INCONTACT),
        "device_destroyed": audit["created"] == 1 and audit["destroyed"] == 1 and audit["all_devices_destroyed"],
    }
    return checks


def probe(*, requested=False, current_platform=None, environ=None):
    report = {"schema": 1, "check": "windows-ink-native-runtime", "status": "unsupported",
              "verified": False, "platform": sys.platform if current_platform is None else current_platform,
              "native_events": [], "qt_tablet_events": [], "checks": {},
              "physical_tablet_tested": False}
    refused = runtime_guard(requested=requested, current_platform=current_platform, environ=environ)
    # The injectable platform argument supports refusal tests. It must never let
    # a caller simulate win32 and load a Windows QPA plugin on a Unix desktop.
    if refused is None and sys.platform != "win32":
        refused = ("not_windows", "The actual Python runtime is not Windows; native input is refused.")
    if refused:
        report["reason"] = {"code": refused[0], "message": refused[1]}
        return report

    app = widget = event_filter = pointer = audit = None
    try:
        api = ink._load_api()
        if not ink.windows_ink_available(api):
            raise ProbeUnsupported("synthetic_pen_api_unavailable", "This Windows image does not expose the native synthetic pointer APIs.")
        configure_receiver_api(api)
        check_input_desktop(api)
        audit = AuditedAPI(api)
        # Imported only after the hosted-Windows guard. offscreen/minimal cannot
        # prove Windows delivery, even if other CI smoke checks use those plugins.
        from PySide6.QtCore import QAbstractNativeEventFilter, QEventLoop, QTimer, Qt, qVersion
        from PySide6.QtWidgets import QApplication, QWidget
        if QApplication.instance() is not None:
            raise ProbeUnsupported("existing_qt_application", "Run the probe in its own process with the native Windows QPA plugin.")
        app = QApplication(["paperdesk-windows-ink-check", "-platform", "windows"])
        report.update(qt_version=qVersion(), qt_platform=app.platformName(), os_version=platform.version())
        if app.platformName() != "windows":
            raise ProbeUnsupported("not_windows_qpa", "Qt did not select the native Windows QPA plugin.")

        class Receiver(QWidget):
            def tabletEvent(self, event):
                report["qt_tablet_events"].append({
                    "source": "qt-tablet-event", "type": event.type().name,
                    "spontaneous": event.spontaneous(), "pressure": event.pressure(),
                    "tilt_x": event.xTilt(), "tilt_y": event.yTilt(),
                    "x": event.globalPosition().x(), "y": event.globalPosition().y(),
                })
                event.accept()

        widget = Receiver()
        widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        widget.setAttribute(Qt.WidgetAttribute.WA_TabletTracking)
        widget.setWindowTitle("PaperDesk isolated CI native pen receiver")
        widget.resize(480, 320)
        if app.primaryScreen() is None:
            raise ProbeUnsupported("screen_unavailable", "Qt found no screen on the hosted Windows desktop.")
        available = app.primaryScreen().availableGeometry()
        widget.move(available.center() - widget.rect().center())
        widget.show()
        widget.raise_()
        widget.activateWindow()
        hwnd = int(widget.winId())

        class NativeFilter(QAbstractNativeEventFilter):
            def nativeEventFilter(self, event_type, message):
                try:
                    if bytes(event_type) == b"windows_generic_MSG":
                        native = ctypes.cast(int(message), ctypes.POINTER(MSG)).contents
                        event = decode_pointer_message(native, api, hwnd)
                        if event is not None:
                            report["native_events"].append(event)
                except Exception as error:
                    # Exceptions in PySide virtual callbacks otherwise go only to
                    # stderr. A receiver failure must prevent a verified report.
                    report.setdefault("receiver_errors", []).append(type(error).__name__ + ": " + str(error))
                return False, 0  # Observe only; Qt still translates the message.

        event_filter = NativeFilter()
        app.installNativeEventFilter(event_filter)

        def pump(milliseconds):
            loop = QEventLoop()
            QTimer.singleShot(milliseconds, loop.quit)
            loop.exec()

        pump(500)
        screen = ScreenRect(*(api.GetSystemMetrics(index) for index in (76, 77, 78, 79)))
        pixel_points = own_target_points(api, hwnd)
        for point in pixel_points:
            require_own_target(api, hwnd, point)
        normalized = [((x - screen.x) / (screen.width - 1), (y - screen.y) / (screen.height - 1))
                      for x, y in pixel_points]
        report["expected"] = [{"x": point[0], "y": point[1], "pressure": sample[0],
                               "tilt_x": sample[1], "tilt_y": sample[2]}
                              for point, sample in zip(pixel_points, EXPECTED_NATIVE, strict=True)]
        pointer = ink.WindowsInkPointer(screen, api=audit)
        def target(point):
            require_own_target(api, hwnd, tuple(round(value) for value in screen.point(*point)))
        run_sequence(pointer, normalized, pump, target)
        report["audit"] = audit.summary()
        report["checks"] = validate_observations(report["native_events"], report["qt_tablet_events"], report["audit"])
        if report.get("receiver_errors"):
            raise RuntimeError("The native event receiver failed; inspect receiver_errors.")
        if all(report["checks"].values()):
            report.update(status="verified", verified=True)
            report["reason"] = {"code": "observed_native_pen", "message": "Windows and Qt received the pen pressure/tilt sequence and final up; the synthetic device was destroyed."}
        elif not report["native_events"]:
            raise ProbeUnsupported("native_events_not_delivered", "Windows accepted input but the hosted desktop delivered no native pointer messages; runtime pressure/tilt remains unverified.")
        else:
            report["status"] = "failed"
            report["reason"] = {"code": "receiver_checks_failed", "message": "The genuine receiver observations did not satisfy every pressure/tilt, final-up and cleanup check."}
    except ProbeUnsupported as error:
        report["reason"] = {"code": error.code, "message": str(error)}
    except (ink.WindowsInkUnavailable, PointerPermissionError) as error:
        report["reason"] = {"code": "native_api_refused", "message": str(error), "win32_error": _last_error()}
    except Exception as error:
        report["status"] = "failed"
        report["reason"] = {"code": "probe_error", "message": type(error).__name__ + ": " + str(error)}
    finally:
        if pointer is not None and not pointer.closed:
            try:
                pointer.close()
            except Exception as error:
                report["cleanup_error"] = type(error).__name__ + ": " + str(error)
        if audit is not None:
            report["audit"] = audit.summary()
            if not report["audit"]["all_devices_destroyed"]:
                report.update(status="failed", verified=False)
                report["reason"] = {"code": "device_cleanup_failed", "message": "The probe could not account for destruction of every created synthetic device."}
        if app is not None and event_filter is not None:
            app.removeNativeEventFilter(event_filter)
        if widget is not None:
            widget.close()
        if app is not None:
            app.processEvents()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-isolated-ci", action="store_true")
    parser.add_argument("--report", type=Path, help="Write the JSON evidence artifact.")
    parser.add_argument("--require-verified", action="store_true", help="Also fail on unsupported CI desktops.")
    args = parser.parse_args(argv)
    report = probe(requested=args.run_isolated_ci)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return int(report["status"] == "failed" or (args.require_verified and not report["verified"]))


if __name__ == "__main__":
    raise SystemExit(main())
