"""Lifecycle regressions use disposable demos and local gated service doubles."""
import os
import threading
import time
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPointF, QSettings, Qt
from PySide6.QtGui import QFocusEvent, QGuiApplication, QImage, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from paperdesk.backend import SuiteService
from paperdesk.models import DeviceConfig
from paperdesk.transport import DeviceError, UnknownMutation
from paperdesk.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(["paperdesk-lifecycle-tests"])


def pump(app, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            app.processEvents()
            return
        time.sleep(.002)
    raise AssertionError("Local lifecycle fixture did not settle")


@pytest.fixture
def make_window(app, tmp_path):
    windows = []
    def make(service):
        settings = QSettings(str(tmp_path / f"settings-{len(windows)}.ini"), QSettings.IniFormat)
        window = MainWindow(service=service, demo=True, settings=settings)
        windows.append(window)
        window.show()
        pump(app, lambda: not window.runner.busy)
        return window
    yield make
    for window in windows:
        release = getattr(window.service, "release", None)
        if release is not None:
            release.set()
        window.close()
        pump(app, lambda: not window.runner.running)
        window.deleteLater()
    app.processEvents()


class GatedInput:
    """No SSH, native input, application launch, or filesystem mutations."""
    simulated = True
    def __init__(self, await_release=False):
        self.config = DeviceConfig(host="simulation")
        self.connected = False
        self.art_running = False
        self.cancel = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.await_release = await_release
        self.calls = []
        self.begins = 0

    def begin_job(self):
        self.begins += 1
        self.cancel.clear()

    def connect(self):
        self.connected = True
        return self.status()

    def status(self):
        return {"connected": self.connected, "simulated": True, "model": "paper-pro",
                "capabilities": {"keyboard": True, "experimental-ink": True},
                "active_document": {}, "art": {"active": self.art_running}}

    def documents(self):
        return []

    def cancel_current(self):
        self.calls.append(("cancel",))
        self.cancel.set()
        return {"ok": True}

    def type_text(self, text):
        self.calls.append(("type", text))
        self.entered.set()
        assert self.cancel.wait(3), "The GUI did not signal cancellation"
        if self.await_release:
            assert self.release.wait(3), "The test did not release its gated operation"
        raise UnknownMutation("Synthetic input stopped after partial input; no replay")

    def send_key(self, key):
        self.calls.append(("key", key))
        return {"ok": True}

    def stop_art(self):
        self.calls.append(("stop_art",))
        self.art_running = False
        return {"ok": True}

    def disconnect(self):
        self.calls.append(("disconnect",))
        self.connected = False


class PointerFixture(GatedInput):
    def __init__(self, hold_down=False, fail_up=False):
        super().__init__()
        self.hold_down, self.fail_up = hold_down, fail_up
        self.contact = False

    def status(self):
        result = super().status()
        result["capabilities"].update({"touch": True, "direct-paste": True})
        return result

    def type_text(self, text):
        self.calls.append(("type", text))
        return {"ok": True}

    def paste_image(self, path):
        assert not QImage(path).isNull()
        self.calls.append(("image",))
        return {"ok": True}

    def touch(self, phase, x, y):
        assert 0 <= x <= 1 and 0 <= y <= 1
        self.calls.append(("touch", phase, x, y))
        if phase == "down":
            self.contact = True
            self.entered.set()
            if self.hold_down:
                assert self.release.wait(3)
        elif phase == "up":
            if self.fail_up:
                raise UnknownMutation("Synthetic release receipt lost; no replay")
            self.contact = False
        return {"ok": True}

    def cancel_current(self):
        self.contact = False  # models EOF releasing the helper's kernel device
        return super().cancel_current()


def mouse_event(kind, x, y):
    button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button, buttons, Qt.KeyboardModifier.NoModifier)


def test_real_demo_setup_reconnect_never_uses_pairing_api(app, make_window, tmp_path):
    service = SuiteService.demo(tmp_path / "tablet")
    # Guard the physical API itself, instead of replacing sockets. The real
    # demo's connect/status/document operations still run unchanged.
    service.probe_host_key = Mock(side_effect=AssertionError("Simulation must never probe SSH"))
    service.trust_host_key = Mock(side_effect=AssertionError("Simulation must never store SSH trust"))
    window = make_window(service)
    window.disconnect_tablet()
    pump(app, lambda: not window.runner.busy)
    window.pages["setup"].connect_panel.submit()
    pump(app, lambda: not window.runner.busy)
    assert window.connected and window.simulated
    service.probe_host_key.assert_not_called()
    service.trust_host_key.assert_not_called()


def test_status_failure_releases_backend_art_even_when_ui_marks_lost(app, make_window, tmp_path):
    service = SuiteService.demo(tmp_path / "tablet")
    service.install(["art-display"])
    window = make_window(service)
    service.start_art("krita")
    window.art_state = {"active": True, "application": "krita"}
    service.status = Mock(side_effect=DeviceError("Synthetic status read failed"))
    window.refresh_status()
    pump(app, lambda: not window.runner.busy)
    assert not window.connected and not service._connected
    assert not service.art.status()["active"]


def test_disconnect_cleanup_survives_cancelled_mutation_without_reset(app, make_window):
    service = GatedInput()
    window = make_window(service)
    window.run("type", lambda s: s.type_text("synthetic"), label="Type fixture", mutation=True)
    assert service.entered.wait(2)
    begins = service.begins
    assert window.disconnect_tablet()
    pump(app, lambda: not window.runner.busy)
    assert service.begins == begins, "Cleanup must not reset the pending stop signal"
    assert service.calls[-2:] == [("stop_art",), ("disconnect",)]
    assert not service.connected and not window.connected
    assert sum(call[0] == "type" for call in service.calls) == 1
    assert window.uncertain is not None


def test_stop_during_job_begin_cannot_be_erased_by_cancellation_reset(app, make_window):
    service = GatedInput()
    window = make_window(service)
    beginning = threading.Event()
    release_begin = threading.Event()
    original_begin = service.begin_job
    def delayed_begin():
        beginning.set()
        assert release_begin.wait(3)
        original_begin()
    service.begin_job = delayed_begin
    window.run("type", lambda s: s.type_text("must not be sent"), label="Type fixture", mutation=True)
    assert beginning.wait(2)
    window.disconnect_tablet()
    release_begin.set()
    pump(app, lambda: not window.runner.busy)
    assert not any(call[0] == "type" for call in service.calls)
    assert not service.connected
    assert service.calls[-2:] == [("stop_art",), ("disconnect",)]


@pytest.mark.parametrize("focus_loss", [False, True])
def test_live_stop_drops_captured_keys_and_preserves_clicked_keys(app, make_window, focus_loss):
    service = GatedInput()
    window = make_window(service)
    page = window.pages["type"]
    page.live.start()
    page.live.text.emit("first")
    assert service.entered.wait(2)
    page.live.text.emit("stale")
    page.feed("key", "enter")  # an independent on-screen action
    if focus_loss:
        page.live.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
    else:
        page.live.stop()
    pump(app, lambda: not window.runner.busy)
    assert not page.live.live and ("cancel",) in service.calls
    assert [call for call in service.calls if call[0] == "type"] == [("type", "first")]
    assert page._pending == [("key", "enter", False)]
    assert not any(call[0] == "key" for call in service.calls), "Partial input holds further changes"


def test_stopping_waiting_live_job_leaves_unrelated_work_and_clicked_key(app, make_window):
    service = GatedInput()
    window = make_window(service)
    reading = threading.Event()
    release_read = threading.Event()
    def read(_service):
        reading.set()
        assert release_read.wait(3)
        return {"ok": True}
    window.run("read-fixture", read, label="Read synthetic fixture")
    assert reading.wait(2)
    page = window.pages["type"]
    page.live.start()
    page.live.text.emit("must be dropped")
    page.feed("key", "enter")
    page.live.stop()
    assert ("cancel",) not in service.calls, "Stopping a waiting live job must not cancel an unrelated running call"
    release_read.set()
    pump(app, lambda: not window.runner.busy)
    assert ("key", "enter") in service.calls
    assert not any(call[0] == "type" for call in service.calls)


def test_close_remains_responsive_and_keeps_files_until_worker_and_cleanup_finish(app, make_window):
    service = GatedInput(await_release=True)
    window = make_window(service)
    service.art_running = True
    window.art_state = {"active": True, "application": "krita"}
    temporary = window.temp_dir()
    fixture = temporary / "pending-preview.txt"
    fixture.write_text("Synthetic pending file")
    window.run("type", lambda s: s.type_text("synthetic"), label="Type fixture", mutation=True)
    assert service.entered.wait(2)
    window.pages["type"].ink._cancel.clear()
    before = time.monotonic()
    window.close()
    assert time.monotonic() - before < .3
    assert window.isVisible() and window.runner.running
    assert fixture.exists(), "An active worker may still be reading its prepared file"
    assert window.pages["type"].ink._cancel.is_set()
    assert "Closing:" in window.status_text
    service.release.set()
    pump(app, lambda: not window.runner.running and not window.isVisible())
    assert not temporary.exists()
    assert not service.art_running and not service.connected
    assert service.calls[-2:] == [("stop_art",), ("disconnect",)]


def test_shutdown_cleans_owned_service_even_with_disconnected_display(app, make_window):
    service = GatedInput()
    window = make_window(service)
    service.art_running = True
    window._connected = False
    assert window.shutdown()
    assert not service.art_running and not service.connected
    assert service.calls[-2:] == [("stop_art",), ("disconnect",)]


@pytest.mark.parametrize("modifiers", [Qt.KeyboardModifier.ControlModifier,
                                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
                                       Qt.KeyboardModifier.MetaModifier])
def test_live_clipboard_shortcuts_type_host_text(app, make_window, modifiers):
    service = PointerFixture()
    window = make_window(service)
    page = window.pages["type"]
    QGuiApplication.clipboard().setText("Synthetic host clipboard")
    page.live.start()
    page.live.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_V, modifiers, "v"))
    pump(app, lambda: not window.runner.busy)
    assert ("type", "Synthetic host clipboard") in service.calls
    assert not any(call[0] == "key" for call in service.calls), "Desktop paste must not send the tablet's paste shortcut"
    QGuiApplication.clipboard().clear()


def test_live_clipboard_image_uses_native_paste_and_ctrl_p_uses_tablet_clipboard(app, make_window):
    service = PointerFixture()
    window = make_window(service)
    page = window.pages["type"]
    image = QImage(30, 40, QImage.Format.Format_ARGB32)
    image.fill(0x804080C0)
    QGuiApplication.clipboard().setImage(image)
    page.live.start()
    page.live.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier, "v"))
    pump(app, lambda: not window.runner.busy)
    assert ("image",) in service.calls
    assert not any(call[0] in {"type", "key"} for call in service.calls)
    page.live.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier, "p"))
    pump(app, lambda: not window.runner.busy)
    assert ("key", "paste") in service.calls
    QGuiApplication.clipboard().clear()


def test_drag_coalesces_moves_and_releases_once_outside_surface(app, make_window):
    service = PointerFixture(hold_down=True)
    window = make_window(service)
    page = window.pages["type"]
    surface = page.touch
    surface.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, 20, 30))
    assert service.entered.wait(2)
    for index in range(1000):
        surface.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, index % 230, index % 310))
        assert len(page._touch_pending) <= 1
    surface.mouseReleaseEvent(mouse_event(QEvent.Type.MouseButtonRelease, 500, -20))
    assert len(page._touch_pending) <= 2
    service.release.set()
    pump(app, lambda: not window.runner.busy)
    events = [call for call in service.calls if call[0] == "touch"]
    assert [call[1] for call in events] == ["down", "move", "up"]
    assert events[-1][2:] == (1.0, 0.0)
    assert not service.contact and not page._touch_outstanding


@pytest.mark.parametrize("trigger", ["focus", "capture", "escape", "stop", "disconnect", "close"])
def test_contact_releases_on_every_lifecycle_boundary(app, make_window, trigger):
    service = PointerFixture()
    window = make_window(service)
    page, surface = window.pages["type"], window.pages["type"].touch
    surface.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, 60, 80))
    pump(app, lambda: not window.runner.busy)
    assert service.contact
    if trigger == "focus":
        surface.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
    elif trigger == "capture":
        QApplication.sendEvent(surface, QEvent(QEvent.Type.UngrabMouse))
    elif trigger == "escape":
        surface.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    elif trigger == "stop":
        page.stop_forwarding()
    elif trigger == "disconnect":
        window.disconnect_tablet()
    else:
        window.close()
    pump(app, lambda: not window.runner.busy)
    assert not service.contact and not surface._contact
    releases = [call for call in service.calls if call[:2] == ("touch", "up")]
    assert len(releases) <= 1, "A release with uncertain delivery must not be replayed"


def test_lost_release_receipt_closes_helper_without_replaying_up(app, make_window):
    service = PointerFixture(fail_up=True)
    window = make_window(service)
    page = window.pages["type"]
    page.tap(.4, .6)
    pump(app, lambda: not window.runner.busy)
    assert not service.contact
    assert len([call for call in service.calls if call[:2] == ("touch", "up")]) == 1
    assert ("cancel",) in service.calls and window.uncertain is not None


def test_keyboard_touch_cursor_can_press_drag_and_release(app, make_window):
    service = PointerFixture()
    window = make_window(service)
    surface = window.pages["type"].touch
    def key(value):
        surface.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, value, Qt.KeyboardModifier.NoModifier))
        pump(app, lambda: not window.runner.busy)
    key(Qt.Key.Key_Right)  # position cursor without sending an input contact
    assert not any(call[0] == "touch" for call in service.calls)
    key(Qt.Key.Key_Return)
    assert service.contact
    key(Qt.Key.Key_Down)
    key(Qt.Key.Key_Return)
    events = [call for call in service.calls if call[0] == "touch"]
    assert [call[1] for call in events] == ["down", "move", "up"]
    assert not service.contact
