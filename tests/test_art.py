"""Art tests use generated fixtures and fake native APIs; never contact a tablet."""
import ctypes
import hashlib
import json
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from paperdesk.art import ArtError, ArtManager, SUPPORTED_FIRMWARE, XOCHITL_SHA256, find_program, validate_options
from paperdesk.art_runtime import ArtSession
from paperdesk.models import DeviceConfig
from paperdesk.native_pointer import CGRect, CGPoint, CGSize, INPUT, MacPointer, PointerPermissionError, ScreenRect, WindowsPointer
from paperdesk.vendor.art import transforms as t
from paperdesk.vendor.art.canvas import CanvasMirror, valid_frame_header


def manager(tmp_path, simulated=False, platform="linux"):
    result = ArtManager(DeviceConfig(state_dir=tmp_path / "state"), simulated=simulated)
    result.platform, result.home, result.environ = platform, tmp_path, {"PATH": ""}
    return result


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_simulated_art_starts_no_process_input_or_network(tmp_path, monkeypatch, platform):
    m = manager(tmp_path, True, platform)
    monkeypatch.setattr("subprocess.Popen", Mock(side_effect=AssertionError("no subprocess")))
    monkeypatch.setattr("paperdesk.art_runtime.ArtSession", Mock(side_effect=AssertionError("no native input")))
    m.transport = Mock()
    result = m.start("krita")
    assert result["ok"] and result["simulated"] and m.status()["active"]
    assert m.open("blender")["simulated"]
    assert m.install_plugin(str(tmp_path / "plugins"))["simulated"]
    assert not (tmp_path / "plugins").exists()
    assert m.stop()["active"] is False
    m.transport.run.assert_not_called()
    m.transport.stream.assert_not_called()
    json.dumps(m.applications())
    json.dumps(m.status())


@pytest.mark.parametrize("value", ["krita; touch /tmp/injected", "$(id)", "--help", "​blender"])
def test_application_name_cannot_inject_command(tmp_path, monkeypatch, value):
    m = manager(tmp_path)
    popen = Mock()
    monkeypatch.setattr("subprocess.Popen", popen)
    with pytest.raises(ArtError):
        m.open(value)
    with pytest.raises(ArtError):
        m.start(value)
    popen.assert_not_called()


def test_filename_is_one_absolute_argv_entry_without_shell(tmp_path, monkeypatch):
    m = manager(tmp_path)
    executable = tmp_path / "krita"
    executable.write_text("test fixture")
    executable.chmod(0o700)
    m.set_program_path("krita", str(executable))
    artwork = tmp_path / "--flag; $(touch PWNED).kra"
    artwork.write_bytes(b"fixture")
    child = Mock(pid=123)
    child.poll.return_value = None
    popen = Mock(return_value=child)
    monkeypatch.setattr("subprocess.Popen", popen)
    result = m.open("krita", str(artwork))
    assert result["pid"] == 123
    args, kwargs = popen.call_args
    assert args[0] == [str(executable), str(artwork.resolve())]
    assert kwargs["shell"] is False and kwargs["start_new_session"] is True
    m.stop()
    child.terminate.assert_not_called()  # Never closes unsaved artwork.
    child.kill.assert_not_called()


@pytest.mark.parametrize("platform,relative,application", [
    ("darwin", "Applications/Krita.app/Contents/MacOS/krita", "krita"),
    ("darwin", "Applications/Inkscape.app/Contents/MacOS/inkscape", "inkscape"),
    ("darwin", "Applications/Blender.app/Contents/MacOS/blender", "blender"),
    ("win32", "Programs/Krita (x64)/bin/krita.exe", "krita"),
    ("win32", "Programs/Inkscape/bin/inkscape.exe", "inkscape"),
    ("win32", "Programs/Blender Foundation/Blender 4.5/blender.exe", "blender"),
])
def test_native_program_locations(tmp_path, platform, relative, application):
    executable = tmp_path / relative
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    executable.chmod(0o700)
    result = find_program(application, platform=platform, home=tmp_path,
                          environ={"PATH": "", "ProgramFiles": str(tmp_path / "Programs")})
    assert result == [str(executable)]


def test_flatpak_discovery_only_reads_files(tmp_path, monkeypatch):
    flatpak = tmp_path / "flatpak"
    flatpak.write_bytes(b"fixture")
    flatpak.chmod(0o700)
    (tmp_path / "data" / "flatpak" / "app" / "org.kde.krita").mkdir(parents=True)
    monkeypatch.setattr("subprocess.run", Mock(side_effect=AssertionError("discovery must not run commands")))
    monkeypatch.setattr("paperdesk.art.shutil.which", lambda name, path=None: str(flatpak) if name == "flatpak" else None)
    assert find_program("krita", platform="linux", home=tmp_path, environ={"PATH": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data")}) == [str(flatpak), "run", "org.kde.krita"]


@pytest.mark.parametrize("options", [
    {"pressure": 0}, {"pressure": True}, {"curve": float("nan")}, {"tilt": 5},
    {"refresh": float("inf")}, {"rotation": "left;shutdown"}, {"aspect": "1:0"},
    {"aspect": "-1:-1"}, {"aspect": -1}, {"touch": "yes"}, {"unknown": "x"},
    {"screen_rect": [0, 0, 0, 100]}, {"open_path": []},
])
def test_art_options_fail_before_native_actions(options):
    with pytest.raises(ArtError):
        validate_options(options, "linux")


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_non_linux_pressure_limits_are_explicit(tmp_path, monkeypatch, platform):
    m = manager(tmp_path, platform=platform)
    monkeypatch.setattr("paperdesk.art.windows_ink_available", lambda: False)
    monkeypatch.setattr("paperdesk.art.mac_input_permission", lambda: False)
    caps = m.capabilities()
    assert caps["pointer_only"] and caps["pointer"]
    assert not caps["pressure"] and not caps["tilt"] and not caps["native_multitouch"]
    assert caps["canvas_ipc"] and not caps["display_verified"]
    assert validate_options({"display": True}, platform)["display"]
    with pytest.raises(ArtError, match="Pair and connect"):
        m.start(options={"display": True})


def test_plugin_install_is_explicit_and_preserves_existing_files(tmp_path):
    m = manager(tmp_path)
    target = tmp_path / "pykrita"
    m.plugin_install_plan(str(target))
    assert not target.exists()
    target.mkdir()
    (target / "other-plugin.py").write_text("preserve me")
    result = m.install_plugin(str(target))
    assert result["requires_restart"]
    assert (target / "rm_viewport" / "viewport.py").is_file()
    assert (target / "rm_viewport.desktop").is_file()
    assert (target / "other-plugin.py").read_text() == "preserve me"
    assert not list(target.rglob("*.pyc"))
    (target / "rm_viewport" / "viewport.py").write_text("original plugin customisation")
    with pytest.raises(ArtError, match="already exists"):
        m.install_plugin(str(target))
    result = m.install_plugin(str(target), replace=True)
    backup = Path(result["backup"])
    assert (backup / "rm_viewport" / "viewport.py").read_text() == "original plugin customisation"
    assert (target / "other-plugin.py").read_text() == "preserve me"


def test_plugin_install_refuses_symlinks(tmp_path):
    m = manager(tmp_path)
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ArtError, match="symbolic links"):
        m.install_plugin(str(link))
    assert not list(actual.iterdir())


class Function:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class WindowsAPI:
    def __init__(self):
        self.events = []
        self.GetSystemMetrics = Function(lambda index: {76: -1920, 77: 0, 78: 3840, 79: 1080}[index])
        self.SendInput = Function(self.send)

    def send(self, count, pointer, size):
        assert count == 1 and size == ctypes.sizeof(INPUT)
        event = ctypes.cast(pointer, ctypes.POINTER(INPUT)).contents
        self.events.append((event.mi.dx, event.mi.dy, event.mi.dwFlags))
        return 1


def test_windows_native_virtual_desktop_and_release():
    api = WindowsAPI()
    pointer = WindowsPointer(user32=api)
    pointer.send(1, 0, True)
    x, y, flags = api.events[-1]
    assert (x, y) == (65535, 0)
    assert flags & pointer.DOWN and flags & pointer.VIRTUALDESK and flags & pointer.ABSOLUTE
    pointer.close()
    assert api.events[-1][2] & pointer.UP
    count = len(api.events)
    pointer.close()
    assert len(api.events) == count
    assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_windows_failed_send_still_releases_uncertain_button():
    api = WindowsAPI()
    pointer = WindowsPointer(user32=api)
    api.SendInput = Function(lambda *_: 0)
    with pytest.raises(PointerPermissionError, match="privilege"):
        pointer.send(0.5, 0.5, True)
    api.SendInput = Function(api.send)
    pointer.close()
    assert api.events[-1][2] & pointer.UP


class MacAPI:
    def __init__(self, permission=True):
        self.events = []
        self.released = []
        self.CGPreflightPostEventAccess = Function(lambda: permission)
        self.CGMainDisplayID = Function(lambda: 1)
        self.CGDisplayBounds = Function(lambda _: CGRect(CGPoint(0, 0), CGSize(1440, 900)))
        self.CGEventCreateMouseEvent = Function(self.create)
        self.CGEventPost = Function(lambda tap, event: None)
        self.CFRelease = Function(lambda event: self.released.append(event))

    def create(self, source, kind, point, button):
        self.events.append((kind, point.x, point.y))
        return len(self.events)


def test_mac_native_mouse_up_and_event_release():
    api = MacAPI()
    pointer = MacPointer(graphics=api, foundation=api)
    pointer.send(1, 1, True)
    assert api.events[-1] == (1, 1439, 899)
    pointer.send(0.5, 0.5, True)
    assert api.events[-1][0] == 6
    pointer.close()
    assert api.events[-1][0] == 2
    assert len(api.released) == len(api.events)


def test_mac_checks_permission_without_prompt_or_input():
    api = MacAPI(permission=False)
    with pytest.raises(PointerPermissionError, match="Accessibility"):
        MacPointer(graphics=api, foundation=api)
    assert not api.events


def test_screen_coordinates_are_bounded():
    screen = ScreenRect(-1920, 0, 1920, 1080)
    assert screen.point(-1, 2) == (-1920, 1079)
    with pytest.raises(ValueError):
        screen.point(float("nan"), 0)


class FakeOutput:
    def __init__(self, *_):
        self.events = []
        self.release_count = 0
        self.closed = False

    def emit(self, events, touch=False):
        self.events.extend(events or [])

    def release(self):
        self.release_count += 1

    def close(self):
        self.release()
        self.closed = True


class BlockingStream:
    def __init__(self):
        self.closed = False
        self.first = True
        self.wake = threading.Event()

    def read(self, size):
        if self.first:
            self.first = False
            return b"".join(t.RECORD.pack(0, 0, *event) for event in [(t.EV_KEY, t.BTN_TOUCH, 1), (t.EV_SYN, 0, 0)])
        self.wake.wait(5)
        return b""

    def close(self):
        self.closed = True
        self.wake.set()


class SessionTransport:
    def __init__(self):
        self.streams = []
        self.scripts = []

    def stream(self, script):
        self.scripts.append(script)
        stream = BlockingStream()
        self.streams.append(stream)
        return stream


def test_stopping_stream_releases_input_grab_and_native_button(tmp_path, monkeypatch):
    monkeypatch.setattr("paperdesk.art_runtime.LinuxOutput", FakeOutput)
    transport = SessionTransport()
    options = validate_options({"touch": False}, "linux")
    session = ArtSession(transport, "linux", options, tmp_path / "no-socket")
    session.start()
    for _ in range(100):
        if session.counts["pen"]:
            break
        time.sleep(0.005)
    assert session.counts["pen"] == 1
    assert (t.EV_KEY, t.BTN_TOUCH, 1) in session.output.events
    session.stop()
    assert not session.status()["active"]
    assert session.output.closed and session.output.release_count
    assert all(stream.closed for stream in transport.streams)
    assert all(not worker.is_alive() for worker in session.threads)
    assert len(transport.scripts) == 1
    script = transport.scripts[0]
    assert script.startswith("exec /home/root/.local/lib/paperdesk/current/bin/pen-bridge g /dev/input/event2")
    assert "install" not in script and "systemctl" not in script and "kill" not in script


def test_pen_stream_never_reconnects_implicitly(tmp_path, monkeypatch):
    monkeypatch.setattr("paperdesk.art_runtime.LinuxOutput", FakeOutput)
    transport = SessionTransport()
    session = ArtSession(transport, "linux", validate_options({"touch": False}, "linux"), tmp_path / "no-socket")
    session.start()
    transport.streams[0].close()
    for _ in range(100):
        if session.stop_event.is_set():
            break
        time.sleep(0.005)
    assert session.stop_event.is_set()
    session.stop()
    assert len(transport.scripts) == 1 and session.output.closed


def test_tablet_probe_fails_closed_without_starting_stream(tmp_path):
    m = manager(tmp_path, platform="darwin")
    transport = Mock()
    transport.run.return_value = "firmware=3.99.0\narch=aarch64\nxochitl=unknown\n"
    m.transport = transport
    with pytest.raises(ArtError, match="unsupported"):
        m.start()
    transport.stream.assert_not_called()
    assert "systemctl" not in transport.run.call_args.args[0]


def test_missing_helper_requires_wizard_without_implicit_deploy(tmp_path):
    m = manager(tmp_path, platform="darwin")
    transport = Mock()
    transport.run.return_value = f"model=reMarkable Paper Pro\nfirmware={SUPPORTED_FIRMWARE}\narch=aarch64\nxochitl={XOCHITL_SHA256}\nhelper=\npen_node=yes\n"
    m.transport = transport
    with pytest.raises(ArtError, match="setup wizard"):
        m.start()
    transport.write_bytes.assert_not_called()
    transport.stream.assert_not_called()


@pytest.mark.parametrize("model", ["", "reMarkable Paper Pro Move", "Unknown tablet"])
def test_art_refuses_other_models_even_with_matching_firmware_and_binary(tmp_path, model):
    m = manager(tmp_path, platform="darwin")
    m.transport = Mock()
    m.transport.run.return_value = f"model={model}\nfirmware={SUPPORTED_FIRMWARE}\narch=aarch64\nxochitl={XOCHITL_SHA256}\n"
    with pytest.raises(ArtError, match="unsupported"):
        m.start()
    m.transport.stream.assert_not_called()


def test_canvas_protocol_is_app_owned_and_has_no_installer():
    mirror = CanvasMirror.__new__(CanvasMirror)
    mirror.feed = None
    mirror.have = mirror.zoned = None
    mirror.say = Mock()
    process = Mock()
    process.poll.return_value = None
    mirror.start_stream = Mock(return_value=process)
    assert mirror.tablet("hb")
    script = mirror.start_stream.call_args.args[0]
    assert "/tmp/paperdesk-viewport" in script and "rm-viewport" not in script
    assert "systemctl" not in script and "install.sh" not in script and "kill " not in script
    process.stdin.write.assert_called_once_with(b"hb 0\n")


def test_canvas_header_rejects_bad_geometry():
    assert valid_frame_header({"rect": [0, 0, 100, 100], "screen": [100, 100], "hash": "fixture"})
    for head in [{}, {"rect": [0, 0, -1, 10], "screen": [100, 100], "hash": "x"},
                 {"rect": [0, 0, 10, 10], "screen": [0, 100], "hash": "x"},
                 {"rect": [float("nan"), 0, 10, 10], "screen": [100, 100], "hash": "x"}]:
        assert not valid_frame_header(head)


def test_pressure_transform_preserves_zero_and_clamps():
    curve = t.pressure_curve(0.5, 0.8, 1)
    assert curve(0) == 0 and curve(4096, 90) == 4096
    assert 0 < curve(1) < 4096
    assert curve(100, 75) >= curve(100, 0)


def test_linux_pen_tilt_rotation_and_release_frames():
    frames = t.Frames(t.Mapper("left", None))
    frames.feed(t.EV_ABS, t.ABS_X, t.X_MAX)
    frames.feed(t.EV_ABS, t.ABS_Y, t.Y_MAX)
    frames.feed(t.EV_ABS, t.ABS_TILT_X, 3000)
    frames.feed(t.EV_ABS, t.ABS_TILT_Y, -2000)
    events = frames.feed(t.EV_SYN, 0, 0)
    assert (t.EV_ABS, t.ABS_X, t.Y_MAX) in events
    assert (t.EV_ABS, t.ABS_Y, 0) in events
    assert (t.EV_ABS, t.ABS_TILT_X, -20) in events
    assert (t.EV_ABS, t.ABS_TILT_Y, -30) in events


class InkAPI(WindowsAPI):
    def __init__(self, fail=False):
        super().__init__()
        self.ink_events = []
        self.destroyed = []
        self.created = []
        self.fail = fail
        self.CreateSyntheticPointerDevice = Function(self.create_device)
        self.InjectSyntheticPointerInput = Function(self.inject)
        self.DestroySyntheticPointerDevice = Function(lambda handle: self.destroyed.append(handle))

    def create_device(self, kind, count, feedback):
        self.created.append((kind, count, feedback))
        return len(self.created) + 10

    def inject(self, handle, pointer, count):
        from paperdesk.windows_ink import POINTER_TYPE_INFO
        events = ctypes.cast(pointer, ctypes.POINTER(POINTER_TYPE_INFO))
        packet = []
        for index in range(count):
            item = events[index]
            if item.type == 3:
                pen = item.penInfo
                packet.append({"type": item.type, "flags": pen.pointerInfo.pointerFlags,
                               "x": pen.pointerInfo.ptPixelLocation.x, "y": pen.pointerInfo.ptPixelLocation.y,
                               "pressure": pen.pressure, "tilt": (pen.tiltX, pen.tiltY),
                               "pen_flags": pen.penFlags, "mask": pen.penMask})
            else:
                info = item.touchInfo.pointerInfo
                packet.append({"type": item.type, "flags": info.pointerFlags, "id": info.pointerId,
                               "x": info.ptPixelLocation.x, "y": info.ptPixelLocation.y})
        self.ink_events.append(packet)
        return not self.fail


def test_windows_ink_structs_match_win64_abi():
    from paperdesk.windows_ink import POINTER_INFO, POINTER_PEN_INFO, POINTER_TOUCH_INFO, POINTER_TYPE_INFO
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert [ctypes.sizeof(kind) for kind in (POINTER_INFO, POINTER_PEN_INFO, POINTER_TOUCH_INFO, POINTER_TYPE_INFO)] == [96, 120, 144, 152]
        assert POINTER_INFO.ptPixelLocation.offset == 32
        assert POINTER_INFO.PerformanceCount.offset == 80
        assert POINTER_TYPE_INFO.data.offset == 8


def test_windows_ink_pressure_tilt_eraser_and_final_up():
    from paperdesk.windows_ink import WindowsInkPointer, DOWN, UP, INRANGE, INCONTACT, PEN_ERASER, PEN_INVERTED
    api = InkAPI()
    pointer = WindowsInkPointer(api=api)
    pointer.send(0.5, 0.5, True, pressure=2048, tilt_x=-45, tilt_y=75, eraser=True)
    event = api.ink_events[-1][0]
    assert event["type"] == 3 and event["pressure"] == 512 and event["tilt"] == (-45, 75)
    assert event["flags"] & DOWN and event["flags"] & INRANGE and event["flags"] & INCONTACT
    assert event["pen_flags"] == PEN_ERASER | PEN_INVERTED and event["mask"] == 13
    pointer.close()
    final = api.ink_events[-1][0]
    assert final["flags"] & UP and not final["flags"] & INRANGE and not final["flags"] & INCONTACT
    assert final["pressure"] == 0
    assert api.destroyed == [11]
    pointer.close()
    assert api.destroyed == [11]


def test_windows_ink_updates_and_hover_are_not_mouse_events():
    from paperdesk.windows_ink import WindowsInkPointer, DOWN, UPDATE, UP
    api = InkAPI()
    pointer = WindowsInkPointer(api=api)
    pointer.send(0, 0, False, near=True)
    assert api.ink_events[-1][0]["flags"] & UPDATE
    pointer.send(0, 0, True, pressure=9000, tilt_x=-200, tilt_y=200)
    assert api.ink_events[-1][0]["pressure"] == 1024
    assert api.ink_events[-1][0]["tilt"] == (-90, 90)
    pointer.send(0.1, 0.1, True, pressure=1000)
    assert api.ink_events[-1][0]["flags"] & UPDATE and not api.ink_events[-1][0]["flags"] & DOWN
    pointer.send(0.1, 0.1, False, near=True)
    assert api.ink_events[-1][0]["flags"] & UP
    pointer.close()
    assert not api.events  # SendInput was never used.


def test_windows_ink_always_destroys_after_release_rejection():
    from paperdesk.windows_ink import WindowsInkPointer
    api = InkAPI()
    pointer = WindowsInkPointer(api=api)
    pointer.send(0.5, 0.5, True, pressure=4096)
    api.fail = True
    with pytest.raises(PointerPermissionError):
        pointer.close()
    assert pointer.closed and api.destroyed == [11]


def test_windows_ink_export_detection_does_not_create_or_inject():
    from paperdesk.windows_ink import windows_ink_available
    api = InkAPI()
    assert windows_ink_available(api)
    assert not api.created and not api.ink_events
    assert not windows_ink_available(WindowsAPI())


def test_windows_ink_touch_transitions_and_all_contacts_up():
    from paperdesk.windows_ink import WindowsInkTouch, DOWN, UPDATE, UP
    api = InkAPI()
    touch = WindowsInkTouch(api=api)
    touch.send({0: (0.2, 0.2), 1: (0.8, 0.2)})
    assert len(api.ink_events[-1]) == 2 and all(event["flags"] & DOWN for event in api.ink_events[-1])
    touch.send({0: (0.3, 0.3)})
    assert api.ink_events[-1][0]["flags"] & UPDATE and api.ink_events[-1][1]["flags"] & UP
    touch.close()
    assert all(event["flags"] & UP for event in api.ink_events[-1])
    assert api.destroyed == [11]


def test_windows_ink_capabilities_are_distinct_from_mouse(tmp_path, monkeypatch):
    m = manager(tmp_path, platform="win32")
    monkeypatch.setattr("paperdesk.art.windows_ink_available", lambda: True)
    caps = m.capabilities()
    assert caps["pressure"] and caps["tilt"] and caps["multitouch"] and not caps["pointer_only"]
    m._session = Mock(output=Mock(backend="native-mouse"))
    caps = m.capabilities()
    assert not caps["pressure"] and caps["pointer_only"]


def test_windows_runtime_forwards_full_pen_and_native_touch(tmp_path, monkeypatch):
    from paperdesk.art_runtime import WindowsInkOutput
    from paperdesk.windows_ink import WindowsInkPointer, WindowsInkTouch, UP
    api = InkAPI()
    monkeypatch.setattr("paperdesk.windows_ink.WindowsInkPointer", lambda screen=None: WindowsInkPointer(screen, api=api))
    monkeypatch.setattr("paperdesk.windows_ink.WindowsInkTouch", lambda screen=None: WindowsInkTouch(screen, api=api))
    mapper = t.Mapper("portrait", None)
    output = WindowsInkOutput(mapper, mapper, True)
    output.emit([(t.EV_KEY, t.BTN_TOOL_PEN, 1), (t.EV_ABS, t.ABS_X, t.X_MAX // 2),
                 (t.EV_ABS, t.ABS_Y, t.Y_MAX // 2), (t.EV_ABS, t.ABS_PRESSURE, 4096),
                 (t.EV_ABS, t.ABS_TILT_X, 30), (t.EV_KEY, t.BTN_TOUCH, 1)])
    assert api.ink_events[-1][0]["pressure"] == 1024 and api.ink_events[-1][0]["tilt"] == (30, 0)
    output.emit([(t.EV_ABS, t.ABS_MT_SLOT, 0), (t.EV_ABS, t.ABS_MT_TRACKING_ID, 1),
                 (t.EV_ABS, t.ABS_MT_POSITION_X, 500), (t.EV_ABS, t.ABS_MT_POSITION_Y, 700)], touch=True)
    assert api.ink_events[-1][0]["type"] == 2
    output.close()
    assert api.ink_events[-1][0]["flags"] & UP and len(api.destroyed) == 2


class TabletMacAPI(MacAPI):
    def __init__(self):
        super().__init__()
        self.packets = []
        self.objects = {}
        self.CGEventCreate = Function(lambda _: self.new(0, 0, 0))
        self.CGEventCreateMouseEvent = Function(lambda _, kind, point, button: self.new(kind, point.x, point.y))
        self.CGEventSetType = Function(lambda event, kind: self.objects[event].update(type=kind))
        self.CGEventSetLocation = Function(lambda event, point: self.objects[event].update(x=point.x, y=point.y))
        self.CGEventSetIntegerValueField = Function(lambda event, field, value: self.objects[event]["integer"].update({field: value}))
        self.CGEventSetDoubleValueField = Function(lambda event, field, value: self.objects[event]["double"].update({field: value}))
        self.CGEventPost = Function(lambda tap, event: self.packets.append(self.objects[event]))

    def new(self, kind, x, y):
        index = len(self.objects) + 1
        self.objects[index] = {"type": kind, "x": x, "y": y, "integer": {}, "double": {}}
        return index


def test_experimental_quartz_pen_fields_and_proximity():
    from paperdesk.quartz_pen import MacTabletPointer, MOUSE_SUBTYPE, TABLET_PRESSURE, TABLET_TILT_X, TABLET_TILT_Y, TABLET_DEVICE_ID, PROX_DEVICE_ID, PROX_ENTER, PROX_POINTER_TYPE
    api = TabletMacAPI()
    pointer = MacTabletPointer(graphics=api, foundation=api)
    pointer.send(0.5, 0.5, True, pressure=2048, tilt_x=30, tilt_y=-20)
    proximity, point = api.packets[-2:]
    assert proximity["type"] == 24 and proximity["integer"][PROX_ENTER] == 1
    assert proximity["integer"][PROX_POINTER_TYPE] == 1
    assert point["type"] == 1 and point["integer"][MOUSE_SUBTYPE] == 1
    assert point["integer"][TABLET_DEVICE_ID] == proximity["integer"][PROX_DEVICE_ID]
    assert point["double"][TABLET_PRESSURE] == 0.5
    assert point["double"][TABLET_TILT_X] == 0.5
    assert point["double"][TABLET_TILT_Y] == pytest.approx(1 / 3)
    pointer.close()
    up, leave = api.packets[-2:]
    assert up["type"] == 2 and up["double"][TABLET_PRESSURE] == 0
    assert leave["type"] == 24 and leave["integer"][PROX_ENTER] == 0
    assert len(api.released) == len(api.objects)


def test_quartz_eraser_changes_tool_with_balanced_proximity():
    from paperdesk.quartz_pen import MacTabletPointer, PROX_POINTER_TYPE, PROX_ENTER
    api = TabletMacAPI()
    pointer = MacTabletPointer(graphics=api, foundation=api)
    pointer.send(0.5, 0.5, True, pressure=1000)
    pointer.send(0.5, 0.5, True, pressure=1000, eraser=True)
    proximity = [event for event in api.packets if event["type"] == 24]
    assert [event["integer"][PROX_ENTER] for event in proximity] == [1, 0, 1]
    assert proximity[-1]["integer"][PROX_POINTER_TYPE] == 3
    pointer.close()


def test_mac_pen_explicitly_experimental_and_not_default(tmp_path):
    m = manager(tmp_path, platform="darwin")
    assert m.capabilities()["experimental_pressure"] and not m.capabilities()["pressure"]
    assert validate_options(None, "darwin")["mode"] == "auto"
    assert validate_options({"input_mode": "pen"}, "darwin")["mode"] == "pen"
    assert validate_options({"input_mode": "mouse"}, "darwin")["mode"] == "mouse"
