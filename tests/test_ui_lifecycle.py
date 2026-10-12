"""Lifecycle regressions use disposable demos and local gated service doubles."""
import os
import threading
import time
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QSettings
from PySide6.QtGui import QFocusEvent
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
