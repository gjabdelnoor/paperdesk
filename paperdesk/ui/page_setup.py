"""Setup: developer-mode walkthrough, pairing, features, install, recovery, diagnostics."""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QLineEdit, QScrollArea, QSpinBox,
                               QVBoxLayout, QWidget)

from ..models import DeviceConfig
from . import theme
from .page_sandbox import SandboxPanel
from .services import FEATURE_NAMES, FEATURES
from .widgets import Card, DataView, Page, Sections, button, flow, label, set_role

_REFUSAL_KEYS = ("ok", "supported", "compatible", "allowed", "installable")


def plan_refusal(plan: Any) -> str:
    """Why the installer will not proceed, or an empty string when it reports it can."""
    if not isinstance(plan, dict):
        return "The installer returned no plan, so nothing can be installed."
    refused = any(plan.get(key) is False for key in _REFUSAL_KEYS) \
        or any(plan.get(key) is True for key in ("refused", "blocked"))
    if not refused:
        return ""
    denied = plan.get("unsupported")
    reasons = [f"{FEATURE_NAMES.get(str(item.get('feature')), item.get('feature'))}: {item.get('reason')}"
               for item in denied if isinstance(item, dict) and item.get("reason")] \
        if isinstance(denied, list) else []
    return str(plan.get("message") or plan.get("reason") or " ".join(reasons)
               or "The installer reports that this tablet or firmware is not supported.")


def plan_changes(plan: Any) -> str:
    """The installer's own list of changes and cautions, in its words, one per line."""
    if not isinstance(plan, dict):
        return ""
    device = plan.get("device") if isinstance(plan.get("device"), dict) else {}
    lines = []
    if device:
        seen = " ".join(str(device.get(key) or "") for key in ("observed_model", "architecture")).strip()
        lines.append(f"Tablet observed: {seen or device.get('model') or 'unknown'}, firmware "
                     f"{device.get('firmware') or 'not reported'}"
                     + (" (simulated)." if device.get("simulated") else "."))
    changes = plan.get("changes")
    if isinstance(changes, list) and changes:
        lines.append("It would:")
        lines += [f"  •  {item}" for item in changes]
    for key in ("reboot", "document_safety"):
        if isinstance(plan.get(key), str) and plan[key]:
            lines.append(plan[key])
    return "\n".join(lines)


def feature_verdict(plan: Any, feature: str) -> tuple[bool | None, str]:
    """What a plan says about one feature: (supported?, reason).  None means not stated."""
    if not isinstance(plan, dict):
        return None, ""
    verdict, reason = None, ""
    for key in ("supported", "compatibility", "features"):
        table = plan.get(key)
        entry = None
        if isinstance(table, dict):
            entry = next((v for k, v in table.items()
                          if str(k).lower().replace("_", "-") == feature), None)
        elif isinstance(table, list):
            entry = next((v for v in table if isinstance(v, dict)
                          and str(v.get("id", v.get("feature", ""))).lower().replace("_", "-") == feature),
                         None)
        if isinstance(entry, bool) and verdict is None:
            verdict = entry
        elif isinstance(entry, str) and not reason:
            reason = entry
        elif isinstance(entry, dict):
            if verdict is None:
                verdict = next((entry[k] for k in _REFUSAL_KEYS if isinstance(entry.get(k), bool)), None)
            reason = reason or str(entry.get("reason") or entry.get("message") or "")
    return verdict, reason


class ConnectPanel(QWidget):
    """Connection details and the deliberate host-identity trust step."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)

        self.form_card = Card("Tablet connection",
                              "Over the USB cable a tablet answers at 10.11.99.1 with the user name root.")
        saved = win.saved_config()
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        self.host = QLineEdit(saved.host)
        self.host.setAccessibleName("Tablet address")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(saved.port)
        self.port.setAccessibleName("Port")
        self.username = QLineEdit(saved.username)
        self.username.setAccessibleName("User name")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setAccessibleName("Tablet password")
        self.password.setPlaceholderText("Shown on the tablet; see step 1")
        self.show_password = QCheckBox("Show")
        self.show_password.setAccessibleName("Show password")
        self.show_password.toggled.connect(lambda on: self.password.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        self.key_path = QLineEdit(saved.key_path)
        self.key_path.setAccessibleName("Key file")
        self.key_path.setPlaceholderText("Optional: a private key file instead of the password")
        choose_key = button("Choose…", name="Choose key file")
        choose_key.clicked.connect(self._choose_key)
        host_row, pass_row, key_row = QHBoxLayout(), QHBoxLayout(), QHBoxLayout()
        for line in (host_row, pass_row, key_row):
            line.setSpacing(10)
        host_row.addWidget(self.host, 1)
        host_row.addWidget(label("Port", "muted", wrap=False))
        host_row.addWidget(self.port)
        pass_row.addWidget(self.password, 1)
        pass_row.addWidget(self.show_password)
        key_row.addWidget(self.key_path, 1)
        key_row.addWidget(choose_key)
        for row, (text, layout) in enumerate((("Address", host_row), ("User name", None),
                                              ("Password", pass_row), ("Key file", key_row))):
            grid.addWidget(label(text, None, wrap=False), row, 0)
            if layout is None:
                grid.addWidget(self.username, row, 1)
            else:
                grid.addLayout(layout, row, 1)
        self.form_card.body.addLayout(grid)
        self.form_card.add(label(
            "The password stays in this window's memory for this session only. It is never written to "
            "disk, never logged, and you never need a terminal. The address, user name and key file "
            "location are remembered.", "caption"))
        self.check_button = button("Check tablet identity", "primary",
                                   "Read the tablet's identity; nothing is trusted or changed yet")
        self.disconnect_button = button("Disconnect")
        self.check_button.clicked.connect(self.submit)
        self.disconnect_button.clicked.connect(lambda: win.disconnect_tablet())
        self.form_card.add(flow(self.check_button, self.disconnect_button))
        self.state_label = self.form_card.add(label("", "muted"))
        self.state_label.setAccessibleName("Connection state")
        column.addWidget(self.form_card)
        for edit in (self.host, self.username, self.password, self.key_path):
            edit.textChanged.connect(self.refresh_state)
            edit.returnPressed.connect(self.submit)

        self.trust_card = Card("Is this your tablet?")
        self.trust_intro = self.trust_card.add(label("", None))
        self.fingerprint = label("", "fingerprint", selectable=True)
        self.fingerprint.setFont(theme.mono_font())
        self.fingerprint.setAccessibleName("Host key fingerprint")
        self.trust_card.add(self.fingerprint)
        self.trust_card.add(label(
            "PaperDesk has not sent your password and has changed nothing. Trust this identity only if "
            "the tablet is plugged straight into this computer by its USB cable, or if the fingerprint "
            "matches one you recorded earlier. Once trusted it is remembered, and PaperDesk refuses to "
            "connect if this address ever presents a different one.", "muted"))
        self.trust_check = QCheckBox("The tablet is plugged directly into this computer, or I have "
                                     "compared the fingerprint")
        self.trust_check.toggled.connect(self.refresh_state)
        self.trust_card.add(self.trust_check)
        self.trust_button = button("Trust this tablet and connect", "primary")
        self.cancel_button = button("Do not trust")
        self.trust_button.clicked.connect(self._trust)
        self.cancel_button.clicked.connect(win.cancel_pairing)
        self.trust_card.add(flow(self.trust_button, self.cancel_button))
        column.addWidget(self.trust_card)
        column.addStretch(1)

    def _reveal_trust(self) -> None:
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        if parent is not None and not self.trust_card.isHidden():
            parent.ensureWidgetVisible(self.trust_card, 0, 24)
            self.trust_check.setFocus()

    def _choose_key(self) -> None:
        path = self.win.dialogs.open_file("Choose a private key file", "", "keys")
        if path:
            self.key_path.setText(path)

    def config(self) -> DeviceConfig:
        return DeviceConfig(host=self.host.text().strip(), port=self.port.value(),
                            username=self.username.text().strip() or "root",
                            key_path=self.key_path.text().strip(), password=self.password.text())

    def ready(self) -> bool:
        return bool(self.host.text().strip()) and bool(self.password.text() or self.key_path.text().strip())

    def focus_first_missing(self) -> None:
        (self.host if not self.host.text().strip() else self.password).setFocus()

    def submit(self) -> None:
        if self.win.simulated:
            self.win.connect_simulator(pair=True)
        elif not self.ready():
            self.focus_first_missing()
            self.win.notify("info", "Enter the tablet's password or choose a key file, then check its identity.")
        else:
            self.trust_check.setChecked(False)
            self.win.connect_with(self.config())

    def _trust(self) -> None:
        if self.trust_check.isChecked():
            self.win.trust_and_connect()

    def refresh_state(self, *_args) -> None:
        win, mode = self.win, self.win.connection_mode
        sim, connected, busy = win.simulated, win.connected, mode == "connecting"
        for widget in (self.host, self.port, self.username, self.password, self.show_password,
                       self.key_path):
            widget.setEnabled(not sim and not connected and not busy and mode != "unavailable")
        self.check_button.setVisible(not connected)
        self.check_button.setText("Start simulated tablet" if sim else "Check tablet identity")
        self.check_button.setEnabled(not busy and mode != "unavailable" and (sim or self.ready()))
        self.disconnect_button.setVisible(connected and not sim)

        pairing = win.pairing
        if mode == "unavailable":
            text, role = win.backend_error, "error"
        elif busy:
            text, role = "Working… see the status line at the bottom of the window.", "muted"
        elif connected and sim:
            text, role = "The simulated tablet is running. No address or password is involved.", "ok"
        elif connected:
            text, role = f"Connected to {win.status.get('host') or self.host.text()}.", "ok"
        elif win.connect_error:
            text, role = win.connect_error, "error"
        elif sim:
            text, role = "The simulator needs no address or password. Start it to continue.", "muted"
        elif not self.ready():
            text, role = "Enter the password from the tablet, or choose a key file.", "muted"
        else:
            text, role = "Ready. The first step only reads the tablet's identity.", "muted"
        self.state_label.setText(text)
        set_role(self.state_label, role)

        show_trust = bool(pairing) and not connected and pairing.get("state") in ("new", "changed")
        appearing = show_trust and self.trust_card.isHidden()
        self.trust_card.setVisible(show_trust)
        if appearing:
            QTimer.singleShot(0, self._reveal_trust)
        if show_trust:
            kind = f"{pairing['key_type']} " if pairing.get("key_type") else ""
            self.fingerprint.setText(pairing.get("fingerprint") or "")
            self.fingerprint.setVisible(bool(pairing.get("fingerprint")))
            changed = pairing["state"] == "changed"
            origin = "The simulated tablet" if sim else f"The device at {pairing['host']}, port {pairing['port']},"
            if changed:
                self.trust_card.title_label.setText("This is not the tablet you trusted")
                self.trust_intro.setText(
                    f"{origin} now presents a different {kind}identity from the one PaperDesk "
                    "remembers. PaperDesk refuses to connect, has not sent your password, and offers no "
                    "override here. A factory reset legitimately changes a tablet's identity; anything "
                    "else deserves suspicion. The remembered identities are kept in trusted-hosts.json "
                    "in PaperDesk's state folder.")
                set_role(self.trust_intro, "error")
            else:
                self.trust_card.title_label.setText("Is this your tablet?")
                self.trust_intro.setText(f"{origin} presented this {kind}identity:")
                set_role(self.trust_intro, "strong")
            self.trust_check.setVisible(not changed)
            self.trust_button.setVisible(not changed)
            self.trust_button.setEnabled(self.trust_check.isChecked() and not busy)
            self.cancel_button.setText("Close" if changed else "Do not trust")


class SetupPage(Page):
    def __init__(self, win):
        super().__init__(win, "Setup", "Connect a tablet, choose extras, and undo them if you change your mind.")
        self._plan: Any = None
        self._plan_features: list[str] | None = None
        self.sections = Sections("Setup sections")
        self.col.addWidget(self.sections, 1)
        self.sections.add("developer", "1  Developer mode", self._build_developer())
        self.connect_panel = ConnectPanel(win)
        self.sections.add("connect", "2  Connect", self.connect_panel)
        self.sections.add("features", "3  Features", self._build_features())
        self.sections.add("install", "4  Review and install", self._build_install())
        self.sections.add("recovery", "Recovery", self._build_recovery())
        self.sections.add("diagnostics", "Diagnostics", self._build_diagnostics())
        self.sandbox = SandboxPanel(win)
        self.sections.add("sandbox", "Sandbox", self.sandbox)
        self.sections.set_section_visible("sandbox", win.simulated)

    @staticmethod
    def _column() -> tuple[QWidget, QVBoxLayout]:
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)
        return holder, column

    # -- sections ------------------------------------------------------
    def _build_developer(self) -> QWidget:
        holder, column = self._column()
        card = Card("Your tablet should already be in developer mode")
        card.add(label(
            "PaperDesk expects developer mode to be switched on already. It never turns developer mode "
            "on and never resets a tablet: switching it on erases the tablet, and that choice is made "
            "on the tablet itself, by you. If yours is freshly in developer mode, four steps remain.",
            "muted"))
        steps = (
            ("Plug in and wake the tablet",
             "Use the USB-C cable that came with it, straight into this computer. A sleeping tablet "
             "does not answer."),
            ("Find the tablet's password",
             "On the tablet open Settings › Help › Copyrights and licenses. Under General information "
             "it shows a password and the address 10.11.99.1. Menu names can differ slightly between "
             "firmware versions."),
            ("Connect from PaperDesk",
             "In the next step you type that password into PaperDesk, never into a terminal. PaperDesk "
             "first shows the tablet's identity and asks whether you trust it."),
            ("Choose extras, or none",
             "Sending, fetching and exporting documents works straight away. Typing, touch, image "
             "paste and art mode are optional extras that depend on the tablet's exact firmware."),
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(1, 1)
        for index, (title, text) in enumerate(steps):
            number = label(str(index + 1), "step", wrap=False)
            grid.addWidget(number, index, 0, Qt.AlignmentFlag.AlignTop)
            cell = QVBoxLayout()
            cell.setSpacing(2)
            cell.addWidget(label(title, "strong"))
            cell.addWidget(label(text, None))
            grid.addLayout(cell, index, 1)
        card.body.addLayout(grid)
        go = button("Continue to Connect", "primary")
        go.clicked.connect(lambda: self.sections.select("connect"))
        self.dev_sim_button = button("Try the simulator instead", None,
                                     "Walk through setup with a pretend tablet")
        self.dev_sim_button.clicked.connect(self.win.start_simulation)
        card.add(flow(go, self.dev_sim_button))
        column.addWidget(card)
        column.addStretch(1)
        return holder

    def _build_features(self) -> QWidget:
        holder, column = self._column()
        card = Card("Optional extras",
                    "Tick what you want. Nothing is installed until you review the plan and confirm.")
        self.feature_checks: dict[str, QCheckBox] = {}
        self.feature_states: dict[str, Any] = {}
        for feature, name, about in FEATURES:
            check = QCheckBox(name)
            check.setAccessibleName(name)
            check.setAccessibleDescription(about)
            check.toggled.connect(self._selection_changed)
            state = label("", "muted")
            state.setAccessibleName(f"{name} compatibility")
            row = QHBoxLayout()
            row.addWidget(check)
            row.addStretch(1)
            row.addWidget(state)
            card.body.addLayout(row)
            about_label = label(about, "caption")
            about_label.setContentsMargins(24, 0, 0, 4)
            card.add(about_label)
            self.feature_checks[feature] = check
            self.feature_states[feature] = state
        column.addWidget(card)

        gate = Card("Firmware restrictions")
        self.firmware_label = gate.add(label("", "strong"))
        self.firmware_label.setAccessibleName("Tablet firmware")
        gate.add(label(
            "These extras reach into the tablet's own software, so each is tied to exact firmware "
            "versions. On any other firmware the installer refuses instead of guessing, and the plan "
            "says so. Document actions in Library keep working either way.", "muted"))
        self.plan_button = button("Review installation plan", "primary",
                                  "Ask the installer what it would change; nothing is installed")
        self.plan_button.clicked.connect(self.review_plan)
        gate.add(flow(self.plan_button))
        column.addWidget(gate)
        column.addStretch(1)
        return holder

    def _build_install(self) -> QWidget:
        holder, column = self._column()
        card = Card("Installation plan")
        self.plan_summary = card.add(label("", "muted"))
        self.plan_summary.setAccessibleName("Plan summary")
        self.plan_changes = card.add(label("", None))
        self.plan_changes.setAccessibleName("What the installer would change")
        self.plan_changes.setTextFormat(Qt.TextFormat.PlainText)
        self.plan_view = DataView("Installation plan details")
        card.add(self.plan_view)
        self.replan_button = button("Review plan again")
        self.install_button = button("Install", "primary")
        self.replan_button.clicked.connect(self.review_plan)
        self.install_button.clicked.connect(self.install)
        card.add(flow(self.install_button, self.replan_button))
        card.add(label("PaperDesk writes only its own files on the tablet plus temporary settings that a "
                       "restart clears. If an installation ends with an unknown outcome it is not retried; "
                       "run Diagnostics before deciding what to do.", "caption"))
        column.addWidget(card)
        self.result_card = Card("What the installer reported")
        self.result_summary = self.result_card.add(label("", None))
        self.result_view = DataView("Installer result details")
        self.result_card.add(self.result_view)
        self.result_card.setVisible(False)
        column.addWidget(self.result_card)
        column.addStretch(1)
        return holder

    def _build_recovery(self) -> QWidget:
        holder, column = self._column()
        card = Card("Remove PaperDesk's extras from the tablet",
                    "Uninstalling removes what PaperDesk installed and leaves your documents and other "
                    "tools' settings alone.")
        self.uninstall_button = button("Uninstall from tablet…", "danger")
        self.uninstall_button.clicked.connect(self.uninstall)
        card.add(flow(self.uninstall_button))
        self.uninstall_summary = card.add(label("", None))
        self.uninstall_view = DataView("Uninstall result details")
        self.uninstall_view.setVisible(False)
        card.add(self.uninstall_view)
        column.addWidget(card)
        help_card = Card("If something looks wrong")
        for title, text in (
                ("The tablet behaves oddly after installing",
                 "Restart the tablet. Temporary settings are cleared by a restart. Then connect and "
                 "uninstall here."),
                ("An operation ended with an unknown outcome",
                 "Do not repeat it. Look at the tablet, run Diagnostics, and only then decide. PaperDesk "
                 "holds further changes until you dismiss the warning."),
                ("The connection dropped",
                 "Wake the tablet, check the cable and connect again. PaperDesk does not reconnect or "
                 "retry by itself."),
                ("The tablet's identity changed",
                 "PaperDesk refuses to connect. A factory reset is the usual innocent reason; if you did "
                 "not reset the tablet, do not enter its password.")):
            help_card.add(label(title, "strong"))
            help_card.add(label(text, "muted"))
        column.addWidget(help_card)
        column.addStretch(1)
        return holder

    def _build_diagnostics(self) -> QWidget:
        holder, column = self._column()
        card = Card("Diagnostics", "A read-only report from the device service. Useful when asking for help; "
                                   "check it for anything you would rather not share before sending it.")
        self.diag_button = button("Run diagnostics", "primary")
        self.diag_copy = button("Copy report")
        self.diag_save = button("Save report…")
        self.diag_button.clicked.connect(self.run_diagnostics)
        self.diag_copy.clicked.connect(self.copy_diagnostics)
        self.diag_save.clicked.connect(self.save_diagnostics)
        card.add(flow(self.diag_button, self.diag_copy, self.diag_save))
        self.diag_view = DataView("Diagnostics report")
        self.diag_view.setMinimumHeight(260)
        card.add(self.diag_view)
        column.addWidget(card, 1)
        return holder

    # -- state ---------------------------------------------------------
    def selected_features(self) -> list[str]:
        return [feature for feature, _, _ in FEATURES if self.feature_checks[feature].isChecked()]

    def _plan_current(self) -> bool:
        return self._plan_features is not None and self._plan_features == self.selected_features()

    def _selection_changed(self, *_args) -> None:
        self.refresh_state()

    def refresh_state(self) -> None:
        win, connected = self.win, self.win.connected
        self.connect_panel.refresh_state()
        self.sections.set_section_visible("sandbox", win.simulated)
        self.sandbox.refresh_state()
        self.dev_sim_button.setVisible(not win.simulated)
        if not connected:
            self._plan, self._plan_features = None, None
        selected = self.selected_features()
        status = win.status
        if connected:
            self.firmware_label.setText(f"{win.device_name()}, firmware {status.get('firmware') or 'not reported'}"
                                        + (" (simulated)" if win.simulated else ""))
        else:
            self.firmware_label.setText("Connect a tablet to check its firmware.")
        for feature, state in self.feature_states.items():
            verdict, reason = feature_verdict(self._plan, feature)
            if not connected:
                text, role = "", "muted"
            elif win.supports(feature):
                text, role = "Active on this tablet", "ok"
            elif verdict is False:
                text, role = "Refused: " + (reason or "not supported on this firmware"), "error"
            elif verdict is True:
                text, role = reason or "Supported on this firmware", "ok"
            else:
                text, role = reason or "Not active", "muted"
            state.setText(text)
            set_role(state, role)
        self.plan_button.setEnabled(connected and bool(selected))
        self.replan_button.setEnabled(connected and bool(selected))

        refusal = plan_refusal(self._plan) if self._plan is not None else ""
        names = ", ".join(FEATURE_NAMES[f] for f in selected)
        if not connected:
            summary, role = "Connect a tablet, choose extras in step 3, then review the plan here.", "muted"
        elif not selected:
            summary, role = "No extras are selected. Choose some in step 3; everyday actions need none.", "muted"
        elif not self._plan_current():
            summary, role = f"Selected: {names}. Review the plan before installing.", "muted"
        elif refusal:
            summary, role = f"The installer will not proceed: {refusal}", "error"
        else:
            message = str(self._plan.get("message") or "") if isinstance(self._plan, dict) else ""
            summary = f"Plan for: {names}. {message}".strip()
            role = "strong"
        self.plan_summary.setText(summary)
        set_role(self.plan_summary, role)
        shown = self._plan is not None and self._plan_current()
        self.plan_view.setVisible(shown)
        self.plan_changes.setText(plan_changes(self._plan) if shown else "")
        self.plan_changes.setVisible(shown and bool(self.plan_changes.text()))
        self.install_button.setText(f"Install {len(selected)} extra{'' if len(selected) == 1 else 's'}…"
                                    if selected else "Install…")
        self.install_button.setEnabled(connected and bool(selected) and self._plan_current() and not refusal)
        self.uninstall_button.setEnabled(connected)
        self.diag_button.setEnabled(win.service is not None)
        has_report = self.diag_view.data is not None
        self.diag_copy.setEnabled(has_report)
        self.diag_save.setEnabled(has_report)

    # -- actions -------------------------------------------------------
    def review_plan(self) -> None:
        features = self.selected_features()
        if not features:
            self.win.notify("info", "Tick at least one extra in step 3 to see an installation plan.")
            return
        started = self.win.run("plan", lambda s: s.installer_plan(features),
                               label="Ask the installer for its plan",
                               on_done=lambda plan: self._plan_ready(plan, features))
        if started:
            self.sections.select("install")

    def _plan_ready(self, plan: Any, features: list[str]) -> None:
        self._plan, self._plan_features = plan, features
        self.plan_view.set_data(plan)
        self.refresh_state()

    def install(self) -> None:
        features = self.selected_features()
        if not features or not self._plan_current() or plan_refusal(self._plan):
            self.win.notify("warn", "Review a current installation plan first; nothing was installed.")
            return
        names = ", ".join(FEATURE_NAMES[f] for f in features)
        target = "the simulated tablet" if self.win.simulated else self.win.device_name()
        if not self.win.dialogs.confirm(
                f"Install on {target}?",
                f"PaperDesk will install: {names}.\n\nIt makes the changes listed in the plan you just "
                "reviewed. If the outcome is unclear it will not try again.", "Install"):
            return
        self.result_card.setVisible(False)
        self.win.run("install", lambda s: s.install(features), mutation=True,
                     label=f"Install {names}", on_done=self._installed)

    def _installed(self, result: Any) -> None:
        info = result if isinstance(result, dict) else {}
        failed = info.get("ok") is False
        self.result_summary.setText(str(info.get("message") or ("The installer reported a failure."
                                                                if failed else "The installer finished.")))
        set_role(self.result_summary, "error" if failed else "ok")
        self.result_view.set_data(info or None)
        self.result_card.setVisible(True)
        self._plan, self._plan_features = None, None
        self.refresh_state()
        self.win.refresh_status_quietly()

    def uninstall(self) -> None:
        target = "the simulated tablet" if self.win.simulated else self.win.device_name()
        if not self.win.dialogs.confirm(
                f"Uninstall from {target}?",
                "PaperDesk will remove the extras it installed. Your documents are not touched. "
                "If the outcome is unclear it will not try again.", "Uninstall", danger=True):
            return
        self.win.run("uninstall", lambda s: s.uninstall(), mutation=True,
                     label="Uninstall PaperDesk's extras", on_done=self._uninstalled)

    def _uninstalled(self, result: Any) -> None:
        info = result if isinstance(result, dict) else {}
        failed = info.get("ok") is False
        self.uninstall_summary.setText(str(info.get("message") or ("The uninstaller reported a failure."
                                                                   if failed else "Uninstall finished.")))
        set_role(self.uninstall_summary, "error" if failed else "ok")
        self.uninstall_view.set_data(info or None)
        self.uninstall_view.setVisible(bool(info))
        self._plan, self._plan_features = None, None
        self.win.refresh_status_quietly()

    def run_diagnostics(self) -> None:
        self.win.run("diagnostics", lambda s: s.diagnostics(), needs_connection=False,
                     label="Run diagnostics", on_done=self._diagnosed)

    def _diagnosed(self, report: Any) -> None:
        self.diag_view.set_data(report)
        self.refresh_state()

    def _report_text(self) -> str:
        return json.dumps(self.diag_view.data, indent=2, default=str, ensure_ascii=False)

    def copy_diagnostics(self) -> None:
        if self.diag_view.data is not None:
            QGuiApplication.clipboard().setText(self._report_text())
            self.win.notify("ok", "Done: diagnostics report copied to the clipboard.")

    def save_diagnostics(self) -> None:
        if self.diag_view.data is None:
            return
        path = self.win.dialogs.save_file("Save diagnostics report", "paperdesk-diagnostics.json",
                                          "JSON files (*.json)")
        if path:
            try:
                with open(path, "x", encoding="utf-8") as handle:
                    handle.write(self._report_text())
                self.win.notify("ok", f"Done: diagnostics report saved to {path}.")
            except OSError as exc:
                self.win.notify("error", f"The report could not be saved. {exc}")
