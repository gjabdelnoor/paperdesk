"""Art: open drawing applications and run the tablet as a drawing surface."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QButtonGroup, QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QSlider, QSpinBox

from .services import (ART_APPLICATIONS, ART_PLUGIN, ART_PLUGIN_PLAN, ART_PROGRAM, accepts, describe_error,
                       method)
from .widgets import Card, DataView, Page, button, flow, label, set_role

APPS = [("krita", "Krita", "Painting and illustration"), ("inkscape", "Inkscape", "Vector drawing"),
        ("blender", "Blender", "3D and Grease Pencil")]
APP_NAMES = {app: name for app, name, _ in APPS}
ROTATIONS = [("auto", "Automatic"), ("portrait", "Portrait"), ("left", "Landscape, turned left"),
             ("right", "Landscape, turned right"), ("inverted", "Upside down")]
# (row label, key in the art bridge's own capability report)
CAPABILITIES = [("Pen position", "pointer"), ("Pen pressure", "pressure"), ("Pen tilt", "tilt"),
                ("Touch gestures", "multitouch"), ("Canvas on the tablet", "art_display")]
EXIT_HELP = ("Stop tablet mode here or from the bar at the top of any page. Disconnecting or closing "
             "PaperDesk stops it first, and your art application stays open. If the tablet still "
             "behaves as a drawing surface afterwards, restart the tablet: tablet mode uses only "
             "temporary settings, which a restart clears.")
WINDOWS_INK_NOTE = ("This computer reports Windows Ink pen input. In Krita choose Settings › Configure "
                    "Krita › Tablet settings › Windows 8+ Pointer Input, then restart Krita, or pressure "
                    "will not arrive.")
_STOPPED = ("stopped", "idle", "off", "inactive")


class ArtPage(Page):
    def __init__(self, win):
        super().__init__(win, "Art", "Draw with the tablet in a desktop art application.")
        self.application = "krita"
        self.app_info: dict[str, dict] = {}
        self._discovered_for: int | None = None
        self._touch_defaulted = False
        self._plugin_plan: dict | None = None
        self._plugin_exists = False
        self._build_applications()
        self._build_mode()
        self._build_display()
        self.col.addStretch(1)
        self._slider_text()

    # -- construction --------------------------------------------------
    def _build_applications(self) -> None:
        apps = Card("Application")
        self.app_buttons: dict[str, Any] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        row = []
        for app, name, about in APPS:
            btn = button(name, None, about, name=f"Use {name}")
            btn.setCheckable(True)
            btn.setMinimumWidth(104)
            btn.clicked.connect(lambda _=False, a=app: self.choose(a))
            group.addButton(btn)
            self.app_buttons[app] = btn
            row.append(btn)
        self.app_buttons["krita"].setChecked(True)
        apps.add(flow(*row))
        self.app_state = apps.add(label("", "muted"))
        self.app_state.setAccessibleName("Application installation state")
        self.open_button = button("Open Krita")
        self.open_file_button = button("Open file in Krita…")
        self.program_button = button("Choose executable…", None,
                                     "Point PaperDesk at an AppImage, portable copy or unusual install")
        self.discover_button = button("Look again", None, "Check again which applications are installed")
        self.open_button.clicked.connect(self.open_app)
        self.open_file_button.clicked.connect(self.open_file)
        self.program_button.clicked.connect(self.choose_program)
        self.discover_button.clicked.connect(lambda: self.discover(quiet=False))
        apps.add(flow(self.open_button, self.open_file_button, self.program_button, self.discover_button))
        self.sim_note = apps.add(label(
            "In the simulation nothing is launched: the simulator only reports what it would open.",
            "caption"))
        self.col.addWidget(apps)

    def _build_mode(self) -> None:
        mode = Card("Tablet mode", "Sends the tablet's pen to this computer as a drawing tablet.")
        self.cap_labels: dict[str, Any] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(1, 1)
        grid.addWidget(label("Tablet extra", "muted", wrap=False), 0, 0)
        self.extra_label = label("", None)
        self.extra_label.setAccessibleName("Art tablet extra state")
        grid.addWidget(self.extra_label, 0, 1)
        for index, (text, key) in enumerate(CAPABILITIES, start=1):
            grid.addWidget(label(text, "muted", wrap=False), index, 0)
            value = label("", None)
            value.setAccessibleName(f"{text} support")
            grid.addWidget(value, index, 1)
            self.cap_labels[key] = value
        mode.body.addLayout(grid)
        self.limits_label = mode.add(label("", "caption"))
        self.limits_label.setAccessibleName("Art input limits")
        self.ink_note = mode.add(label(WINDOWS_INK_NOTE, "warn"))
        self.ink_note.setAccessibleName("Windows Ink requirement")

        tune = QGridLayout()
        tune.setHorizontalSpacing(14)
        tune.setVerticalSpacing(8)
        tune.setColumnStretch(1, 1)
        tune.addWidget(label("Rotation", None, wrap=False), 0, 0)
        self.rotation = QComboBox()
        self.rotation.setAccessibleName("Tablet rotation")
        for value, text in ROTATIONS:
            self.rotation.addItem(text, value)
        tune.addWidget(self.rotation, 0, 1, 1, 2)
        self.pressure, self.pressure_value = self._slider("Pressure", 5, 100, 50)
        self.curve, self.curve_value = self._slider("Pressure curve", 20, 300, 80)
        self.tilt, self.tilt_value = self._slider("Tilt strength", 0, 300, 0)
        for row, (text, slider, shown) in enumerate((("Pressure", self.pressure, self.pressure_value),
                                                     ("Pressure curve", self.curve, self.curve_value),
                                                     ("Tilt strength", self.tilt, self.tilt_value)), start=1):
            tune.addWidget(label(text, None, wrap=False), row, 0)
            tune.addWidget(slider, row, 1)
            tune.addWidget(shown, row, 2)
        self.eraser_check = QCheckBox("Set eraser size")
        self.eraser_check.setAccessibleDescription("Otherwise the art bridge keeps its own eraser size")
        self.eraser = QSpinBox()
        self.eraser.setRange(4, 200)
        self.eraser.setValue(40)
        self.eraser.setAccessibleName("Eraser size")
        self.eraser_check.toggled.connect(self.refresh_state)
        eraser_row = QHBoxLayout()
        eraser_row.addWidget(self.eraser)
        eraser_row.addWidget(label("4 to 200", "caption", wrap=False))
        eraser_row.addStretch(1)
        tune.addWidget(self.eraser_check, 4, 0)
        tune.addLayout(eraser_row, 4, 1, 1, 2)
        mode.body.addLayout(tune)
        self.touch_check = QCheckBox("Forward touch gestures (pan, zoom, rotate)")
        self.touch_check.setAccessibleName("Forward touch gestures")
        mode.add(self.touch_check)
        mode.add(label("Pressure runs 0.05 to 1, curve 0.2 to 3 and tilt 0 to 3. They are handed to the "
                       "art bridge exactly as shown; it checks them and reports the settings it used. "
                       "They apply the next time tablet mode starts.", "caption"))
        self.start_button = button("Start tablet mode", "primary")
        self.stop_button = button("Stop tablet mode")
        self.start_button.clicked.connect(self.start_mode)
        self.stop_button.clicked.connect(self.stop_mode)
        mode.add(flow(self.start_button, self.stop_button))
        self.mode_notice = mode.add(label("", "warn"))
        self.mode_notice.setAccessibleName("Tablet mode state")
        self.report = DataView("What the art bridge reported")
        self.report.setVisible(False)
        mode.add(self.report)
        mode.add(label(EXIT_HELP, "caption"))
        self.col.addWidget(mode)

    def _build_display(self) -> None:
        card = Card("Krita canvas on the tablet",
                    "Optional, Krita only. It needs three things, and PaperDesk never does the second "
                    "or third for you without being asked.")
        self.display_steps: list[Any] = []
        for _ in range(3):
            step = label("", None)
            card.add(step)
            self.display_steps.append(step)
        self.plugin_plan_button = button("Review plugin copy", None,
                                         "Show where the Krita plugin would be copied; nothing is changed")
        self.plugin_button = button("Copy plugin to Krita…")
        self.plugin_replace_button = button("Replace existing plugin, keeping a backup…", "danger")
        self.plugin_plan_button.clicked.connect(self.plan_plugin)
        self.plugin_button.clicked.connect(lambda: self.copy_plugin(False))
        self.plugin_replace_button.clicked.connect(lambda: self.copy_plugin(True))
        card.add(flow(self.plugin_plan_button, self.plugin_button, self.plugin_replace_button))
        self.plugin_note = card.add(label("", "muted"))
        self.plugin_note.setAccessibleName("Krita plugin state")
        self.plugin_ready = QCheckBox("I enabled reMarkable Viewport in Krita's Python Plugin Manager and "
                                      "restarted Krita myself")
        self.plugin_ready.setAccessibleName("Krita plugin enabled and Krita restarted")
        self.plugin_ready.toggled.connect(self.refresh_state)
        card.add(self.plugin_ready)
        self.display_check = QCheckBox("Show the Krita canvas on the tablet when tablet mode starts")
        self.display_check.setAccessibleName("Show the Krita canvas on the tablet")
        card.add(self.display_check)
        self.display_note = card.add(label("", "warn"))
        self.display_note.setAccessibleName("Canvas display availability")
        self.col.addWidget(card)

    def _slider(self, name: str, low: int, high: int, value: int):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        slider.setValue(value)
        slider.setAccessibleName(name)
        slider.valueChanged.connect(self._slider_text)
        shown = label("", "muted", wrap=False)
        shown.setMinimumWidth(48)
        return slider, shown

    def _slider_text(self) -> None:
        if hasattr(self, "tilt"):
            for slider, shown in ((self.pressure, self.pressure_value), (self.curve, self.curve_value),
                                  (self.tilt, self.tilt_value)):
                shown.setText(f"{slider.value() / 100:.2f}")

    # -- state ---------------------------------------------------------
    def options(self) -> dict:
        """Exactly the keys ArtManager.validate_options accepts; touch is always stated."""
        options: dict[str, Any] = {
            "rotation": str(self.rotation.currentData()), "pressure": self.pressure.value() / 100,
            "curve": self.curve.value() / 100, "tilt": self.tilt.value() / 100,
            "touch": self.touch_check.isEnabled() and self.touch_check.isChecked()}
        if self.eraser_check.isChecked() and self.eraser_check.isEnabled():
            options["eraser"] = self.eraser.value()
        if self.display_check.isEnabled() and self.display_check.isChecked() and self.application == "krita":
            options["display"] = True
        return options

    def choose(self, application: str) -> None:
        self.application = application
        self.app_buttons[application].setChecked(True)
        self.refresh_state()

    def bridge(self) -> dict:
        """The art bridge's own capability report: from a running session, else from discovery."""
        running = self.win.art_state.get("capabilities")
        if isinstance(running, dict):
            return running
        found = self.app_info.get(self.application, {}).get("capabilities")
        return found if isinstance(found, dict) else {}

    def _capability(self, key: str) -> bool | None:
        """What is known about one art capability; None when nobody has reported it."""
        bridge = self.bridge()
        for name in (key, key.replace("_", "-"), key.replace("-", "_")):
            if isinstance(bridge.get(name), bool):
                return bridge[name]
        caps = self.win.capabilities
        for name in (f"art-{key}".replace("_", "-"), key.replace("_", "-")):
            if name in caps and name != "art-display":
                return caps[name]
        return None

    def installed(self, application: str) -> bool | None:
        info = self.app_info.get(application)
        return info.get("installed") is True if info else None

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._discovered_for != id(self.win.service):
            self.discover()

    def refresh_state(self) -> None:
        win, name = self.win, APP_NAMES[self.application]
        service = win.service
        active = bool(win.art_state.get("active"))
        if self._discovered_for is not None and self._discovered_for != id(service):
            self.app_info, self._discovered_for, self._plugin_plan = {}, None, None
            if self.isVisible():
                self.discover()
        info = self.app_info.get(self.application) or {}
        installed = self.installed(self.application)
        can_discover = method(service, *ART_APPLICATIONS) is not None
        if service is None:
            state, role = "The device backend is not available.", "error"
        elif win.simulated:
            state, role = f"Simulation: {name} is not looked for or launched.", "muted"
        elif not can_discover:
            state, role = (f"This build cannot tell whether {name} is installed; opening it will say."), "muted"
        elif installed is None:
            state, role = f"{name} has not been looked for yet.", "muted"
        elif installed:
            state, role = f"{name} is installed: {info.get('executable') or 'found'}", "ok"
        else:
            state, role = (f"{name} is not installed. Install it from its own website or your software "
                           "centre, or choose its executable if it lives somewhere unusual."), "warn"
        self.app_state.setText(state)
        set_role(self.app_state, role)
        self.open_button.setText(f"Open {name}")
        self.open_file_button.setText(f"Open file in {name}…")
        launchable = service is not None and (win.simulated or installed is not False)
        self.open_button.setEnabled(launchable)
        self.open_file_button.setEnabled(launchable)
        self.program_button.setVisible(not win.simulated and method(service, *ART_PROGRAM) is not None)
        self.discover_button.setVisible(can_discover and not win.simulated)
        self.sim_note.setVisible(win.simulated)

        extra = win.supports("art-display")
        if not win.connected:
            self.extra_label.setText("Connect to find out")
            set_role(self.extra_label, "muted")
        else:
            self.extra_label.setText("Art tablet mode is active on the tablet" if extra
                                     else "Not active — install it in Setup › Features")
            set_role(self.extra_label, "ok" if extra else "warn")
        bridge = self.bridge()
        for key, value in self.cap_labels.items():
            known = self._capability(key)
            if key == "art_display" and self.application != "krita":
                known = False
            if known is True:
                value.setText("Supported")
                set_role(value, "ok")
            elif known is False:
                value.setText("Not available here")
                set_role(value, "warn")
            else:
                value.setText("Not reported yet")
                set_role(value, "muted")
        pointer_only = bridge.get("pointer_only") is True
        limits = str(bridge.get("limits") or "")
        support = str(bridge.get("tablet_support") or "")
        self.limits_label.setText(
            ("On this computer the pen works as a plain pointer, without pressure or tilt. " if pointer_only
             else "Where pressure is not reported as supported, the pen is a plain pointer; PaperDesk "
                  "does not present pointer-only input as pressure support. ")
            + limits + (f" Validated tablets: {support}." if support else ""))
        self.ink_note.setVisible(any("ink" in str(key).lower() and value is True
                                     for key, value in bridge.items()))

        idle = extra and not active
        pressure_ok = self._capability("pressure") is not False and not pointer_only
        self.start_button.setEnabled(idle)
        self.start_button.setText(f"Start tablet mode with {name}")
        self.stop_button.setEnabled(win.connected and active)
        self.rotation.setEnabled(idle)
        for widget in (self.pressure, self.curve, self.eraser_check):
            widget.setEnabled(idle and pressure_ok)
        self.tilt.setEnabled(idle and pressure_ok and self._capability("tilt") is not False)
        self.eraser.setEnabled(idle and pressure_ok and self.eraser_check.isChecked())
        touch_ok = self._capability("multitouch") is True
        if touch_ok and not self._touch_defaulted:
            self._touch_defaulted = True
            self.touch_check.setChecked(True)
        self.touch_check.setEnabled(idle and touch_ok)
        if active:
            running = APP_NAMES.get(str(win.art_state.get("application")), "the art application")
            self.mode_notice.setText(f"Tablet mode is running with {running}.")
            set_role(self.mode_notice, "ok")
        elif not extra:
            self.mode_notice.setText(win.unsupported_reason("art-display", "art tablet mode"))
            set_role(self.mode_notice, "warn")
        else:
            self.mode_notice.setText("Tablet mode is off.")
            set_role(self.mode_notice, "muted")
        self._refresh_display(extra, active)

    def _refresh_display(self, extra: bool, active: bool) -> None:
        win, service = self.win, self.win.service
        can_plan = method(service, *ART_PLUGIN_PLAN) is not None
        can_copy = method(service, *ART_PLUGIN) is not None
        display_cap = self._capability("art_display")
        steps = (
            ("1  The art tablet extra on the tablet", extra,
             "active" if extra else "not active — Setup › Features installs it, and again after the "
                                    "tablet restarts"),
            ("2  The reMarkable Viewport plugin copied into Krita", None,
             "copy it below; existing plugin files are never overwritten without a backup"),
            ("3  The plugin enabled in Krita, and Krita restarted", self.plugin_ready.isChecked(),
             "confirmed by you" if self.plugin_ready.isChecked() else "you do this in Krita: Settings › "
             "Configure Krita › Python Plugin Manager"))
        for widget, (title, done, detail) in zip(self.display_steps, steps):
            widget.setText(f"<b>{title}</b> — {detail}")
            set_role(widget, "ok" if done else "muted")
        self.plugin_plan_button.setVisible(can_plan)
        self.plugin_plan_button.setEnabled(service is not None)
        self.plugin_button.setEnabled(can_copy and (self._plugin_plan is not None or not can_plan))
        self.plugin_replace_button.setVisible(can_copy and self._plugin_exists)
        if not can_copy:
            self.plugin_note.setText("This build's device backend offers no plugin copy. Copy the plugin "
                                     "by hand from PaperDesk's art resources, or use a build that has it.")
            set_role(self.plugin_note, "warn")
        elif not self.plugin_note.text():
            self.plugin_note.setText("Nothing has been copied. Review where the plugin would go first.")
        if self.application != "krita":
            reason = "Canvas display works with Krita only. Choose Krita above to use it."
        elif not extra:
            reason = win.unsupported_reason("art-display", "the canvas display")
        elif display_cap is False:
            reason = "The art bridge reports that canvas display is not available on this computer."
        elif not self.plugin_ready.isChecked():
            reason = "Tick step 3 once the plugin is enabled in Krita and Krita has been restarted."
        else:
            reason = ""
        self.display_check.setEnabled(not reason and not active)
        if reason and self.display_check.isChecked():
            self.display_check.setChecked(False)
        self.display_note.setText(reason)
        self.display_note.setVisible(bool(reason))

    # -- applications --------------------------------------------------
    def discover(self, quiet: bool = True) -> bool:
        """Ask the art bridge which applications are installed.  Reads local files only."""
        service = self.win.service
        if service is None or method(service, *ART_APPLICATIONS) is None:
            return False
        self._discovered_for = id(service)
        return self.win.run("art-apps", lambda s: method(s, *ART_APPLICATIONS)(), needs_connection=False,
                            quiet=quiet, label="Look for installed art applications",
                            on_done=self._discovered)

    def _discovered(self, result: Any) -> None:
        if isinstance(result, dict):
            result = result.get("applications")
        self.app_info = {str(item.get("id")): item for item in (result or [])
                         if isinstance(item, dict) and item.get("id")}
        self.refresh_state()

    def choose_program(self) -> None:
        app, name = self.application, APP_NAMES[self.application]
        path = self.win.dialogs.open_file(f"Choose the {name} executable", "", "keys")
        if path:
            self.win.run("art-program", lambda s: method(s, *ART_PROGRAM)(app, path), needs_connection=False,
                         label=f"Use {Path(path).name} as {name}",
                         on_done=lambda _r: self.discover())

    def open_app(self) -> None:
        app, name = self.application, APP_NAMES[self.application]
        self.win.run("open-art", lambda s: s.open_art(app, ""), needs_connection=False,
                     key=f"open-art:{app}", label=f"Open {name}")

    def open_file(self) -> None:
        app, name = self.application, APP_NAMES[self.application]
        path = self.win.dialogs.open_file(f"Open file in {name}", "", "pictures")
        if path:
            self.win.run("open-art", lambda s: s.open_art(app, path), needs_connection=False,
                         key=f"open-art:{app}:{path}", label=f"Open {Path(path).name} in {name}")

    # -- Krita plugin --------------------------------------------------
    def plan_plugin(self) -> None:
        self.win.run("art-plugin-plan", lambda s: method(s, *ART_PLUGIN_PLAN)(), needs_connection=False,
                     label="Check where the Krita plugin would be copied", on_done=self._plugin_planned)

    def _plugin_planned(self, plan: Any) -> None:
        self._plugin_plan = plan if isinstance(plan, dict) else {}
        files = ", ".join(str(item) for item in self._plugin_plan.get("files") or [])
        self.plugin_note.setText(
            f"Would copy {files or 'the plugin'} into {self._plugin_plan.get('destination') or 'Krita’s plugin folder'}. "
            f"{self._plugin_plan.get('message') or ''} Nothing has been copied yet.")
        set_role(self.plugin_note, "muted")
        self.refresh_state()

    def copy_plugin(self, replace: bool) -> None:
        install = method(self.win.service, *ART_PLUGIN)
        if install is None:
            return
        where = str((self._plugin_plan or {}).get("destination") or "Krita's plugin folder")
        target = "The simulation changes no files. " if self.win.simulated else ""
        if not self.win.dialogs.confirm(
                "Replace the Krita plugin?" if replace else "Copy the plugin to Krita?",
                f"{target}PaperDesk copies the reMarkable Viewport plugin into {where}. "
                + ("The existing plugin is backed up first, then replaced. " if replace else
                   "If a plugin is already there, nothing is changed. ")
                + "It does not enable the plugin, edit Krita's settings or restart Krita.",
                "Replace with backup" if replace else "Copy plugin", danger=replace):
            return

        def call(service: Any) -> Any:
            copy = method(service, *ART_PLUGIN)
            return copy(replace=True) if replace and accepts(copy, "replace") else copy()
        self.win.run("art-plugin", call, needs_connection=False, mutation=True,
                     label="Copy the Krita plugin", on_done=self._plugin_copied,
                     on_error=self._plugin_failed)

    def _plugin_copied(self, result: Any) -> None:
        info = result if isinstance(result, dict) else {}
        failed = info.get("ok") is False
        backup = f" The previous plugin was kept in {info['backup']}." if info.get("backup") else ""
        self.plugin_note.setText(str(info.get("message") or ("The plugin was not copied." if failed
                                                             else "Plugin copied.")) + backup)
        set_role(self.plugin_note, "error" if failed else "ok")
        self._plugin_exists = False
        self.refresh_state()

    def _plugin_failed(self, exc: BaseException) -> None:
        text = describe_error(exc)
        self._plugin_exists = "already exists" in text.lower()
        self.plugin_note.setText(f"Nothing was copied. {text}")
        set_role(self.plugin_note, "error")
        self.refresh_state()

    # -- tablet mode ---------------------------------------------------
    def start_mode(self) -> None:
        if not self.win.supports("art-display"):
            self.win.notify("warn", self.win.unsupported_reason("art-display", "art tablet mode"))
            return
        app, options = self.application, self.options()
        self.win.run("start-art", lambda s: s.start_art(app, options), mutation=True, key="art-mode",
                     label=f"Start tablet mode with {APP_NAMES[app]}",
                     on_done=lambda result: self._mode_result(result, app, starting=True))

    def stop_mode(self) -> None:
        self.win.run("stop-art", lambda s: s.stop_art(), mutation=True, key="art-mode",
                     label="Stop tablet mode", on_error=self._stop_failed,
                     on_done=lambda result: self._mode_result(result, "", starting=False))

    def _mode_result(self, result: Any, application: str, starting: bool) -> None:
        info = result if isinstance(result, dict) else {}
        ok = info.get("ok") is not False
        if isinstance(info.get("active"), bool):
            active = info["active"]
        elif starting:
            active = ok and str(info.get("state") or "").lower() not in _STOPPED
        else:
            active = not ok
        state = {"active": active,
                 "application": application or self.win.art_state.get("application", "")}
        capabilities = info.get("capabilities", self.win.art_state.get("capabilities"))
        if isinstance(capabilities, dict):
            state["capabilities"] = capabilities
        self.win.art_state = state
        self.report.set_data(info or None)
        self.report.setVisible(bool(info))
        self.win._refresh()

    def _stop_failed(self, _exc: BaseException) -> None:
        self.mode_notice.setText("Tablet mode could not be confirmed as stopped. " + EXIT_HELP)
        set_role(self.mode_notice, "error")
