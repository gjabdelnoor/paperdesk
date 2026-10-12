"""Setup › Sandbox: restart, break and replace the simulated tablet.  Simulation only.

Every action here refuses unless the window is in simulation, and the tab is
hidden otherwise.  Nothing in this file can reach a real tablet.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLineEdit, QVBoxLayout, QWidget

from .services import SIM_FAULT, SIM_REBOOT, describe_error, method
from .widgets import Card, button, flow, label, set_role

MODELS = [("paper-pro", "reMarkable Paper Pro"), ("rm2", "reMarkable 2")]
DEFAULT_FIRMWARE = "3.28.0.172"
FAULTS = [
    ("offline", "Tablet stops answering", "Every operation fails until this is switched off again."),
    ("upload-unknown", "Upload loses its receipt",
     "The file arrives but the answer is lost: an unknown outcome that must not be retried."),
    ("wrong-firmware", "Unsupported firmware", "The tablet reports a firmware the extras do not allow."),
    ("wrong-hash", "Unverified tablet software", "The tablet's software build does not match the verified one."),
    ("duplicate-loader", "Another tool's loader is present",
     "The installer must refuse rather than replace someone else's configuration."),
    ("write-failure", "File writes fail during install", "Every third staged file fails to write."),
    ("manifest-corrupt", "Checksum mismatch during install", "Staged files fail verification."),
    ("activation-failure", "Activation fails after install", "The installer has to roll back."),
    ("reboot-during-install", "Tablet restarts mid-install",
     "Activation is interrupted, leaving an unknown outcome."),
    ("uninstall-failure", "Uninstall fails part-way", "The uninstaller has to restore what it removed."),
    ("restore-failure", "Rollback cannot be verified", "Recovery itself fails and says so."),
]
LIMITS = ("This sandbox rehearses setup and communication: connecting, installing and removing extras, "
          "restarts, lost connections and unknown outcomes. It is a behavioural model written for "
          "PaperDesk. It does not run reMarkable's software, and it cannot show whether typing, touch, "
          "images, the pen, the display or e-ink refresh behave correctly on a physical tablet. A step "
          "that works here is not evidence that it works on yours.")


class SandboxPanel(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._generation = win.simulation_generation
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)

        about = Card("Testing sandbox — simulated tablet only")
        about.add(label(LIMITS, "muted"))
        self.notice = about.add(label("", "warn"))
        self.notice.setAccessibleName("Sandbox availability")
        column.addWidget(about)

        restart = Card("Restart the simulated tablet",
                       "A restart clears temporary settings. The advanced extras stop working until you "
                       "run Setup › Review and install again, which is what the installer plans for.")
        self.reboot_button = button("Restart simulated tablet", None, "Simulation only")
        self.reboot_button.clicked.connect(self.reboot)
        self.restore_button = button("Go to Features to restore extras", "link")
        self.restore_button.clicked.connect(lambda: win.navigate("setup", "features"))
        restart.add(flow(self.reboot_button, self.restore_button))
        self.reboot_note = restart.add(label("", "muted"))
        self.reboot_note.setAccessibleName("Simulated restart result")
        column.addWidget(restart)

        faults = Card("Make something go wrong",
                      "Each switch changes how the simulated tablet behaves from the next operation on. "
                      "Use them to see what PaperDesk says and refuses to do.")
        self.fault_checks: dict[str, QCheckBox] = {}
        for fault, title, text in FAULTS:
            check = QCheckBox(title)
            check.setAccessibleName(f"Simulated fault: {title}")
            check.setAccessibleDescription(text)
            check.clicked.connect(lambda on, f=fault: self.set_fault(f, on))
            faults.add(check)
            caption = label(text, "caption")
            caption.setContentsMargins(24, 0, 0, 2)
            faults.add(caption)
            self.fault_checks[fault] = check
        self.fault_note = faults.add(label("", "muted"))
        self.fault_note.setAccessibleName("Simulated fault state")
        column.addWidget(faults)

        fresh = Card("Start a fresh simulated tablet",
                     "Throws this pretend tablet away and starts an empty one, freshly in developer mode, "
                     "with nothing installed. It uses no saved address, key or password and no network.")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.model = QComboBox()
        self.model.setAccessibleName("Simulated tablet model")
        for value, text in MODELS:
            self.model.addItem(text, value)
        self.firmware = QLineEdit(DEFAULT_FIRMWARE)
        self.firmware.setAccessibleName("Simulated firmware version")
        self.firmware.setMaximumWidth(150)
        self.firmware.setValidator(QRegularExpressionValidator(
            QRegularExpression(r"\d{1,3}(\.\d{1,4}){1,3}"), self.firmware))
        self.firmware.textChanged.connect(self.refresh_state)
        row.addWidget(label("Model", None, wrap=False))
        row.addWidget(self.model, 1)
        row.addWidget(label("Firmware", None, wrap=False))
        row.addWidget(self.firmware)
        fresh.body.addLayout(row)
        fresh.add(label(f"The extras that reach into the tablet's software are tied to firmware "
                        f"{DEFAULT_FIRMWARE} on a Paper Pro; choose anything else to rehearse a refusal.",
                        "caption"))
        self.fresh_button = button("Start fresh simulated tablet…", None, "Simulation only")
        self.fresh_button.clicked.connect(self.start_fresh)
        fresh.add(flow(self.fresh_button))
        column.addWidget(fresh)
        column.addStretch(1)

    def refresh_state(self, *_args) -> None:
        win, service = self.win, self.win.service
        sim = win.simulated
        if self._generation != win.simulation_generation or not sim:
            self._generation = win.simulation_generation
            for check in self.fault_checks.values():
                check.setChecked(False)
            self.reboot_note.setText("")
            self.fault_note.setText("")
        can_reboot = method(service, *SIM_REBOOT) is not None
        can_fault = method(service, *SIM_FAULT) is not None
        if not sim:
            notice = "The sandbox works only in the simulation. Start it from Home › Try the simulator."
        elif not (can_reboot and can_fault):
            notice = "This build's simulator does not offer restarts or faults; only a fresh tablet can be started."
        else:
            notice = ""
        self.notice.setText(notice)
        self.notice.setVisible(bool(notice))
        self.reboot_button.setEnabled(sim and can_reboot and win.connected)
        for check in self.fault_checks.values():
            check.setEnabled(sim and can_fault)
        self.fresh_button.setEnabled(sim and self.firmware.hasAcceptableInput())

    # -- actions: all refuse outside the simulation ----------------------
    def reboot(self) -> None:
        if not self.win.simulated:
            return
        self.win.run("sandbox-reboot", lambda s: method(s, *SIM_REBOOT)(), mutation=True,
                     label="Restart the simulated tablet", on_done=self._rebooted)

    def _rebooted(self, result: Any) -> None:
        info = result if isinstance(result, dict) else {}
        self.reboot_note.setText(str(info.get("message") or "The simulated tablet restarted.")
                                 + " Extras that depend on temporary settings are off until setup is "
                                   "run again.")
        set_role(self.reboot_note, "error" if info.get("ok") is False else "ok")
        self.win.refresh_status_quietly()

    def set_fault(self, fault: str, enabled: bool) -> None:
        check = self.fault_checks[fault]
        if not self.win.simulated:
            check.setChecked(False)
            return
        title = check.text()
        started = self.win.run(
            "sandbox-fault", lambda s: method(s, *SIM_FAULT)(fault, enabled), needs_connection=False,
            dedupe=False, label=f"{'Switch on' if enabled else 'Switch off'} simulated fault: {title}",
            on_done=lambda _r: self._fault_set(title, enabled),
            on_error=lambda exc: self._fault_failed(check, enabled, exc))
        if not started:
            check.setChecked(not enabled)

    def _fault_set(self, title: str, enabled: bool) -> None:
        active = [check.text() for check in self.fault_checks.values() if check.isChecked()]
        self.fault_note.setText(("Active simulated faults: " + ", ".join(active) + ".") if active
                                else "No simulated faults are active.")
        set_role(self.fault_note, "warn" if active else "muted")
        self.win.refresh_status_quietly()

    def _fault_failed(self, check: QCheckBox, enabled: bool, exc: BaseException) -> None:
        check.setChecked(not enabled)
        self.fault_note.setText(f"The fault was not changed. {describe_error(exc)}")
        set_role(self.fault_note, "error")

    def start_fresh(self) -> None:
        if not self.win.simulated or not self.firmware.hasAcceptableInput():
            return
        model, name, firmware = str(self.model.currentData()), self.model.currentText(), self.firmware.text()
        if self.win.dialogs.confirm(
                "Start a fresh simulated tablet?",
                f"The current simulated tablet and everything on it is discarded, and an empty {name} "
                f"on firmware {firmware} takes its place. No real tablet is involved.", "Start fresh"):
            self.win.start_fresh_simulation(model, firmware)
