"""Offscreen tests for the Qt Widgets interface.

The window is driven through a recording test double of the SuiteService API,
so nothing here opens a network connection or touches a tablet.  Set
PAPERDESK_UI_SHOTS=<directory> to also save a screenshot of every page.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from paperdesk.models import DeviceConfig  # noqa: E402
from paperdesk.ui import MainWindow, create_window  # noqa: E402
from paperdesk.ui.services import ServiceProvider  # noqa: E402

FINGERPRINT = "SHA256:Zk3vQ0m1r8yJ4uXbT2sLw9aHnE6cPdF5gKoR7iVtUxA"
FEATURE_IDS = ("keyboard", "touch", "direct-paste", "art-display")


class UnknownMutationError(RuntimeError):
    """Stands in for the backend's 'outcome unknown, do not retry' exception."""


class HostTrustRequired(RuntimeError):
    """Stands in for the backend's refusal to connect to an unpaired or changed host."""


class FakeService:
    """Records every call and the thread it ran on; behaves like a small tablet."""

    def __init__(self, simulated: bool = True, supported: tuple[str, ...] = FEATURE_IDS):
        self.config = DeviceConfig()
        self.simulated = simulated
        self.supported = set(supported)
        self.connected = False
        self.installed: set[str] = set()
        self.calls: list[tuple] = []
        self.threads: set[int] = set()
        self.fail: dict[str, BaseException] = {}
        self.gates: dict[str, threading.Event] = {}
        self.entered: dict[str, threading.Event] = {}
        self.probe_extra: dict = {}
        self.trusted: set[str] = set()
        self.host_changed = False
        self.basic = {"screenshot": True, "pages": True, "refresh": True}
        self.typed = ""
        self.art_running = False
        self.drop_connection = False
        self.model = "Paper Pro"
        self.firmware = "3.28.0.172"
        self.programs = {"krita": "/usr/bin/krita", "inkscape": "/usr/bin/inkscape"}
        self.art_caps: dict = {}
        self._active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self.docs = [
            {"id": "d1", "name": "Field notes", "type": "notebook", "pages": 12,
             "modified": "2026-10-01 09:12", "folder": ""},
            {"id": "d2", "name": "Bridge survey", "type": "pdf", "pages": 48,
             "modified": "2026-09-22 17:40", "folder": "Work"},
            {"id": "d3", "name": "A Room with a View", "type": "epub", "pages": 210,
             "modified": "2026-08-03 21:05", "folder": "Reading"},
        ]

    def _call(self, name: str, *args):
        with self._lock:
            self.calls.append((name, *args))
            self.threads.add(threading.get_ident())
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            if name in self.entered:
                self.entered[name].set()
            if name in self.gates:
                assert self.gates[name].wait(10), "test gate was never released"
            if name in self.fail:
                raise self.fail[name]
        finally:
            with self._lock:
                self._active -= 1

    def count(self, name: str) -> int:
        return sum(1 for call in self.calls if call[0] == name)

    # -- pairing and connection ---------------------------------------
    def probe_host_key(self, config):
        self._call("probe_host_key", config.host)
        return {"host": config.host, "port": config.port, "fingerprint": FINGERPRINT,
                "key_type": "ssh-ed25519", "trusted": FINGERPRINT in self.trusted, **self.probe_extra}

    def trust_host_key(self, config, fingerprint):
        self._call("trust_host_key", fingerprint)
        self.trusted.add(fingerprint)
        return {"ok": True, "message": "Identity remembered."}

    def _status(self) -> dict:
        connected = self.connected and not self.drop_connection
        caps = {"files": True, "upload": True, "download": True, "export": True, "sync": True,
                **self.basic, **{feature: feature in self.installed for feature in FEATURE_IDS},
                "art_pressure": "art-display" in self.installed}
        return {"connected": connected, "simulated": self.simulated, "model": self.model,
                "firmware": self.firmware, "host": self.config.host,
                "transport": "simulator" if self.simulated else "ssh", "capabilities": caps,
                "active_document": {"id": "d1", "name": "Field notes", "page": 3, "pages": 12},
                "message": "Simulated tablet ready." if self.simulated else "Tablet ready."}

    def connect(self):
        self._call("connect")
        if self.host_changed:
            raise HostTrustRequired(f"The tablet host key changed. Observed {FINGERPRINT}. "
                                    "Verify it on the tablet before pairing again.")
        if not self.simulated and FINGERPRINT not in self.trusted:
            raise HostTrustRequired("This tablet is not paired. Observe its SSH fingerprint and "
                                    "confirm trust before connecting.")
        self.connected = True
        return self._status()

    def disconnect(self):
        self._call("disconnect")
        self.connected = False

    def status(self):
        self._call("status")
        return self._status()

    # -- documents ------------------------------------------------------
    def documents(self):
        self._call("documents")
        return [dict(doc) for doc in self.docs]

    def upload(self, path, folder=""):
        self._call("upload", path, folder)
        self.docs.append({"id": f"u{len(self.docs)}", "name": Path(path).stem, "type": "pdf",
                          "pages": 1, "modified": "2026-10-12 08:00", "folder": folder})
        return {"ok": True, "message": f"{Path(path).name} is on the tablet.", "id": self.docs[-1]["id"]}

    def download(self, document_id, destination):
        self._call("download", document_id, destination)
        Path(destination).write_bytes(b"%PDF-1.4 test")
        return {"ok": True, "message": "Saved.", "path": destination}

    def export_current(self, destination):
        self._call("export_current", destination)
        Path(destination).write_bytes(b"%PDF-1.4 test")
        return {"ok": True, "message": "Exported 12 pages.", "path": destination}

    def screenshot(self, destination):
        self._call("screenshot", destination)
        image = QImage(60, 80, QImage.Format.Format_RGB32)
        image.fill(QColor("#F4F1E8"))
        image.save(destination, "PNG")
        return {"ok": True, "message": "Captured 60 × 80.", "path": destination}

    def refresh(self):
        self._call("refresh")
        return {"ok": True, "message": "Library reloaded."}

    def sync(self, folder):
        self._call("sync", folder)
        return {"ok": True, "message": "Folder is up to date.", "downloaded": 0, "uploaded": 0}

    # -- typing ---------------------------------------------------------
    def type_text(self, text):
        self._call("type_text", text)
        self.typed += text
        return {"ok": True, "message": f"Typed {len(text)} characters."}

    def send_key(self, key):
        self._call("send_key", key)
        return {"ok": True, "message": f"Pressed {key}."}

    def paste_image(self, path):
        self._call("paste_image", path)
        return {"ok": True, "message": "Image placed."}

    def add_page(self, template):
        self._call("add_page", template)
        return {"ok": True, "message": f"Added a {template} page."}

    def touch(self, phase, x, y):
        self._call("touch", phase, round(x, 2), round(y, 2))
        return {"ok": True, "message": "Touch sent."}

    # -- installer ------------------------------------------------------
    def installer_plan(self, features):
        self._call("installer_plan", tuple(features))
        blocked = [f for f in features if f not in self.supported]
        reasons = {f: "Available for the observed model and architecture." if f in self.supported
                   else "Requires the exact pinned firmware." for f in FEATURE_IDS}
        return {"device": {"model": "paper-pro", "firmware": "3.28.0.172"}, "features": list(features),
                "supported": {f: f in self.supported for f in FEATURE_IDS}, "compatibility": reasons,
                "compatible": not blocked,
                "unsupported": [{"feature": f, "reason": reasons[f]} for f in blocked],
                "changes": ["Stage checksum-verified files under /home/root/.local/lib/paperdesk."]}

    def install(self, features):
        self._call("install", tuple(features))
        self.installed.update(features)
        return {"ok": True, "message": "Installed.", "features": list(features)}

    def uninstall(self):
        self._call("uninstall")
        self.installed.clear()
        return {"ok": True, "message": "Removed."}

    def diagnostics(self):
        self._call("diagnostics")
        return {"simulated": self.simulated, "checks": [{"name": "transport", "ok": True}]}

    # -- art ------------------------------------------------------------
    def start_art(self, application="krita", options=None):
        self._call("start_art", application, dict(options or {}))
        self.art_running = True
        return {"ok": True, "active": True, "application": application, "message": "Tablet mode started.",
                "capabilities": {"pressure": True, "tilt": False, "pointer_only": False,
                                 "limits": "Tilt is not validated in this test double."}}

    def stop_art(self):
        self._call("stop_art")
        self.art_running = False
        return {"ok": True, "active": False, "message": "Tablet mode stopped."}

    def open_art(self, application, path=""):
        self._call("open_art", application, path)
        return {"ok": True, "message": f"Simulated launch of {application}."}

    def art_applications(self):
        self._call("art_applications")
        caps = {"launch": True, "pointer": True, "pressure": True, "tilt": True, "multitouch": True,
                "art_display": True, "pointer_only": False, "limits": "Test double limits.",
                **self.art_caps}
        return [{"id": app, "name": app.capitalize(), "installed": app in self.programs,
                 "executable": self.programs.get(app, ""), "capabilities": dict(caps),
                 "message": "Ready to launch" if app in self.programs else f"Install {app}."}
                for app in ("krita", "inkscape", "blender")]

    def set_art_program(self, application, path):
        self._call("set_art_program", application, path)
        self.programs[application] = path
        return {"ok": True, "message": "Application path selected."}

    def art_plugin_plan(self, destination=""):
        self._call("art_plugin_plan")
        return {"ok": True, "destination": "/home/tester/.local/share/krita/pykrita",
                "files": ["rm_viewport.desktop", "rm_viewport/"], "message": "Copy the Krita viewport plugin."}

    def install_art_plugin(self, destination="", replace=False):
        self._call("install_art_plugin", replace)
        return {"ok": True, "message": "Krita viewport plugin copied.", "backup": "/tmp/backup" if replace else ""}

    # -- stickers and experimental ink ----------------------------------
    def create_sticker(self, path, folder=""):
        self._call("create_sticker", path, folder)
        self.docs.append({"id": f"s{len(self.docs)}", "name": Path(path).stem, "type": "notebook",
                          "pages": 1, "modified": "2026-10-12 08:05", "folder": folder})
        return {"ok": True, "message": "Sticker notebook uploaded.", "id": self.docs[-1]["id"]}

    def probe_ink(self):
        self._call("probe_ink")
        return {"ok": True, "axis_ratio": 0.75, "message": "Select the pen and an empty region."}

    def stream_ink(self, strokes, cancelled=lambda: False, progress=lambda n, total: None):
        self._call("stream_ink", [list(map(tuple, stroke)) for stroke in strokes])
        total = sum(len(stroke) + 1 for stroke in strokes) + 1
        for sent in range(total):
            if cancelled():
                raise RuntimeError(f"Ink cancelled; partial marks may remain, no replay. {sent} records "
                                   "sent; inspect the tablet before any new drawing.")
            progress(sent + 1, total)
        return {"ok": True, "records": total, "message": "Ink events sent once. Inspect the marks."}

    # -- sandbox --------------------------------------------------------
    def simulate_reboot(self):
        self._call("simulate_reboot")
        self.installed -= {"direct-paste", "art-display"}
        return {"ok": True, "message": "Simulated tablet restarted."}

    def simulation_fault(self, name, enabled=True):
        self._call("simulation_fault", name, enabled)
        return {"ok": True, "message": f"{name} {'on' if enabled else 'off'}."}


class StubDialogs:
    """Replaces native dialogs so tests never block on a modal window."""

    def __init__(self):
        self.files: list[str] = []
        self.save_path = ""
        self.folder = ""
        self.confirmations: list[str] = []
        self.answer = True

    def open_file(self, *_args, **_kwargs):
        return self.files[0] if self.files else ""

    def open_files(self, *_args, **_kwargs):
        return list(self.files)

    def save_file(self, *_args, **_kwargs):
        return self.save_path

    def directory(self, *_args, **_kwargs):
        return self.folder

    def confirm(self, title, *_args, **_kwargs):
        self.confirmations.append(title)
        return self.answer

    def inform(self, *_args, **_kwargs):
        pass


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    root = tmp_path_factory.mktemp("qsettings")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(root))
    QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, str(root))
    instance = QApplication.instance() or QApplication(["paperdesk-tests"])
    yield instance


@pytest.fixture
def make(app, tmp_path):
    windows: list[MainWindow] = []

    def build(service=None, demo=False, **kwargs) -> MainWindow:
        settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
        window = MainWindow(service=service, demo=demo, settings=settings, **kwargs)
        window.dialogs = StubDialogs()
        windows.append(window)
        return window

    yield build
    for window in windows:
        for gate in getattr(window.service, "gates", {}).values():
            gate.set()
        window.close()
        assert not window.runner.running
        window.deleteLater()
    app.processEvents()


def settle(app, window, timeout: float = 8.0) -> None:
    """Process events until the operation queue has drained."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if not window.runner.busy:
            app.processEvents()
            if not window.runner.busy:
                return
        time.sleep(0.003)
    raise AssertionError(f"operations did not finish: {window.status_text}")


def demo_window(app, make, **service_args):
    service = FakeService(simulated=True, **service_args)
    window = make(service, demo=True)
    settle(app, window)
    return window, service


def live_window(app, make, password="correct horse"):
    service = FakeService(simulated=False)
    window = make(service)
    panel = window.pages["setup"].connect_panel
    panel.password.setText(password)
    return window, service, panel


# ----------------------------------------------------------------------
def test_exports_and_default_start_is_disconnected(app, make):
    service = FakeService(simulated=False)
    window = make(service)
    assert isinstance(window, QMainWindow) and callable(create_window)
    app.processEvents()
    assert window.connection_mode == "disconnected"
    assert service.calls == [], "the default window must not call the backend on its own"
    assert not window.sim_banner.isVisibleTo(window)
    assert window.pages["home"].title_label.text() == "No tablet connected"
    assert not window.pages["library"].upload_button.isEnabled()


def test_create_window_returns_a_main_window_without_connecting(app):
    window = create_window()
    try:
        assert isinstance(window, MainWindow)
        assert window.connection_mode in ("disconnected", "unavailable")
        if window.connection_mode == "unavailable":
            assert "backend" in window.status_text.lower()
    finally:
        window.close()
    assert not window.runner.running


def test_demo_reads_simulation_everywhere(app, make):
    window, service = demo_window(app, make)
    window.show()
    app.processEvents()
    assert window.connection_mode == "simulation" and window.connected
    assert window.sim_banner.isVisible() and "Simulation" in window.sim_banner.text.text()
    assert "Simulation" in window.windowTitle()
    assert window.pages["home"].title_label.text().startswith("Simulation")
    assert "Simulation" in window.chip_label.text()
    assert service.count("connect") == 1 and service.count("probe_host_key") == 0
    assert service.count("open_art") == 0 and service.count("start_art") == 0
    assert window.pages["library"].table.topLevelItemCount() == 3


def test_navigation_reaches_every_page_and_section(app, make):
    window, _ = demo_window(app, make)
    window.resize(900, 650)
    window.show()
    app.processEvents()
    assert window.minimumSizeHint().width() <= 900 and window.minimumSizeHint().height() <= 650
    for page_id in ("home", "library", "type", "art", "setup"):
        window.nav[page_id].click()
        app.processEvents()
        assert window.current_page == page_id and window.nav[page_id].isChecked()
        assert window.nav[page_id].accessibleName()
    assert window.pages["type"].sections.ids() == ["write", "keyboard", "clipboard", "pages", "touch",
                                                   "ink"]
    assert window.pages["setup"].sections.visible_ids() == ["developer", "connect", "features", "install",
                                                            "recovery", "diagnostics", "sandbox"]
    for page_id in ("type", "setup"):
        for section in window.pages[page_id].sections.ids():
            window.navigate(page_id, section)
            app.processEvents()
            assert window.pages[page_id].sections.current == section
    menus = [action.text().replace("&", "") for action in window.menuBar().actions()]
    assert menus == ["File", "Tablet", "View", "Help"]


def test_try_the_simulator_from_a_disconnected_window(app, make):
    live, sim = FakeService(simulated=False), FakeService(simulated=True)
    window = make(live, provider=ServiceProvider(live_service=live, demo_service=sim))
    assert window.connection_mode == "disconnected"
    window.pages["home"].sim_button.click()
    settle(app, window)
    assert window.connection_mode == "simulation" and window.connected
    assert live.calls == [], "starting the simulator must not touch the real-tablet service"
    window.leave_simulation()
    settle(app, window)
    assert window.connection_mode == "disconnected" and window.service is live
    assert sim.count("disconnect") == 1


def test_operation_completion_and_failure_are_reported(app, make, tmp_path):
    window, service = demo_window(app, make)
    library = window.pages["library"]
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.4")
    window.dialogs.files = [str(source)]
    library.upload()
    settle(app, window)
    assert service.count("upload") == 1
    assert library.table.topLevelItemCount() == 4, "the list is re-read after an upload"

    window.dialogs.save_path = str(tmp_path / "out.pdf")
    library.export_current()
    settle(app, window)
    assert window.status_text.startswith("Done: Export Field notes as PDF.")
    assert "Exported 12 pages." in window.status_text and (tmp_path / "out.pdf").exists()

    service.fail["screenshot"] = RuntimeError("The tablet is asleep. Wake it and try again.")
    window.pages["type"].copy_screenshot()
    settle(app, window)
    assert window.status_text.startswith("Failed: Capture the tablet screen.")
    assert "Wake it and try again." in window.status_text
    assert window.uncertain is None and window.connected


def test_backend_reported_failure_is_never_shown_as_done(app, make):
    window, service = demo_window(app, make)
    service.refresh = lambda: {"ok": False, "message": "The screen is busy."}
    assert window.redraw_tablet() is True
    settle(app, window)
    assert window.status_text.startswith("Not completed:") and "The screen is busy." in window.status_text


def test_unknown_mutation_is_not_retried_and_holds_changes(app, make, tmp_path):
    window, service = demo_window(app, make)
    source = tmp_path / "notes.pdf"
    source.write_bytes(b"%PDF-1.4")
    window.dialogs.files = [str(source)]
    service.fail["upload"] = UnknownMutationError("The link dropped mid-upload. Do not retry.")
    window.pages["library"].upload()
    settle(app, window)
    assert service.count("upload") == 1, "an uncertain mutation is never retried"
    assert window.uncertain is not None
    assert "Outcome unknown" in window.uncertain_banner.text.text()
    assert window.status_text.startswith("Outcome unknown")

    del service.fail["upload"]
    window.pages["library"].upload()
    window.redraw_tablet()
    settle(app, window)
    assert service.count("upload") == 1 and service.count("refresh") == 0, "changes stay on hold"
    assert window.status_text.startswith("Not started")
    before = service.count("status")
    window.check_after_uncertain()
    settle(app, window)
    assert service.count("status") == before + 1, "reading state is still allowed"

    window.dismiss_uncertain()
    window.pages["library"].upload()
    settle(app, window)
    assert service.count("upload") == 2 and window.uncertain is None


def test_operations_are_serialized_off_the_gui_thread_without_duplicates(app, make):
    window, service = demo_window(app, make)
    service.installed.add("keyboard")
    window.refresh_status()
    settle(app, window)
    page = window.pages["type"]
    service.gates["type_text"] = threading.Event()
    service.entered["type_text"] = threading.Event()
    page.text_edit.setPlainText("hello")
    page.send_text()
    assert service.entered["type_text"].wait(5)
    page.send_text()
    page.send_button.click()
    app.processEvents()
    assert window.status_text.startswith("Already in progress")
    page.add_page("grid")
    page.add_page("grid")
    assert window.runner.waiting == 1, "a second identical request is refused, not queued"
    assert service.count("add_page") == 0, "nothing runs while another operation is in flight"
    service.gates["type_text"].set()
    settle(app, window)
    assert service.count("type_text") == 1 and service.count("add_page") == 1
    assert service.max_active == 1
    assert threading.get_ident() not in service.threads and len(service.threads) == 1
    assert page.text_edit.toPlainText() == "", "sent text is cleared only after success"


def test_live_keys_keep_order_and_stop_on_failure(app, make):
    window, service = demo_window(app, make)
    service.installed.add("keyboard")
    window.refresh_status()
    settle(app, window)
    page = window.pages["type"]
    for kind, value in (("text", "a"), ("text", "b"), ("key", "enter"), ("text", "c")):
        page.feed(kind, value)
    settle(app, window)
    sent = [call for call in service.calls if call[0] in ("type_text", "send_key")]
    assert "".join(c[1] for c in sent if c[0] == "type_text") == "abc"
    assert [c[0] for c in sent][-2:] == ["send_key", "type_text"]

    service.fail["send_key"] = RuntimeError("Keyboard helper stopped.")
    calls_before = len(service.calls)
    for kind, value in (("key", "tab"), ("text", "lost"), ("key", "enter")):
        page.feed(kind, value)
    settle(app, window)
    after = [c for c in service.calls[calls_before:] if c[0] in ("type_text", "send_key")]
    assert after == [("send_key", "tab")], "keys behind a failed key are dropped, not sent"

    keyboard = page.osk
    seen: list[str] = []
    keyboard.text.connect(seen.append)
    keyboard.set_shift(True)
    keyboard._press("a", "A")
    keyboard._press("1", "!")
    assert seen == ["A", "1"] and not keyboard.shift, "shift applies to one key"


def test_pairing_shows_fingerprint_and_password_is_never_persisted(app, make, tmp_path):
    secret = "tr0ub4dor-&-3"
    window, service, panel = live_window(app, make, password=secret)
    panel.host.setText("10.11.99.1")
    window.request_connect()
    settle(app, window)
    assert [call[0] for call in service.calls] == ["connect", "probe_host_key"]
    assert window.connection_mode == "disconnected" and not service.connected, "no session before trust"
    assert window.current_page == "setup" and window.pages["setup"].sections.current == "connect"
    assert panel.fingerprint.text() == FINGERPRINT
    assert not panel.trust_button.isEnabled(), "trust needs a deliberate tick first"
    panel.trust_button.click()
    assert service.count("trust_host_key") == 0
    panel.trust_check.setChecked(True)
    panel.trust_button.click()
    settle(app, window)
    assert service.calls[2:4] == [("trust_host_key", FINGERPRINT), ("connect",)]
    assert window.connection_mode == "live" and service.config.password == secret
    assert not window.sim_banner.isVisibleTo(window)

    window.disconnect_tablet()
    settle(app, window)
    window.request_connect()
    settle(app, window)
    assert window.connection_mode == "live", "a tablet that is already trusted connects in one step"
    assert service.count("probe_host_key") == 1 and service.count("trust_host_key") == 1

    window.settings.sync()
    stored = (tmp_path / "settings.ini").read_text(encoding="utf-8")
    assert "10.11.99.1" in stored
    assert secret not in stored and "password" not in stored.lower()
    assert all("password" not in key.lower() for key in window.settings.allKeys())
    assert secret not in repr(service.config)
    assert all(secret not in text for _, _, text in window.log)
    assert panel.password.echoMode() == panel.password.EchoMode.Password


def test_changed_host_identity_refuses_to_connect(app, make):
    window, service, panel = live_window(app, make)
    service.host_changed = True
    panel.submit()
    settle(app, window)
    assert window.connection_mode == "disconnected"
    assert window.pairing["state"] == "changed" and window.pairing["fingerprint"] == FINGERPRINT
    assert service.count("probe_host_key") == 0
    assert panel.trust_button.isHidden() and panel.trust_check.isHidden()
    assert window.trust_and_connect() is False
    settle(app, window)
    assert service.count("trust_host_key") == 0 and service.count("connect") == 1
    assert "refuses" in panel.state_label.text()


def test_unsupported_capabilities_are_disabled_until_installed(app, make):
    window, service = demo_window(app, make, supported=("keyboard", "direct-paste", "art-display"))
    typing, art, setup = window.pages["type"], window.pages["art"], window.pages["setup"]
    typing.text_edit.setPlainText("hello")
    assert not typing.send_button.isEnabled() and not typing.osk.isEnabled()
    assert not typing.clip_image_button.isEnabled() and not typing.touch.isEnabled()
    assert not art.start_button.isEnabled()
    assert "not available" in typing.keyboard_notice.text()
    assert typing.shot_copy_button.isEnabled() and window.pages["library"].upload_button.isEnabled(), \
        "basic actions stay available"
    service.basic["screenshot"] = False
    window.refresh_status()
    settle(app, window)
    assert not typing.shot_copy_button.isEnabled() and "screenshots" in typing.shot_notice.text()
    service.basic["screenshot"] = True
    typing.send_text()
    art.start_mode()
    settle(app, window)
    assert service.count("type_text") == 0 and service.count("start_art") == 0

    setup.feature_checks["touch"].setChecked(True)
    setup.review_plan()
    settle(app, window)
    assert not setup.install_button.isEnabled(), "the installer's refusal blocks installation"
    assert "exact pinned firmware" in setup.plan_summary.text()
    assert setup.feature_states["touch"].text() == "Refused: Requires the exact pinned firmware."

    setup.feature_checks["touch"].setChecked(False)
    setup.feature_checks["keyboard"].setChecked(True)
    assert not setup.install_button.isEnabled(), "a changed selection needs a fresh plan"
    setup.install()
    assert service.count("install") == 0
    setup.review_plan()
    settle(app, window)
    assert setup.install_button.isEnabled()
    window.dialogs.answer = False
    setup.install_button.click()
    settle(app, window)
    assert service.count("install") == 0, "declining the confirmation installs nothing"
    window.dialogs.answer = True
    setup.install_button.click()
    settle(app, window)
    assert service.count("install") == 1 and window.dialogs.confirmations
    assert typing.send_button.isEnabled() and typing.osk.isEnabled()
    assert not typing.touch.isEnabled() and not art.start_button.isEnabled()

    setup.uninstall()
    settle(app, window)
    assert service.count("uninstall") == 1 and not typing.send_button.isEnabled()


def test_touch_surface_sends_one_down_and_one_up(app, make):
    window, service = demo_window(app, make)
    surface = window.pages["type"].touch
    assert not surface.isEnabled()
    service.installed.add("touch")
    window.refresh_status()
    settle(app, window)
    assert surface.isEnabled()
    surface._tap(0.25, 0.5)
    settle(app, window)
    assert [c for c in service.calls if c[0] == "touch"] == [("touch", "down", 0.25, 0.5),
                                                             ("touch", "up", 0.25, 0.5)]


def test_art_mode_reports_state_and_is_stopped_on_exit(app, make):
    window, service = demo_window(app, make)
    service.installed.update(("art-display",))
    window.refresh_status()
    settle(app, window)
    art = window.pages["art"]
    assert art.cap_labels["pressure"].text() == "Supported"
    assert art.cap_labels["tilt"].text() == "Not reported yet"
    art.choose("inkscape")
    art.rotation.setCurrentIndex(2)
    art.display_check.setChecked(True)
    art.start_button.click()
    settle(app, window)
    assert not art.display_check.isChecked(), "canvas display cannot be ticked outside Krita"
    assert service.calls[-1] == ("start_art", "inkscape", {
        "rotation": "left", "pressure": 0.5, "curve": 0.8, "tilt": 0.0, "touch": False})
    assert art.cap_labels["tilt"].text() == "Not available here", "the bridge's own report wins"
    assert window.art_state["active"] and window.art_banner.isVisibleTo(window)
    assert not art.start_button.isEnabled() and art.stop_button.isEnabled()
    window.close()
    assert not service.art_running, "closing the window stops tablet mode first"
    assert service.calls[-2:] == [("stop_art",), ("disconnect",)]


def test_connection_loss_is_reported_and_never_reconnects(app, make):
    window, service = demo_window(app, make)
    service.fail["documents"] = ConnectionError("The tablet stopped answering.")
    service.drop_connection = True
    window.reload_documents()
    settle(app, window)
    assert not window.connected and window.connection_lost
    assert window.lost_banner.isVisibleTo(window)
    assert window.pages["home"].title_label.text() == "Simulation — not started"
    assert service.count("connect") == 1 and service.count("documents") == 2
    assert not window.pages["library"].upload_button.isEnabled()


def test_worker_teardown_is_clean_even_with_work_in_flight(app, make):
    window, service = demo_window(app, make)
    service.gates["diagnostics"] = threading.Event()
    service.entered["diagnostics"] = threading.Event()
    window.pages["setup"].run_diagnostics()
    window.reload_documents()
    assert service.entered["diagnostics"].wait(5)
    threading.Timer(0.2, service.gates["diagnostics"].set).start()
    documents_before = service.count("documents")
    assert window.shutdown() is True
    assert not window.runner.running
    assert service.count("documents") == documents_before, "waiting work is dropped at shutdown"
    assert service.calls[-1] == ("disconnect",) and not service.connected
    assert window.run("status", lambda s: s.status(), label="late") is False
    assert window.shutdown() is True


def test_image_sticker_upload_and_setup_link_for_missing_extras(app, make, tmp_path):
    window, service = demo_window(app, make)
    typing = window.pages["type"]
    picture = tmp_path / "leaf.png"
    image = QImage(40, 30, QImage.Format.Format_RGB32)
    image.fill(QColor("#335533"))
    image.save(str(picture), "PNG")
    window.dialogs.files = [str(picture)]
    assert typing.sticker_button.isEnabled(), "stickers need no installed extras"
    typing.sticker_button.click()
    settle(app, window)
    assert [c for c in service.calls if c[0] == "create_sticker"] == [("create_sticker", str(picture), "")]
    assert "Sticker notebook uploaded." in window.status_text
    assert window.pages["library"].table.topLevelItemCount() == 4

    service.fail["create_sticker"] = RuntimeError("Install or restore the compatible keyboard feature first.")
    typing.upload_sticker()
    settle(app, window)
    assert window.status_text.startswith("Failed:")
    assert window.hint_button.isVisibleTo(window) and "Features" in window.hint_button.text()
    window.hint_button.click()
    assert window.current_page == "setup" and window.pages["setup"].sections.current == "features"

    window.disconnect_tablet()
    settle(app, window)
    assert not typing.sticker_button.isEnabled()


def _figure(tmp_path) -> str:
    path = tmp_path / "figure.png"
    image = QImage(64, 64, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    for x in range(16, 48):
        for y in range(20, 44):
            image.setPixelColor(x, y, QColor("black"))
    image.save(str(path), "PNG")
    return str(path)


def test_figure_as_ink_needs_preview_pen_check_and_inspected_mark(app, make, tmp_path):
    pytest.importorskip("PIL")
    window, service = demo_window(app, make)
    ink = window.pages["type"].ink
    assert not ink.prepare_button.isEnabled() and not ink.draw_button.isEnabled()
    window.dialogs.files = [_figure(tmp_path)]
    ink.choose_source()
    ink.prepare_button.click()
    settle(app, window)
    assert ink.preview_current() and ink.prepared["strokes"], "the preview is traced locally"
    assert ink.preview.pixmap() is not None and not ink.preview.pixmap().isNull()
    assert service.count("stream_ink") == 0 and service.count("probe_ink") == 0

    ink.draw()
    ink.draw_mark()
    settle(app, window)
    assert service.count("stream_ink") == 0, "no ink without a pen check and a calibration mark"
    assert not ink.mark_button.isEnabled() and not ink.confirm.isEnabled()

    ink.probe_button.click()
    settle(app, window)
    assert service.count("probe_ink") == 1 and ink.axis_ratio == 0.75
    assert ink.preview_current(), "the preview is redrawn with the measured pen shape"
    assert ink.mark_button.isEnabled() and not ink.draw_button.isEnabled()
    ink.mark_button.click()
    settle(app, window)
    marks = [c for c in service.calls if c[0] == "stream_ink"]
    assert marks == [("stream_ink", [[(1000, 1500), (1000, 1000), (1500, 1000)]])]
    assert ink.calibrated and not ink.draw_button.isEnabled(), "the mark must be inspected first"
    ink.draw()
    settle(app, window)
    assert service.count("stream_ink") == 1
    ink.confirm.setChecked(True)
    assert ink.draw_button.isEnabled()

    ink.edges[0].setValue(2000)
    assert not ink.calibrated and not ink.confirm.isChecked(), "moving the area voids the calibration"
    ink.edges[0].setValue(1000)
    ink.prepare()
    settle(app, window)
    ink.draw_mark()
    settle(app, window)
    ink.confirm.setChecked(True)
    window.dialogs.answer = False
    ink.draw_button.click()
    settle(app, window)
    assert service.count("stream_ink") == 2, "declining the confirmation draws nothing"
    window.dialogs.answer = True
    ink.draw_button.click()
    settle(app, window)
    assert service.count("stream_ink") == 3
    assert "Ink events sent once" in ink.outcome.text() and window.uncertain is None
    assert not ink.calibrated and not ink.draw_button.isEnabled(), "each drawing needs a new mark"


def test_stopped_ink_is_reported_as_partial_and_never_replayed(app, make, tmp_path):
    pytest.importorskip("PIL")
    window, service = demo_window(app, make)
    ink = window.pages["type"].ink
    window.dialogs.files = [_figure(tmp_path)]
    ink.choose_source()
    ink.prepare()
    settle(app, window)
    ink.probe()
    settle(app, window)
    ink.draw_mark()
    settle(app, window)
    ink.confirm.setChecked(True)
    service.gates["stream_ink"] = threading.Event()
    service.entered["stream_ink"] = threading.Event()
    ink.draw()
    assert service.entered["stream_ink"].wait(5)
    app.processEvents()
    assert ink.drawing and ink.cancel_button.isEnabled() and not ink.draw_button.isEnabled()
    ink.cancel_button.click()
    service.gates.pop("stream_ink").set()
    settle(app, window)
    assert service.count("stream_ink") == 2, "a stopped drawing is never sent again"
    assert not ink.drawing and "Stopped at your request" in ink.outcome.text()
    assert "stay on the page" in ink.outcome.text()
    assert window.uncertain is not None and "Outcome unknown" in window.uncertain_banner.text.text()
    ink.probe()
    settle(app, window)
    assert service.count("probe_ink") == 1, "tablet changes stay on hold after partial ink"


def test_art_applications_options_and_krita_plugin_steps(app, make, tmp_path):
    window, service = demo_window(app, make)
    service.simulated = False  # exercise the installed/not-installed wording
    window._simulated = False
    service.installed.add("art-display")
    window.refresh_status()
    settle(app, window)
    art = window.pages["art"]
    art.discover()
    settle(app, window)
    assert "is installed: /usr/bin/krita" in art.app_state.text()
    assert art.touch_check.isChecked() and art.touch_check.isEnabled(), "touch follows the bridge's report"
    art.choose("blender")
    assert "not installed" in art.app_state.text() and not art.open_button.isEnabled()
    program = tmp_path / "blender"
    program.write_text("#!/bin/sh\n")
    window.dialogs.files = [str(program)]
    art.program_button.click()
    settle(app, window)
    assert ("set_art_program", "blender", str(program)) in service.calls
    assert art.open_button.isEnabled() and "is installed" in art.app_state.text()

    art.choose("krita")
    assert not art.display_check.isEnabled(), "canvas display waits for the plugin steps"
    assert not art.plugin_button.isEnabled(), "the copy is reviewed before it is offered"
    art.plugin_plan_button.click()
    settle(app, window)
    assert "pykrita" in art.plugin_note.text() and service.count("install_art_plugin") == 0
    window.dialogs.answer = False
    art.plugin_button.click()
    settle(app, window)
    assert service.count("install_art_plugin") == 0, "the plugin is never copied silently"
    window.dialogs.answer = True
    service.fail["install_art_plugin"] = RuntimeError("A reMarkable Viewport plugin already exists.")
    art.plugin_button.click()
    settle(app, window)
    assert art.plugin_replace_button.isVisibleTo(art) or not art.plugin_replace_button.isHidden()
    del service.fail["install_art_plugin"]
    art.plugin_replace_button.click()
    settle(app, window)
    assert service.calls[-1] == ("install_art_plugin", True) or ("install_art_plugin", True) in service.calls
    assert "kept in /tmp/backup" in art.plugin_note.text()

    art.plugin_ready.setChecked(True)
    assert art.display_check.isEnabled()
    art.display_check.setChecked(True)
    art.tilt.setValue(150)
    art.eraser_check.setChecked(True)
    art.eraser.setValue(64)
    art.start_button.click()
    settle(app, window)
    started = [c for c in service.calls if c[0] == "start_art"][-1]
    assert started == ("start_art", "krita", {"rotation": "auto", "pressure": 0.5, "curve": 0.8,
                                              "tilt": 1.5, "touch": True, "eraser": 64, "display": True})
    art.stop_mode()
    settle(app, window)

    service.art_caps = {"pressure": False, "tilt": False, "multitouch": False, "art_display": False,
                        "pointer_only": True, "windows_ink": True}
    window.art_state = {"active": False, "application": ""}
    art.discover()
    settle(app, window)
    window.show()
    app.processEvents()
    assert art.cap_labels["pressure"].text() == "Not available here"
    assert not art.pressure.isEnabled() and not art.touch_check.isEnabled()
    assert "plain pointer" in art.limits_label.text() and art.ink_note.isVisibleTo(art)
    assert art.options()["touch"] is False and "display" not in art.options()


def test_sandbox_is_simulation_only_and_fresh_tablets_ignore_saved_settings(app, make):
    live = FakeService(simulated=False)
    sim = FakeService(simulated=True)
    made: list[tuple] = []

    def fresh(model, firmware):
        made.append((model, firmware))
        service = FakeService(simulated=True)
        service.model, service.firmware = model, firmware
        return service
    provider = ServiceProvider(live_service=live, demo_service=sim)
    provider.set_fresh_factory(fresh)
    window = make(live, provider=provider)
    setup = window.pages["setup"]
    sandbox = setup.sandbox
    assert "sandbox" not in setup.sections.visible_ids(), "the sandbox tab is hidden outside simulation"
    sandbox.reboot()
    sandbox.set_fault("offline", True)
    assert window.start_fresh_simulation("rm2") is False
    settle(app, window)
    assert live.calls == [] and made == []

    window.start_simulation()
    settle(app, window)
    assert "sandbox" in setup.sections.visible_ids()
    sim.installed.update(("keyboard", "direct-paste"))
    window.refresh_status()
    settle(app, window)
    sandbox.reboot_button.click()
    settle(app, window)
    assert sim.count("simulate_reboot") == 1
    assert window.supports("keyboard") and not window.supports("direct-paste"), \
        "after a restart the advanced extras need setup again"
    sandbox.fault_checks["upload-unknown"].click()
    settle(app, window)
    assert ("simulation_fault", "upload-unknown", True) in sim.calls
    assert "Upload loses its receipt" in sandbox.fault_note.text()

    sandbox.model.setCurrentIndex(1)
    sandbox.firmware.setText("3.20.0.92")
    sandbox.fresh_button.click()
    settle(app, window)
    assert made == [("rm2", "3.20.0.92")] and window.service is not sim and window.service is not live
    assert window.connection_mode == "simulation" and window.connected
    assert window.status["model"] == "rm2" and window.status["firmware"] == "3.20.0.92"
    assert sim.calls[-1] == ("disconnect",) and live.calls == []
    assert not sandbox.fault_checks["upload-unknown"].isChecked(), "a fresh tablet starts without faults"
    assert "simulated" in window.pages["setup"].firmware_label.text()
    assert "not evidence" in " ".join(lab.text() for lab in sandbox.findChildren(type(sandbox.notice)))

    window.leave_simulation()
    settle(app, window)
    assert window.service is live and "sandbox" not in setup.sections.visible_ids()


def test_shortcuts_and_sync_result_are_visible(app, make, tmp_path):
    window, service = demo_window(app, make)
    shortcuts = {action.shortcut().toString() for menu in window.menuBar().actions()
                 for action in menu.menu().actions()}
    assert {"Ctrl+G", "Ctrl+I", "Ctrl+U", "Ctrl+D", "Ctrl+E", "Ctrl+K", "F5", "F1"} <= shortcuts
    window.show_shortcuts()
    assert window._shortcut_dialog.isVisible()
    window._shortcut_dialog.close()
    library = window.pages["library"]
    library.sync_edit.setText(str(tmp_path))
    service.sync = lambda folder: {"ok": True, "message": "Exported 2 annotation copies.",
                                   "paths": [str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")]}
    library.refresh_state()
    library.sync()
    settle(app, window)
    assert "Exported 2 annotation copies." in library.sync_result.text()
    assert "a.pdf, b.pdf" in library.sync_result.text()
    service.sync = lambda folder: (_ for _ in ()).throw(RuntimeError("The folder is read-only."))
    library.sync()
    settle(app, window)
    assert "did not finish" in library.sync_result.text() and "read-only" in library.sync_result.text()


def test_real_backend_demo_when_available(app):
    pytest.importorskip("paperdesk.backend")
    window = create_window(demo=True)
    try:
        settle(app, window)
        assert window.connection_mode == "simulation" and window.status.get("simulated") is True
    finally:
        window.close()
    assert not window.runner.running


def test_screenshots_when_requested(app, make, tmp_path):
    target = os.environ.get("PAPERDESK_UI_SHOTS")
    if not target:
        pytest.skip("set PAPERDESK_UI_SHOTS to a directory to save page screenshots")
    out = Path(target)
    out.mkdir(parents=True, exist_ok=True)
    window, service = demo_window(app, make)
    service.installed.update(("keyboard", "art-display"))
    window.refresh_status()
    settle(app, window)
    for size in ((900, 650), (1180, 800)):
        window.resize(*size)
        window.show()
        shots = [("home", None), ("library", None), ("art", None)]
        shots += [("type", s) for s in window.pages["type"].sections.ids()]
        shots += [("setup", s) for s in window.pages["setup"].sections.ids()]
        for page_id, section in shots:
            window.navigate(page_id, section)
            for _ in range(4):
                app.processEvents()
            name = f"{size[0]}-{page_id}" + (f"-{section}" if section else "") + ".png"
            assert window.grab().save(str(out / name))
    live, _, panel = live_window(app, make)
    live.resize(1000, 700)
    live.show()
    for _ in range(3):
        app.processEvents()
    assert live.grab().save(str(out / "live-home.png"))
    panel.submit()
    settle(app, live)
    for _ in range(3):
        app.processEvents()
    assert live.grab().save(str(out / "live-trust.png"))
    assert QGuiApplication.instance() is app
