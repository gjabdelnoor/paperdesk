"""The PaperDesk window: navigation, connection state and the operation queue."""
from __future__ import annotations

import html
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSettings, QSize, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QButtonGroup, QDialog, QFrame, QHBoxLayout, QMainWindow,
                               QMessageBox, QPlainTextEdit, QStackedWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import __version__
from ..models import DeviceConfig
from . import theme
from .page_art import ArtPage
from .page_home import HomePage
from .page_library import LibraryPage
from .page_setup import SetupPage
from .page_type import TypePage
from .services import Dialogs, ServiceProvider, describe_error, setup_hint
from .widgets import Banner, ElidedLabel, NavButton, StateDot, button, label
from .worker import Job, OperationRunner, is_uncertain

PAGES = [("home", "Home"), ("library", "Library"), ("type", "Type"), ("art", "Art"), ("setup", "Setup")]
_CONNECTION_JOBS = {"probe", "trust", "connect", "disconnect", "status", "sandbox-fault"}
SHORTCUTS = [
    ("Ctrl+1 … Ctrl+5", "Home, Library, Type, Art, Setup"),
    ("Ctrl+K", "Connect to the tablet"),
    ("Ctrl+Shift+K", "Disconnect"),
    ("F5", "Check tablet status"),
    ("Shift+F5", "Refresh the tablet's screen"),
    ("Ctrl+U", "Upload PDF or EPUB files"),
    ("Ctrl+D", "Download the selected document"),
    ("Ctrl+E", "Export the open document as PDF"),
    ("Ctrl+I", "Upload an image as a native sticker notebook"),
    ("Ctrl+G", "Figure as ink (experimental)"),
    ("Ctrl+Shift+S", "Save a tablet screenshot"),
    ("Ctrl+Shift+C", "Copy a tablet screenshot"),
    ("Ctrl+Enter", "Send the text in Type › Write"),
    ("Shift+Esc", "Stop the live keyboard"),
    ("Ctrl+C, Ctrl+V, Ctrl+W", "In the live keyboard: copy, paste, close document on the tablet"),
    ("Ctrl+L", "Activity log"),
    ("F1", "This list"),
    ("Tab, Shift+Tab", "Move between controls; Space or Enter activates"),
]


def needs_trust(exc: BaseException) -> bool:
    """True for the backend's 'pair this host first' refusal (HostTrustRequired)."""
    if getattr(exc, "trust_required", False) is True:
        return True
    return any("trust" in cls.__name__.lower() for cls in type(exc).__mro__)


def _flag(info: dict, names: tuple[str, ...], states: tuple[str, ...]) -> bool:
    if any(info.get(name) is True for name in names):
        return True
    return str(info.get("status", info.get("state", ""))).lower() in states


class MainWindow(QMainWindow):
    """Main window.  ``connection_mode`` is one of:
    ``unavailable``, ``disconnected``, ``connecting``, ``live``, ``simulation``.
    """

    state_changed = Signal()
    documents_changed = Signal()

    def __init__(self, service: Any = None, demo: bool = False, parent: QWidget | None = None, *,
                 provider: ServiceProvider | None = None, settings: QSettings | None = None):
        super().__init__(parent)
        self.settings = settings if settings is not None else QSettings("PaperDesk", "PaperDesk")
        self.provider = provider or ServiceProvider(
            live_service=None if demo else service, demo_service=service if demo else None)
        self.dialogs = Dialogs(self)
        self.runner = OperationRunner(self)
        self.backend_error = ""
        self.connect_error = ""
        self.pairing: dict | None = None
        self.documents: list[dict] = []
        self.art_state: dict = {"active": False, "application": ""}
        self.connection_lost = False
        self.log: list[tuple[float, str, str]] = []
        self._status: dict = {}
        self._connected = False
        self._connecting = False
        self._simulated = bool(demo)
        self._uncertain: tuple[str, str] | None = None
        self._live_service: Any = None
        self._config: DeviceConfig | None = None
        self._temp: Path | None = None
        self._closed = False
        self._log_dialog: QDialog | None = None
        self._shortcut_dialog: QDialog | None = None
        self.simulation_generation = 0

        if service is not None:
            self.service = service
        elif demo:
            self.service = self._make(self.provider.demo)
        else:
            self.service = self._make(lambda: self.provider.live(self.saved_config()))

        self.setWindowTitle("PaperDesk")
        self.setMinimumSize(880, 620)
        self.resize(1080, 740)
        theme.apply(self)
        self._build()
        self._build_menus()
        self.runner.started.connect(self._on_started)
        self.runner.succeeded.connect(self._on_succeeded)
        self.runner.failed.connect(self._on_failed)
        self.runner.rejected.connect(self._on_rejected)
        self.runner.changed.connect(self._update_queue)
        self.navigate("home")
        self._refresh()
        if self.service is None:
            self.notify("error", self.backend_error)
        elif demo:
            self.connect_simulator()
        else:
            self.notify("idle", "Not connected. PaperDesk only talks to a tablet when you ask it to.")

    # ------------------------------------------------------------------
    # state
    @property
    def connection_mode(self) -> str:
        if self.service is None:
            return "unavailable"
        if self._connecting:
            return "connecting"
        if self._simulated:
            return "simulation"
        return "live" if self._connected else "disconnected"

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def simulated(self) -> bool:
        return self._simulated

    @property
    def status(self) -> dict:
        return self._status

    @property
    def capabilities(self) -> dict[str, bool]:
        caps = self._status.get("capabilities") or {}
        if not isinstance(caps, dict):
            return {}
        return {str(k).lower().replace("_", "-"): v is True for k, v in caps.items()}

    @property
    def uncertain(self) -> tuple[str, str] | None:
        return self._uncertain

    def supports(self, feature: str) -> bool:
        """Advanced features are usable only when the tablet reports them; fail closed."""
        return self._connected and self.capabilities.get(feature) is True

    def allows(self, capability: str) -> bool:
        """Everyday actions are available unless the tablet explicitly reports them as off."""
        return self._connected and self.capabilities.get(capability, True) is not False

    def unsupported_reason(self, feature: str, what: str) -> str:
        if not self._connected:
            return f"Connect to a tablet to use {what}."
        if feature in self.capabilities:
            return (f"{what[:1].upper() + what[1:]} is not available on this tablet right now. "
                    "Setup › Features shows whether its firmware supports it and installs it.")
        return (f"The tablet did not report support for {what}, so it stays off. "
                "Setup › Features shows what this firmware allows.")

    def device_name(self) -> str:
        return str(self._status.get("model") or "Tablet")

    def saved_config(self) -> DeviceConfig:
        default = DeviceConfig()
        try:
            port = int(self.settings.value("connection/port", default.port))
        except (TypeError, ValueError):
            port = default.port
        return DeviceConfig(
            host=str(self.settings.value("connection/host", default.host) or default.host),
            port=port,
            username=str(self.settings.value("connection/username", default.username) or default.username),
            key_path=str(self.settings.value("connection/key_path", "") or ""))

    def _save_connection(self, config: DeviceConfig) -> None:
        # The password is deliberately absent: it lives in memory only.
        self.settings.setValue("connection/host", config.host)
        self.settings.setValue("connection/port", int(config.port))
        self.settings.setValue("connection/username", config.username)
        self.settings.setValue("connection/key_path", config.key_path)
        self.settings.sync()

    def _make(self, factory: Callable[[], Any]) -> Any:
        try:
            service = factory()
            self.backend_error = ""
            return service
        except Exception as exc:
            self.backend_error = ("The device backend is not available in this build, so PaperDesk "
                                  f"cannot reach a tablet or the simulator. ({describe_error(exc)})")
            return None

    def temp_dir(self) -> Path:
        if self._temp is None:
            self._temp = Path(tempfile.mkdtemp(prefix="paperdesk-"))
        return self._temp

    # ------------------------------------------------------------------
    # construction
    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(196)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 20, 0, 14)
        side.setSpacing(2)
        mark = label("PaperDesk", "wordmark", wrap=False)
        mark.setContentsMargins(22, 0, 0, 14)
        side.addWidget(mark)
        self.nav: dict[str, NavButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for index, (page_id, text) in enumerate(PAGES):
            nav = NavButton(page_id, text, f"Ctrl+{index + 1}")
            nav.clicked.connect(lambda _=False, p=page_id: self.navigate(p))
            group.addButton(nav)
            side.addWidget(nav)
            self.nav[page_id] = nav
        side.addStretch(1)
        chip = QWidget()
        chip_row = QHBoxLayout(chip)
        chip_row.setContentsMargins(22, 0, 12, 0)
        chip_row.setSpacing(8)
        self.chip_dot = StateDot()
        self.chip_label = label("", "caption")
        self.chip_label.setAccessibleName("Connection state")
        chip_row.addWidget(self.chip_dot, 0, Qt.AlignmentFlag.AlignTop)
        chip_row.addWidget(self.chip_label, 1)
        side.addWidget(chip)
        row.addWidget(sidebar)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.sim_banner = Banner("sim", "Simulation notice")
        self.sim_banner.text.setText(
            "<b>Simulation</b> — a pretend tablet running on this computer. "
            "Nothing here reaches a real tablet or the network.")
        self.leave_sim_button = self.sim_banner.add_action("Leave simulation", self.leave_simulation)
        self.uncertain_banner = Banner("error", "Unknown outcome warning")
        self.uncertain_banner.add_action("Check tablet state", self.check_after_uncertain)
        self.uncertain_banner.add_action("I have checked — dismiss", self.dismiss_uncertain)
        self.lost_banner = Banner("warn", "Connection lost notice")
        self.lost_banner.add_action("Reconnect…", self.request_connect)
        self.lost_banner.add_action("Dismiss", self._dismiss_lost)
        self.art_banner = Banner("info", "Art tablet mode notice")
        self.art_banner.add_action("Stop tablet mode", lambda: self.pages["art"].stop_mode())
        for banner in (self.sim_banner, self.uncertain_banner, self.lost_banner, self.art_banner):
            right.addWidget(banner)

        self.stack = QStackedWidget()
        self.pages: dict[str, Any] = {}
        for page_id, cls in (("home", HomePage), ("library", LibraryPage), ("type", TypePage),
                             ("art", ArtPage), ("setup", SetupPage)):
            page = cls(self)
            self.pages[page_id] = page
            self.stack.addWidget(page)
        right.addWidget(self.stack, 1)

        activity = QFrame()
        activity.setObjectName("activity")
        act = QHBoxLayout(activity)
        act.setContentsMargins(20, 7, 12, 7)
        act.setSpacing(9)
        self.activity_dot = StateDot()
        self.activity_label = ElidedLabel("Operation status")
        self.queue_label = label("", "caption", wrap=False)
        self.queue_label.setAccessibleName("Waiting operations")
        log_button = button("Activity log", "link", "Show everything PaperDesk did in this session")
        log_button.clicked.connect(self.show_log)
        self.hint_button = button("", "link", "Go to the place where this can be fixed")
        self.hint_button.clicked.connect(self._follow_hint)
        self.hint_button.hide()
        self._hint: tuple[str, str, str | None] | None = None
        act.addWidget(self.activity_dot)
        act.addWidget(self.activity_label, 1)
        act.addWidget(self.hint_button)
        act.addWidget(self.queue_label)
        act.addWidget(log_button)
        right.addWidget(activity)
        holder = QWidget()
        holder.setLayout(right)
        row.addWidget(holder, 1)

    def _action(self, menu, text: str, slot: Callable, shortcut: str | QKeySequence.StandardKey = "",
                role: QAction.MenuRole | None = None) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        if role is not None:
            action.setMenuRole(role)
        action.triggered.connect(lambda _=False: slot())
        menu.addAction(action)
        return action

    def _build_menus(self) -> None:
        bar = self.menuBar()
        lib, typ, setup = self.pages["library"], self.pages["type"], self.pages["setup"]
        file_menu = bar.addMenu("&File")
        self._action(file_menu, "&Upload to Tablet…", lib.upload, "Ctrl+U")
        self._action(file_menu, "&Download Selected…", lib.download, "Ctrl+D")
        self._action(file_menu, "&Export Open Document as PDF…", lib.export_current, "Ctrl+E")
        file_menu.addSeparator()
        self._action(file_menu, "&Save Tablet Screenshot…", typ.save_screenshot, "Ctrl+Shift+S")
        self._action(file_menu, "&Copy Tablet Screenshot", typ.copy_screenshot, "Ctrl+Shift+C")
        file_menu.addSeparator()
        self._action(file_menu, "Upload &Image as Sticker…", typ.upload_sticker, "Ctrl+I")
        self._action(file_menu, "Figure as In&k…", lambda: self.navigate("type", "ink"), "Ctrl+G")
        file_menu.addSeparator()
        self._action(file_menu, "&Quit", self.close, QKeySequence.StandardKey.Quit,
                     QAction.MenuRole.QuitRole)

        tablet = bar.addMenu("&Tablet")
        self.act_connect = self._action(tablet, "&Connect…", self.request_connect, "Ctrl+K")
        self.act_disconnect = self._action(tablet, "&Disconnect", self.disconnect_tablet, "Ctrl+Shift+K")
        self._action(tablet, "&Refresh Status", self.refresh_status, "F5")
        self._action(tablet, "Redraw Tablet &Screen", self.redraw_tablet, "Shift+F5")
        tablet.addSeparator()
        self.act_sim = self._action(tablet, "&Try the Simulator", self.start_simulation)
        self.act_leave_sim = self._action(tablet, "&Leave Simulation", self.leave_simulation)

        view = bar.addMenu("&View")
        for index, (page_id, text) in enumerate(PAGES):
            self._action(view, f"&{text}", lambda p=page_id: self.navigate(p), f"Ctrl+{index + 1}")
        view.addSeparator()
        self._action(view, "&Activity Log", self.show_log, "Ctrl+L")

        help_menu = bar.addMenu("&Help")
        self._action(help_menu, "&Developer Mode Walkthrough", lambda: self.navigate("setup", "developer"))
        self._action(help_menu, "D&iagnostics", lambda: self.navigate("setup", "diagnostics"))
        self._action(help_menu, "&Recovery and Uninstall", lambda: self.navigate("setup", "recovery"))
        self._action(help_menu, "&Keyboard Shortcuts", self.show_shortcuts, "F1")
        help_menu.addSeparator()
        self._action(help_menu, "&About PaperDesk", self.show_about, role=QAction.MenuRole.AboutRole)

    # ------------------------------------------------------------------
    # navigation and display
    def navigate(self, page_id: str, section: str | None = None) -> None:
        if page_id not in self.pages:
            return
        page = self.pages[page_id]
        self.stack.setCurrentWidget(page)
        self.nav[page_id].setChecked(True)
        if section and hasattr(page, "sections"):
            page.sections.select(section)

    @property
    def current_page(self) -> str:
        for page_id, page in self.pages.items():
            if self.stack.currentWidget() is page:
                return page_id
        return ""

    def notify(self, level: str, text: str) -> None:
        """Show one line in the activity bar and keep it in the session log."""
        colors = {"ok": (theme.OK, True), "error": (theme.ERR, True), "warn": (theme.WARN, True),
                  "busy": (theme.ACCENT, True), "idle": (theme.FAINT, False), "info": (theme.MUTED, False)}
        color, filled = colors.get(level, colors["info"])
        self.activity_dot.set_state(color, filled)
        self.activity_label.set_full_text(text)
        # A problem that Setup or Art can fix gets a link there, so nobody is sent to a terminal.
        self._hint = setup_hint(text) if level in ("error", "warn") else None
        if self._hint is not None and self.current_page == self._hint[1] and (
                self._hint[2] is None or getattr(self.pages[self._hint[1]], "sections", None) is None
                or self.pages[self._hint[1]].sections.current == self._hint[2]):
            self._hint = None
        self.hint_button.setVisible(self._hint is not None)
        if self._hint is not None:
            self.hint_button.setText(self._hint[0])
            self.hint_button.setAccessibleName(self._hint[0])
        self.log.append((time.time(), level, text))
        del self.log[:-400]
        if self._log_dialog is not None and self._log_dialog.isVisible():
            self._fill_log()

    def _follow_hint(self) -> None:
        if self._hint is not None:
            _, page, section = self._hint
            self.hint_button.hide()
            self.navigate(page, section)

    @property
    def status_text(self) -> str:
        return self.activity_label.full_text()

    def _update_queue(self) -> None:
        waiting = self.runner.waiting
        self.queue_label.setText(f"{waiting} waiting" if waiting else "")

    def _refresh(self) -> None:
        mode = self.connection_mode
        self.sim_banner.setVisible(self._simulated)
        self.setWindowTitle("PaperDesk — Simulation" if self._simulated else "PaperDesk")
        chip = {"unavailable": ("Backend unavailable", theme.ERR, True),
                "disconnected": ("Not connected", theme.FAINT, False),
                "connecting": ("Connecting…", theme.ACCENT, False),
                "live": (f"Connected\n{self.device_name()}", theme.OK, True),
                "simulation": ("Simulation\n" + (self.device_name() if self._connected
                                                else "not started"), theme.INK, self._connected)}[mode]
        self.chip_label.setText(chip[0])
        self.chip_dot.set_state(chip[1], chip[2])
        self.lost_banner.setVisible(self.connection_lost and not self._connected)
        if self.art_state.get("active"):
            app = str(self.art_state.get("application") or "the art application").capitalize()
            self.art_banner.show_text(
                f"<b>Art tablet mode is running</b> with {app}. The tablet is acting as a drawing "
                "surface until you stop it.")
        else:
            self.art_banner.hide()
        if hasattr(self, "act_connect"):
            self.act_connect.setEnabled(self.service is not None and not self._connected
                                        and not self._connecting)
            self.act_disconnect.setEnabled(self._connected)
            self.act_sim.setEnabled(not self._simulated)
            self.act_leave_sim.setEnabled(self._simulated)
        for page in self.pages.values():
            page.refresh_state()
        self.state_changed.emit()

    def show_log(self) -> None:
        if self._log_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("Activity log")
            dialog.resize(640, 420)
            layout = QVBoxLayout(dialog)
            layout.addWidget(label("Everything PaperDesk did in this session, oldest first. "
                                   "Typed text and passwords are never recorded.", "muted"))
            self._log_view = QPlainTextEdit()
            self._log_view.setReadOnly(True)
            self._log_view.setAccessibleName("Activity log entries")
            layout.addWidget(self._log_view, 1)
            close = button("Close")
            close.clicked.connect(dialog.close)
            layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
            self._log_dialog = dialog
        self._fill_log()
        self._log_dialog.show()
        self._log_dialog.raise_()

    def _fill_log(self) -> None:
        words = {"ok": "Done", "error": "Problem", "warn": "Note", "busy": "Started"}
        lines = [f"{time.strftime('%H:%M:%S', time.localtime(stamp))}  {words.get(level, ''):8}{text}"
                 for stamp, level, text in self.log]
        self._log_view.setPlainText("\n".join(lines))
        bar = self._log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def show_shortcuts(self) -> None:
        if self._shortcut_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("Keyboard shortcuts")
            dialog.resize(520, 560)
            layout = QVBoxLayout(dialog)
            layout.addWidget(label("Every action is also in the menus and on its page. Shortcuts act on "
                                   "PaperDesk; only Type › Live keyboard sends key presses to the tablet.",
                                   "muted"))
            view = QTreeWidget()
            view.setAccessibleName("Keyboard shortcuts")
            view.setHeaderLabels(["Keys", "What it does"])
            view.setRootIsDecorated(False)
            view.setAlternatingRowColors(True)
            for keys, text in SHORTCUTS:
                QTreeWidgetItem(view, [keys, text])
            view.resizeColumnToContents(0)
            layout.addWidget(view, 1)
            close = button("Close")
            close.clicked.connect(dialog.close)
            layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
            self._shortcut_dialog = dialog
        self._shortcut_dialog.show()
        self._shortcut_dialog.raise_()

    def show_about(self) -> None:
        QMessageBox.about(
            self, "About PaperDesk",
            f"<b>PaperDesk {__version__}</b><p>A desktop companion for reMarkable tablets in "
            "developer mode.</p><p>PaperDesk never connects, installs or retries on its own. "
            "It is an independent project and is not affiliated with reMarkable AS.</p>"
            "<p>Licensed under AGPL-3.0-or-later.</p>")

    # ------------------------------------------------------------------
    # running operations
    def run(self, name: str, fn: Callable[[Any], Any], *, label: str, mutation: bool = False,
            needs_connection: bool = True, dedupe: bool = True, key: str | None = None,
            quiet: bool = False, partial: bool = False,
            on_done: Callable[[Any], None] | None = None,
            on_error: Callable[[BaseException], None] | None = None) -> bool:
        """Queue one backend call.  Returns False, with a visible reason, when refused."""
        if self._closed:
            return False
        if self.service is None:
            self.notify("error", self.backend_error or "The device backend is not available.")
            return False
        if needs_connection and not self._connected:
            self.notify("warn", f"Not started: {label}. Connect to a tablet first (Home › Connect).")
            return False
        if mutation and self._uncertain is not None:
            self.notify("warn", f"Not started: {label}. An earlier change has an unknown outcome; "
                                "check the tablet and dismiss the warning above first.")
            return False
        service = self.service
        job = Job(name=name, label=label, fn=lambda: fn(service), mutation=mutation, quiet=quiet,
                  partial=partial,
                  key=(key or name) if dedupe else None, on_done=on_done, on_error=on_error)
        return self.runner.submit(job)

    def _on_started(self, job: Job) -> None:
        if not job.quiet:
            self.notify("busy", f"Working: {job.label}…")

    def _on_succeeded(self, job: Job, result: Any) -> None:
        if job.quiet:
            return
        message = str(result.get("message") or "") if isinstance(result, dict) else ""
        if isinstance(result, dict) and result.get("ok") is False:
            self.notify("error", f"Not completed: {job.label}. "
                                 f"{message or 'The tablet reported that it did not succeed.'}")
            return
        self.notify("ok", f"Done: {job.label}." + (f" {message}" if message else ""))

    def _on_failed(self, job: Job, exc: BaseException) -> None:
        text = describe_error(exc)
        if job.mutation and (is_uncertain(exc) or job.partial):
            self.hold_uncertain(job.label, text)
        else:
            self.notify("error", f"Failed: {job.label}. {text}")
        if self._connected and job.name not in _CONNECTION_JOBS:
            self._verify_connection()

    def _on_rejected(self, job: Job, reason: str) -> None:
        if job.quiet:
            return
        if reason == "duplicate":
            self.notify("warn", f"Already in progress: {job.label}. It was not started a second time.")
        elif reason == "full":
            self.notify("warn", f"Not started: {job.label}. Too many operations are waiting.")
        else:
            self.notify("warn", f"Cancelled: {job.label}, because the operation before it failed.")

    def hold_uncertain(self, label: str, text: str) -> None:
        """Record that a change may be half-applied: say so, never retry, hold further changes."""
        self._uncertain = (label, text)
        self.uncertain_banner.show_text(
            f"<b>Outcome unknown: {html.escape(label)}.</b> The tablet may or may not have applied "
            f"this change. PaperDesk will not retry it, and further changes are on hold. "
            f"{html.escape(text)} Look at the tablet or check its state before doing it again.")
        self.notify("error", f"Outcome unknown: {label}. Not retried. {text}")
        self._refresh()

    def check_after_uncertain(self) -> None:
        self.refresh_status()
        self.reload_documents()

    def dismiss_uncertain(self) -> None:
        self._uncertain = None
        self.uncertain_banner.hide()
        self.notify("info", "Unknown-outcome warning dismissed. Changes are allowed again.")
        self._refresh()

    # ------------------------------------------------------------------
    # status and documents
    def refresh_status(self) -> bool:
        if not self._connected:
            self.notify("info", "Not connected, so there is no tablet status to refresh.")
            return False
        return self.run("status", lambda s: s.status(), label="Check tablet status",
                        on_done=self._apply_status, on_error=self._status_failed)

    def refresh_status_quietly(self) -> None:
        """Re-read status after a change, without replacing the result in the status line."""
        if self._connected and not self.runner.has_key("status"):
            self._verify_connection()

    def _verify_connection(self) -> None:
        self.run("status", lambda s: s.status(), label="Check tablet status", quiet=True,
                 on_done=self._apply_status, on_error=self._status_failed)

    def _apply_status(self, status: Any) -> None:
        status = dict(status) if isinstance(status, dict) else {}
        was = self._connected
        self._status = status
        self._connected = status.get("connected") is True
        if status.get("simulated") is True:
            self._simulated = True
        art = status.get("art")
        if isinstance(art, dict) and "active" in art:
            self.art_state = {**self.art_state, "active": art.get("active") is True,
                              "application": art.get("application") or self.art_state.get("application")}
        if was and not self._connected:
            self._mark_lost()
        self._refresh()

    def _status_failed(self, exc: BaseException) -> None:
        if self._connected:
            self._mark_lost(describe_error(exc))
            self._refresh()

    def _mark_lost(self, detail: str = "") -> None:
        self._connected = False
        self._status = {}
        self.documents = []
        self.art_state = {"active": False, "application": ""}
        self.connection_lost = True
        self.lost_banner.show_text(
            "<b>Connection lost.</b> Nothing was retried and PaperDesk is not reconnecting on its own. "
            "When the tablet is awake and plugged in, connect again." + (f" ({detail})" if detail else ""))
        self.notify("error", "Connection lost. Nothing was retried." + (f" {detail}" if detail else ""))
        self.documents_changed.emit()

    def _dismiss_lost(self) -> None:
        self.connection_lost = False
        self._refresh()

    def redraw_tablet(self) -> bool:
        if not self.allows("refresh"):
            self.notify("warn", "This tablet does not offer a screen refresh." if self._connected
                        else "Connect to a tablet first to refresh its screen.")
            return False
        return self.run("refresh", lambda s: s.refresh(), label="Refresh the tablet's screen", mutation=True)

    def reload_documents(self, quiet: bool = False) -> bool:
        """Read the document list.  Quiet reloads follow another operation and keep its result visible."""
        if quiet and self.runner.has_key("documents"):
            return False
        return self.run("documents", lambda s: s.documents(), label="Read the tablet's document list",
                        quiet=quiet, on_done=self._set_documents)

    def _set_documents(self, documents: Any) -> None:
        self.documents = [d for d in (documents or []) if isinstance(d, dict)]
        self.documents_changed.emit()

    # ------------------------------------------------------------------
    # connecting
    def request_connect(self) -> None:
        """The one-click path from Home and the menu."""
        if self._connected or self._connecting:
            return
        if self._simulated:
            self.connect_simulator()
            return
        panel = self.pages["setup"].connect_panel
        if panel.ready():
            panel.submit()
        else:
            self.navigate("setup", "connect")
            panel.focus_first_missing()
            self.notify("info", "Enter the tablet's password or choose a key file, then connect.")

    def connect_with(self, config: DeviceConfig) -> bool:
        """Start pairing with a real tablet: read its identity, then wait for trust."""
        if self._simulated:
            self.notify("warn", "Leave the simulation before connecting to a real tablet.")
            return False
        if self._connected or self._connecting:
            return False
        self._save_connection(config)
        service = self._make(lambda: self.provider.live(config))
        if service is None:
            self.notify("error", self.backend_error)
            self._refresh()
            return False
        self.service = service
        self._config = config
        self.pairing = None
        return self._connect(on_error=self._first_connect_failed)

    def _first_connect_failed(self, exc: BaseException) -> None:
        """An unpaired tablet is not an error: read its identity and ask.  A changed one is refused."""
        if not needs_trust(exc):
            self._connect_failed(exc)
            return
        text = describe_error(exc)
        if getattr(exc, "changed", False) is True or "changed" in text.lower():
            found = re.search(r"SHA256:[A-Za-z0-9+/=]{20,}", text)
            self._refuse_changed(found.group(0) if found else "", text)
        else:
            self._probe()

    def _refuse_changed(self, fingerprint: str, detail: str = "") -> None:
        self._connecting = False
        self.pairing = {"host": self._config.host, "port": self._config.port, "fingerprint": fingerprint,
                        "key_type": "", "state": "changed", "message": detail}
        self.connect_error = ("This address now presents a different identity than the one you "
                              "trusted. PaperDesk refuses to connect.")
        self.notify("error", self.connect_error)
        self.navigate("setup", "connect")
        self._refresh()

    def connect_simulator(self, pair: bool = False) -> bool:
        if not self._simulated or self._connected or self._connecting:
            return False
        if pair and callable(getattr(self.service, "probe_host_key", None)):
            self._config = getattr(self.service, "config", None) or DeviceConfig()
            return self._probe(on_error=self._sim_probe_failed)
        return self._connect()

    def _sim_probe_failed(self, exc: BaseException) -> None:
        self._connecting = False
        self.notify("info", "The simulator offered no identity to review; starting it directly.")
        self._connect()

    def _probe(self, on_error: Callable | None = None) -> bool:
        config = self._config
        self.pairing = None
        self.connect_error = ""
        self._connecting = True
        ok = self.run("probe", lambda s: s.probe_host_key(config), needs_connection=False,
                      label=f"Read the identity of {config.host}",
                      on_done=self._on_probe, on_error=on_error or self._connect_failed)
        if not ok:
            self._connecting = False
        self._refresh()
        return ok

    def _on_probe(self, result: Any) -> None:
        info = dict(result) if isinstance(result, dict) else {}
        fingerprint = str(info.get("fingerprint") or "")
        if _flag(info, ("changed", "mismatch"), ("changed", "mismatch", "conflict")):
            state = "changed"
        elif _flag(info, ("trusted", "known"), ("trusted", "known")):
            state = "trusted"
        else:
            state = "new"
        self.pairing = {"host": info.get("host", self._config.host),
                        "port": info.get("port", self._config.port),
                        "fingerprint": fingerprint, "key_type": str(info.get("key_type") or ""),
                        "state": state, "message": str(info.get("message") or "")}
        if state == "trusted":
            self._connect()
            return
        if state == "changed":
            self._refuse_changed(fingerprint, str(info.get("message") or ""))
            return
        self._connecting = False
        if not fingerprint:
            self.pairing = None
            self.connect_error = "The tablet's identity could not be read, so it cannot be trusted."
            self.notify("error", self.connect_error)
        else:
            self.notify("warn", "Review the tablet's identity in Setup › Connect before trusting it.")
        self.navigate("setup", "connect")
        self._refresh()

    def trust_and_connect(self) -> bool:
        """Called only from the explicit trust button."""
        pairing, config = self.pairing, self._config
        if not pairing or pairing.get("state") != "new" or not pairing.get("fingerprint"):
            return False
        fingerprint = pairing["fingerprint"]
        self._connecting = True
        ok = self.run("trust", lambda s: s.trust_host_key(config, fingerprint), needs_connection=False,
                      label=f"Remember the identity of {pairing['host']}",
                      on_done=self._on_trusted, on_error=self._connect_failed)
        if not ok:
            self._connecting = False
        self._refresh()
        return ok

    def _on_trusted(self, result: Any) -> None:
        if isinstance(result, dict) and result.get("ok") is False:
            self._connect_failed(RuntimeError(str(result.get("message") or "Trust was not stored.")),
                                 announce=True)
            return
        if self.pairing:
            self.pairing["state"] = "trusted"
        self._connect()

    def cancel_pairing(self) -> None:
        self.pairing = None
        self.connect_error = ""
        self.notify("info", "Nothing was trusted and no connection was made.")
        self._refresh()

    def _connect(self, on_error: Callable | None = None) -> bool:
        self._connecting = True
        self.connect_error = ""
        text = "Start the simulated tablet" if self._simulated else \
            f"Connect to {self._config.host if self._config else 'the tablet'}"
        ok = self.run("connect", lambda s: s.connect(), label=text, needs_connection=False,
                      on_done=self._on_connected, on_error=on_error or self._connect_failed)
        if not ok:
            self._connecting = False
        self._refresh()
        return ok

    def _on_connected(self, status: Any) -> None:
        self._connecting = False
        self.connection_lost = False
        self._apply_status(status)
        if self._connected:
            self.reload_documents(quiet=True)
        else:
            message = str(self._status.get("message") or "The tablet did not report a connection.")
            self.connect_error = message
            self.notify("error", f"Not connected. {message}")
            self._refresh()

    def _connect_failed(self, exc: BaseException, announce: bool = False) -> None:
        self._connecting = False
        self.connect_error = describe_error(exc)
        if announce:
            self.notify("error", f"Not connected. {self.connect_error}")
        self._refresh()

    def disconnect_tablet(self, then: Callable[[], None] | None = None) -> bool:
        if not self._connected:
            if then:
                then()
            return False
        if self.art_state.get("active"):
            self.pages["art"].stop_mode()

        def done(_result: Any) -> None:
            self._reset_connection()
            if then:
                then()
            self._refresh()
        return self.run("disconnect", lambda s: s.disconnect(), label="Disconnect", on_done=done)

    def _reset_connection(self) -> None:
        self._connected = False
        self._connecting = False
        self._status = {}
        self.documents = []
        self.pairing = None
        self.connect_error = ""
        self.connection_lost = False
        self.art_state = {"active": False, "application": ""}
        self.documents_changed.emit()

    # ------------------------------------------------------------------
    # simulation
    def start_simulation(self) -> bool:
        if self._simulated:
            self.navigate("home")
            return True
        if self._connected or self._connecting or self.runner.busy:
            self.notify("warn", "Disconnect from the tablet and let running work finish before "
                                "starting the simulator.")
            return False
        service = self._make(self.provider.demo)
        if service is None:
            self.notify("error", self.backend_error)
            return False
        self._live_service, self.service = self.service, service
        self._reset_connection()
        self._simulated = True
        self.navigate("home")
        return self.connect_simulator()

    def start_fresh_simulation(self, model: str, firmware: str = "") -> bool:
        """Replace the simulated tablet with a new, empty one.  Simulation only.

        The new tablet comes from the simulator factory alone: no saved address,
        key file or password is read, and nothing reaches the network.
        """
        if not self._simulated:
            self.notify("warn", "A fresh simulated tablet can only be started inside the simulation.")
            return False
        if self._connecting or self.runner.busy:
            self.notify("warn", "Wait for the current operation to finish, then start a fresh tablet.")
            return False

        def swap() -> None:
            service = self._make(lambda: self.provider.demo(model=model, firmware=firmware or None))
            if service is None:
                self.notify("error", self.backend_error)
                self._refresh()
                return
            self.service = service
            self._reset_connection()
            self._uncertain = None
            self.uncertain_banner.hide()
            self.simulation_generation += 1
            self.connect_simulator()
        if self._connected:
            return self.disconnect_tablet(then=swap)
        swap()
        return True

    def leave_simulation(self) -> bool:
        if not self._simulated:
            return False
        if self._connecting or (self.runner.busy and not self._connected):
            self.notify("warn", "Wait for the current operation to finish, then leave the simulation.")
            return False
        if self._connected:
            return self.disconnect_tablet(then=self._swap_to_live)
        self._swap_to_live()
        self._refresh()
        return True

    def _swap_to_live(self) -> None:
        self._reset_connection()
        self._simulated = False
        self._uncertain = None
        self.uncertain_banner.hide()
        live = self._live_service
        self._live_service = None
        self.service = live if live is not None else self._make(
            lambda: self.provider.live(self.saved_config()))
        self.notify("idle", "Left the simulation. Not connected to any tablet."
                    if self.service is not None else self.backend_error)

    # ------------------------------------------------------------------
    # teardown
    def shutdown(self) -> bool:
        """Stop art mode, disconnect and stop the worker.  Safe to call twice."""
        if self._closed:
            return not self.runner.running
        self._closed = True
        final: list[Callable[[], Any]] = []
        service = self.service
        if service is not None and self._connected:
            if self.art_state.get("active"):
                final.append(service.stop_art)
            final.append(service.disconnect)
        done = self.runner.shutdown(final)
        self._connected = False
        if self._temp is not None:
            shutil.rmtree(self._temp, ignore_errors=True)
            self._temp = None
        return done

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(1080, 740)


def create_window(service: Any = None, demo: bool = False) -> MainWindow:
    """Build the main window.  A QApplication must already exist.

    With no service, the default window holds a disconnected SuiteService and
    never connects by itself; ``demo=True`` uses ``SuiteService.demo()``.
    """
    if QApplication.instance() is None:
        raise RuntimeError("Create a QApplication before calling create_window().")
    return MainWindow(service=service, demo=demo)
