# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native platform probe contracts; all APIs here are deterministic in-memory fakes.

These checks run on Linux without touching any desktop, tablet, or input device.
Only tools/windows_ink_check.py on an explicitly requested hosted Windows VM may
generate native input. Fake observations below never count as runtime evidence.
"""
import ctypes
import json

import pytest

from paperdesk.native_pointer import PointerPermissionError, ScreenRect
from paperdesk import windows_ink as ink
from tools import windows_ink_check as check


class Function:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakeUser32:
    """Win32-shaped functions with no native DLL, window, process or network."""
    def __init__(self, fail_injections=(), create_ok=True):
        self.packets, self.destroyed = [], []
        self.fail_injections = set(fail_injections)
        self.CreateSyntheticPointerDevice = Function(lambda *_: 73 if create_ok else 0)
        self.InjectSyntheticPointerInput = Function(self.inject)
        self.DestroySyntheticPointerDevice = Function(lambda handle: self.destroyed.append(handle))
        self.GetSystemMetrics = Function(lambda index: {76: 0, 77: 0, 78: 1600, 79: 900}[index])

    def inject(self, handle, events, count):
        assert handle == 73 and count == 1
        self.packets.append([check.pen_snapshot(events[index].penInfo) for index in range(count)])
        return len(self.packets) not in self.fail_injections


HOSTED_WINDOWS = {"CI": "true", "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
                  "RUNNER_OS": "Windows"}


@pytest.fixture(autouse=True)
def deterministic_fake_last_error(monkeypatch):
    # ctypes' thread-local error on a Windows runner may contain another test's
    # real DLL error. These tests exercise only the fake no-retry error path.
    monkeypatch.setattr(ink, "_last_error", lambda: 0)
    monkeypatch.setattr(check, "_last_error", lambda: 0)


@pytest.mark.parametrize("current_platform,environ,requested,code", [
    ("linux", HOSTED_WINDOWS, True, "not_windows"),
    ("darwin", HOSTED_WINDOWS, True, "not_windows"),
    ("win32", HOSTED_WINDOWS, False, "explicit_ci_probe_required"),
    ("win32", {}, True, "not_ci"),
    ("win32", {"CI": "true"}, True, "not_isolated_hosted_runner"),
    ("win32", {**HOSTED_WINDOWS, "RUNNER_ENVIRONMENT": "self-hosted"}, True, "not_isolated_hosted_runner"),
    ("win32", {**HOSTED_WINDOWS, "RUNNER_OS": "Linux"}, True, "not_isolated_hosted_runner"),
])
def test_runtime_probe_refuses_before_loading_native_or_qt(monkeypatch, current_platform, environ, requested, code):
    def unexpected_load():
        pytest.fail("Refused runtime probe must never load a native API")
    monkeypatch.setattr(ink, "_load_api", unexpected_load)
    report = check.probe(requested=requested, current_platform=current_platform, environ=environ)
    assert report["status"] == "unsupported" and not report["verified"]
    assert report["reason"]["code"] == code
    assert not report["native_events"] and not report["qt_tablet_events"]
    assert "audit" not in report
    json.dumps(report)


def test_only_explicit_hosted_windows_passes_guard():
    assert check.runtime_guard(requested=True, current_platform="win32", environ=HOSTED_WINDOWS) is None


def test_probe_platform_override_cannot_load_windows_input_on_linux(monkeypatch):
    monkeypatch.setattr(check.sys, "platform", "linux")
    monkeypatch.setattr(ink, "_load_api", lambda: pytest.fail("Actual Unix runtime must refuse Windows input"))
    report = check.probe(requested=True, current_platform="win32", environ=HOSTED_WINDOWS)
    assert report["reason"]["code"] == "not_windows" and not report["verified"]


def test_windows_ctypes_abi_has_fixed_widths_and_native_pointer_alignment():
    assert ctypes.sizeof(ink.UINT) == ctypes.sizeof(ink.LONG) == 4
    assert ctypes.sizeof(ink.POINT) == 8
    assert ctypes.sizeof(ink.RECT) == 16
    assert ctypes.sizeof(check.USEROBJECTFLAGS) == 12
    assert dict(ink.POINTER_PEN_INFO._fields_)["tiltX"] is ctypes.c_int32
    assert dict(ink.POINTER_PEN_INFO._fields_)["pressure"] is ctypes.c_uint32
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        # Independent Win64 C ABI expectations (LLP64), including the touch union
        # that controls POINTER_TYPE_INFO's size even during a pen-only stream.
        assert {kind.__name__: ctypes.sizeof(kind) for kind in
                (ink.POINTER_INFO, ink.POINTER_PEN_INFO, ink.POINTER_TOUCH_INFO, ink.POINTER_TYPE_INFO)} == {
                    "POINTER_INFO": 96, "POINTER_PEN_INFO": 120,
                    "POINTER_TOUCH_INFO": 144, "POINTER_TYPE_INFO": 152}
        assert ink.POINTER_INFO.sourceDevice.offset == 16
        assert ink.POINTER_INFO.ptPixelLocation.offset == 32
        assert ink.POINTER_INFO.PerformanceCount.offset == 80
        assert ink.POINTER_PEN_INFO.pressure.offset == 104
        assert ink.POINTER_PEN_INFO.tiltY.offset == 116
        assert ink.POINTER_TYPE_INFO.data.offset == 8
        assert ctypes.sizeof(check.MSG) == 48
        assert check.MSG.wParam.offset == 16 and check.MSG.time.offset == 32
        assert check.MSG.lPrivate.offset == 44


def make_pointer(fail_injections=(), create_ok=True):
    raw = FakeUser32(fail_injections, create_ok)
    audit = check.AuditedAPI(raw)
    pointer = ink.WindowsInkPointer(ScreenRect(0, 0, 1600, 900), api=audit)
    return pointer, raw, audit


def test_audit_forwards_actual_ctypes_signatures_without_replacing_call_result():
    pointer, raw, audit = make_pointer()
    assert raw.CreateSyntheticPointerDevice.argtypes == [ctypes.c_uint32] * 3
    assert raw.CreateSyntheticPointerDevice.restype is ctypes.c_void_p
    assert raw.InjectSyntheticPointerInput.argtypes == [ctypes.c_void_p, ctypes.POINTER(ink.POINTER_TYPE_INFO), ctypes.c_uint32]
    assert raw.InjectSyntheticPointerInput.restype is ctypes.c_int32
    pointer.close()
    assert audit.summary()["all_devices_destroyed"]


def test_controlled_probe_sequence_varies_pressure_tilt_then_releases_and_destroys():
    pointer, raw, audit = make_pointer()
    pumps, points = [], [(0.45, 0.5), (0.5, 0.5), (0.55, 0.5)]
    checked = []
    def pump(duration):
        pumps.append(duration)
        if duration == 200:
            assert raw.packets[-1][0]["flags"] & ink.UP
            assert not raw.destroyed  # Receiver can inspect final up before destroy.
        if duration == 100:
            assert raw.destroyed == [73]
    check.run_sequence(pointer, points, pump, checked.append)
    assert checked == [points[0], *points]
    assert pumps == [120, 120, 120, 120, 200, 100]
    assert [(packet[0]["pressure"], packet[0]["tilt_x"], packet[0]["tilt_y"])
            for packet in raw.packets[1:4]] == list(check.EXPECTED_NATIVE)
    assert raw.packets[1][0]["flags"] & ink.DOWN
    final = raw.packets[-1][0]
    assert final["flags"] & ink.UP
    assert not final["flags"] & (ink.INCONTACT | ink.INRANGE)
    assert final["pressure"] == 0
    assert raw.destroyed == [73] and pointer.closed and pointer.handle is None
    summary = audit.summary()
    assert summary["created"] == summary["destroyed"] == 1
    assert summary["handles_remaining"] == 0
    assert "73" not in json.dumps({key: value for key, value in summary.items() if key != "attempts"})


def test_partial_sequence_failure_still_attempts_up_and_destroys_device():
    # hover succeeds; first contact succeeds; its update fails. Teardown must
    # still release the existing contact, never retry the uncertain frame.
    pointer, raw, audit = make_pointer(fail_injections=(3,))
    with pytest.raises(PointerPermissionError):
        check.run_sequence(pointer, [(0.4, 0.5), (0.5, 0.5), (0.6, 0.5)], lambda _: None, lambda _: None)
    assert len(raw.packets) == 4
    assert raw.packets[-1][0]["flags"] & ink.UP
    assert raw.destroyed == [73] and audit.summary()["all_devices_destroyed"]


def test_final_up_refused_still_destroys_and_is_not_counted_as_injected():
    pointer, raw, audit = make_pointer(fail_injections=(5, 6))
    with pytest.raises(PointerPermissionError):
        check.run_sequence(pointer, [(0.4, 0.5), (0.5, 0.5), (0.6, 0.5)], lambda _: None, lambda _: None)
    summary = audit.summary()
    assert summary["all_devices_destroyed"] and raw.destroyed == [73]
    assert raw.packets[-1][0]["flags"] & ink.UP
    assert not summary["attempts"][-1]["accepted"]
    assert not check.validate_observations([], [], summary)["final_up_injected"]


def test_exception_in_event_pump_still_sends_final_up_and_destroys():
    pointer, raw, audit = make_pointer()
    calls = []
    def pump(duration):
        calls.append(duration)
        if len(calls) == 2:
            raise RuntimeError("deterministic receiver failure")
    with pytest.raises(RuntimeError, match="receiver failure"):
        check.run_sequence(pointer, [(0.4, 0.5), (0.5, 0.5), (0.6, 0.5)], pump, lambda _: None)
    assert raw.packets[-1][0]["flags"] & ink.UP
    assert calls[-1] == 100 and audit.summary()["all_devices_destroyed"]


def test_create_refused_has_no_handle_to_destroy():
    raw = FakeUser32(create_ok=False)
    audit = check.AuditedAPI(raw)
    with pytest.raises(ink.WindowsInkUnavailable, match="interactive desktop"):
        ink.WindowsInkPointer(ScreenRect(0, 0, 800, 600), api=audit)
    assert audit.summary()["created"] == audit.summary()["destroyed"] == 0
    assert not raw.packets and not raw.destroyed


def observation_fixtures():
    """Construct test-only observations, never returned from the runtime probe."""
    native, qt = [], []
    for index, (pressure, tilt_x, tilt_y) in enumerate(check.EXPECTED_NATIVE):
        native.append({"read_ok": True, "pointer_type": ink.PT_PEN,
                       "message": "WM_POINTERDOWN" if index == 0 else "WM_POINTERUPDATE",
                       "flags": ink.INCONTACT | (ink.DOWN if index == 0 else ink.UPDATE),
                       "pressure": pressure, "tilt_x": tilt_x, "tilt_y": tilt_y})
        qt.append({"type": "TabletPress" if index == 0 else "TabletMove", "spontaneous": True,
                   "pressure": pressure / 1024, "tilt_x": tilt_x, "tilt_y": tilt_y})
    native.append({"read_ok": True, "pointer_type": ink.PT_PEN, "message": "WM_POINTERUP",
                   "flags": ink.UP, "pressure": 0, "tilt_x": 0, "tilt_y": 0})
    qt.append({"type": "TabletRelease", "spontaneous": True, "pressure": 0, "tilt_x": 0, "tilt_y": 0})
    audit = {"created": 1, "destroyed": 1, "all_devices_destroyed": True,
             "attempts": [{"accepted": True, "events": [{"flags": ink.UP}]}]}
    return native, qt, audit


def test_os_acceptance_without_receiver_events_can_never_verify_pressure():
    _, _, audit = observation_fixtures()
    checks = check.validate_observations([], [], audit)
    assert checks["device_destroyed"] and checks["final_up_injected"]
    assert not checks["native_pen_down_received"] and not checks["qt_tablet_press_received"]
    assert not all(checks.values())


@pytest.mark.parametrize("damage", ["pressure", "tilt", "qt_missing", "qt_constructed", "up_missing", "up_pressure", "destroy_missing"])
def test_receiver_proof_rejects_pressure_tilt_up_and_cleanup_defects(damage):
    native, qt, audit = observation_fixtures()
    assert all(check.validate_observations(native, qt, audit).values())
    if damage == "pressure":
        native[1]["pressure"] = 511
    elif damage == "tilt":
        qt[2]["tilt_x"] = 34
    elif damage == "qt_missing":
        qt.clear()
    elif damage == "qt_constructed":
        for event in qt:
            event["spontaneous"] = False
    elif damage == "up_missing":
        native.pop()
    elif damage == "up_pressure":
        qt[-1]["pressure"] = 0.5
    elif damage == "destroy_missing":
        audit["destroyed"], audit["all_devices_destroyed"] = 0, False
    assert not all(check.validate_observations(native, qt, audit).values())


def test_message_receiver_only_queries_its_own_window_and_pointer_messages():
    api = type("FakeReceiver", (), {})()
    ids = []
    def read(pointer_id, destination):
        ids.append(pointer_id)
        pen = ctypes.cast(destination, ctypes.POINTER(ink.POINTER_PEN_INFO)).contents
        pen.pointerInfo.pointerType = ink.PT_PEN
        pen.pointerInfo.pointerId = pointer_id
        pen.pointerInfo.pointerFlags = ink.DOWN | ink.INCONTACT
        pen.pressure, pen.tiltX, pen.tiltY = 256, -30, 20
        return 1
    api.GetPointerPenInfo = read
    api.GetPointerDeviceRects = lambda *_: 0
    wrong_window = check.MSG(hwnd=999, message=check.WM_POINTERDOWN, wParam=7)
    other_message = check.MSG(hwnd=42, message=0x200, wParam=7)
    assert check.decode_pointer_message(wrong_window, api, 42) is None
    assert check.decode_pointer_message(other_message, api, 42) is None
    assert not ids
    message = check.MSG(hwnd=42, message=check.WM_POINTERDOWN, wParam=(0xABCD << 16) | 7)
    result = check.decode_pointer_message(message, api, 42)
    assert ids == [7]
    assert result["source"] == "native-window-message" and result["read_ok"]
    assert result["pressure"] == 256 and result["tilt_x"] == -30 and result["tilt_y"] == 20
    assert not result["device_rects_available"]


def test_pointer_info_refused_remains_failed_observation():
    api = type("FakeReceiver", (), {"GetPointerPenInfo": staticmethod(lambda *_: 0)})()
    result = check.decode_pointer_message(check.MSG(hwnd=42, message=check.WM_POINTERUP, wParam=1), api, 42)
    assert not result["read_ok"] and "pressure" not in result


def test_noninteractive_desktop_is_clear_unsupported_before_any_device():
    api = type("FakeDesktop", (), {})()
    api.GetProcessWindowStation = lambda: 21
    def read_flags(handle, kind, destination, size, needed):
        flags = ctypes.cast(destination, ctypes.POINTER(check.USEROBJECTFLAGS)).contents
        flags.dwFlags = 0
        return 1
    api.GetUserObjectInformationW = read_flags
    api.OpenInputDesktop = lambda *_: pytest.fail("Must not open input on a noninteractive window station")
    with pytest.raises(check.ProbeUnsupported) as raised:
        check.check_input_desktop(api)
    assert raised.value.code == "noninteractive_window_station"


def test_read_only_desktop_handle_is_closed_once():
    api = type("FakeDesktop", (), {})()
    api.GetProcessWindowStation = lambda: 21
    def read_flags(handle, kind, destination, size, needed):
        ctypes.cast(destination, ctypes.POINTER(check.USEROBJECTFLAGS)).contents.dwFlags = 1
        return 1
    api.GetUserObjectInformationW = read_flags
    opened, closed = [], []
    api.OpenInputDesktop = lambda *args: opened.append(args) or 31
    api.CloseDesktop = lambda handle: closed.append(handle) or 1
    check.check_input_desktop(api)
    assert opened == [(0, False, 1)] and closed == [31]


def test_cli_report_marks_linux_unsupported_and_require_verified_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check.sys, "platform", "linux")
    path = tmp_path / "evidence.json"
    assert check.main(["--run-isolated-ci", "--report", str(path)]) == 0
    report = json.loads(path.read_text())
    assert report["status"] == "unsupported" and not report["verified"]
    assert check.main(["--run-isolated-ci", "--require-verified"]) == 1
    capsys.readouterr()
