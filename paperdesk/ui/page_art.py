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
                ("Native multi-touch", "native_multitouch"), ("Gestures in Krita's canvas", "canvas_gestures"),
                ("Canvas on the tablet", "art_display")]
# The art bridge's own input methods, worded for the computer it reports running on.
INPUT_MODES = ("auto", "pen", "mouse")
MODE_TEXT = {
    "": ("Automatic", "Pen", "Mouse pointer"),
    "linux": ("Automatic — pen tablet", "Pen tablet", "Mouse pointer — not available on Linux"),
    "windows": ("Automatic — Windows Ink when available", "Pen — Windows Ink",
                "Mouse pointer — no pressure or tilt"),
    "mac": ("Automatic — plain pointer", "Pen — experimental pressure", "Mouse pointer"),
}
MODE_HELP = {
    ("linux", "auto"): "Linux receives the tablet as a pen tablet with pressure and tilt.",
    ("linux", "pen"): "Linux receives the tablet as a pen tablet with pressure and tilt.",
    ("windows", "auto"): "Uses Windows Ink for pressure, tilt and eraser when this computer offers it; "
                         "otherwise the pen moves the pointer.",
    ("windows", "pen"): "Asks for Windows Ink pen input.",
    ("windows", "mouse"): "The pen only moves the pointer. Touch gestures then need the Krita canvas display.",
    ("mac", "auto"): "On macOS the pen moves the pointer. Pressure and tilt are not sent.",
    ("mac", "mouse"): "On macOS the pen moves the pointer. Pressure and tilt are not sent.",
    ("mac", "pen"): "Experimental: pressure and tilt are sent as macOS tablet events. Whether an art "
                    "application receives them has not been verified, so try it on a scratch canvas first.",
}
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
        tune.addWidget(label("Input method", None, wrap=False), 0, 0)
        self.input_mode = QComboBox()
        self.input_mode.setAccessibleName("Art input method")
        for value, text in zip(INPUT_MODES, MODE_TEXT[""]):
            self.input_mode.addItem(text, value)
        self.input_mode.currentIndexChanged.connect(self.refresh_state)
        tune.addWidget(self.input_mode, 0, 1, 1, 2)
        self.mode_help = label("", "caption")
        self.mode_help.setAccessibleName("Input method explanation")
        tune.addWidget(self.mode_help, 1, 1, 1, 2)
        self.mac_pen_ack = QCheckBox("I understand macOS pen pressure is experimental and unverified")
        self.mac_pen_ack.setAccessibleName("Accept experimental macOS pen pressure")
        self.mac_pen_ack.toggled.connect(self.refresh_state)
        tune.addWidget(self.mac_pen_ack, 2, 1, 1, 2)
        tune.addWidget(label("Rotation", None, wrap=False), 3, 0)
        self.rotation = QComboBox()
        self.rotation.setAccessibleName("Tablet rotation")
        for value, text in ROTATIONS:
            self.rotation.addItem(text, value)
        tune.addWidget(self.rotation, 3, 1, 1, 2)
        self.pressure, self.pressure_value = self._slider("Pressure", 5, 100, 50)
        self.curve, self.curve_value = self._slider("Pressure curve", 20, 300, 80)
        self.tilt, self.tilt_value = self._slider("Tilt strength", 0, 300, 0)
        for row, (text, slider, shown) in enumerate((("Pressure", self.pressure, self.pressure_value),
                                                     ("Pressure curve", self.curve, self.curve_value),
                                                     ("Tilt strength", self.tilt, self.tilt_value)), start=4):
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
        eraser_row.setSpacing(8)
        eraser_row.addWidget(self.eraser)
        eraser_row.addWidget(label("4 to 200", "caption", wrap=False))
        eraser_row.addStretch(1)
        tune.addWidget(self.eraser_check, 7, 0)
        tune.addLayout(eraser_row, 7, 1, 1, 2)
        mode.body.addLayout(tune)
        self.touch_check = QCheckBox("Forward touch gestures (pan, zoom, rotate)")
        self.touch_check.setAccessibleName("Forward touch gestures")
        mode.add(self.touch_check)
        self.touch_note = mode.add(label("", "caption"))
        self.touch_note.setAccessibleName("Touch gesture availability")
        self.host_note = mode.add(label("", "warn"))
        self.host_note.setAccessibleName("This computer's art input setup")
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
                    "Optional, Krita only, on Linux, macOS and Windows. Krita's plugin talks to PaperDesk "
                    "on this computer. It needs three things, and PaperDesk never does the second or "
                    "third for you without being asked.")
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
        self.plugin_check_button = button("Check plugin", None,
                                          "Ask Krita's plugin on this computer whether it answers; "
                                          "nothing is changed and the tablet is not involved")
        self.plugin_check_button.clicked.connect(self.check_plugin)
        self.plugin_probe_note = label("Not checked yet. Krita must be open with the plugin enabled for "
                                       "it to answer.", "muted")
        self.plugin_probe_note.setAccessibleName("Krita plugin check result")
        probe = QHBoxLayout()
        probe.setSpacing(12)
        probe.addWidget(self.plugin_check_button, 0, Qt.AlignmentFlag.AlignTop)
        probe.addWidget(self.plugin_probe_note, 1)
        card.body.addLayout(probe)
        self.display_check = QCheckBox("Show the Krita canvas on the tablet when tablet mode starts")
        self.display_check.setAccessibleName("Show the Krita canvas on the tablet")
        self.display_check.toggled.connect(self.refresh_state)
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
        """Exactly the keys ArtManager.validate_options accepts; touch is always stated.

        The input method is sent only when one was chosen, so Automatic stays the bridge's default.
        """
        options: dict[str, Any] = {
            "rotation": str(self.rotation.currentData()), "pressure": self.pressure.value() / 100,
            "curve": self.curve.value() / 100, "tilt": self.tilt.value() / 100,
            "touch": self.touch_check.isEnabled() and self.touch_check.isChecked()}
        if self.input_method() != "auto":
            options["mode"] = self.input_method()
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
        if isinstance(running, dict) and self.win.art_state.get("active"):
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

    def input_method(self) -> str:
        return str(self.input_mode.currentData() or "auto")

    def host(self) -> str:
        """Which kind of computer the art bridge says it runs on: linux, windows, mac or unknown."""
        bridge = self.bridge()
        named = str(bridge.get("platform") or "")
        if named.startswith("linux"):
            return "linux"
        if named in ("darwin", "win32"):
            return "mac" if named == "darwin" else "windows"
        if bridge.get("experimental_pressure") is True:
            return "mac"
        if any(bridge.get(key) is True for key in ("windows_ink_available", "windows_ink", "pointer_only")):
            return "windows"
        return "linux" if "linux_driver_installed" in bridge else ""

    def needs_pen_opt_in(self) -> bool:
        return self.host() == "mac" and self.input_method() == "pen"

    def _native_touch(self) -> bool | None:
        known = self._capability("native_multitouch")
        return self._capability("multitouch") if known is None else known

    def _host_problem(self) -> str:
        """What this computer still lacks for pen input, in the bridge's own terms."""
        bridge, host = self.bridge(), self.host()
        if host == "linux" and bridge.get("linux_driver_installed") is False:
            return ("This computer has no Linux pen driver yet. Install PaperDesk's optional art "
                    "support (the evdev package); nothing is installed automatically.")
        if host == "linux" and bridge.get("linux_input_permission") is False:
            return ("This computer does not let your user create input devices (/dev/uinput is not "
                    "writable). Your distribution's udev rules grant that; PaperDesk never changes "
                    "permissions or runs as administrator.")
        return ""

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
        bridge, host, chosen = self.bridge(), self.host(), self.input_method()
        texts = MODE_TEXT[host]
        for index, text in enumerate(texts):
            self.input_mode.setItemText(index, text)
        mouse_item = self.input_mode.model().item(2)
        mouse_item.setEnabled(host != "linux")
        if host == "linux" and chosen == "mouse":
            self.input_mode.setCurrentIndex(0)  # re-enters refresh_state with Automatic
            return
        self.mode_help.setText(MODE_HELP.get((host, chosen), "The art bridge chooses the input it can "
                                                             "provide on this computer."))
        opt_in = self.needs_pen_opt_in()
        self.mac_pen_ack.setVisible(opt_in)
        if not opt_in and self.mac_pen_ack.isChecked():
            self.mac_pen_ack.setChecked(False)
        experimental = opt_in and bridge.get("experimental_pressure") is True
        for key, value in self.cap_labels.items():
            known = self._native_touch() if key == "native_multitouch" else self._capability(key)
            if key in ("art_display", "canvas_gestures") and self.application != "krita":
                known = False
            if key in ("pressure", "tilt", "native_multitouch") and chosen == "mouse":
                known = False
            if key in ("pressure", "tilt") and experimental:
                value.setText("Experimental — not verified in an art application")
                set_role(value, "warn")
            elif known is True:
                value.setText("Reported available")
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
            "“Reported available” is what the art bridge says this computer can do. It is not a test "
            "with a physical tablet. "
            + ("Here the pen works as a plain pointer unless the input method below says otherwise. "
               if pointer_only else "")
            + (f"Validated tablet: {support}." if support else ""))
        self.limits_label.setToolTip(limits)  # the bridge's full statement, also in its start report
        self.limits_label.setAccessibleDescription(limits)
        self.ink_note.setVisible(chosen != "mouse" and any(
            "ink" in str(key).lower() and value is True for key, value in bridge.items()))
        problem = self._host_problem()
        self.host_note.setText(problem)
        self.host_note.setVisible(bool(problem))

        idle = extra and not active
        if chosen == "mouse":
            pressure_ok = False
        elif host == "mac":
            pressure_ok = chosen == "pen"
        else:
            pressure_ok = self._capability("pressure") is not False and not pointer_only
        self.input_mode.setEnabled(idle)
        self.mac_pen_ack.setEnabled(idle)
        self.start_button.setEnabled(idle and (not opt_in or self.mac_pen_ack.isChecked()))
        self.start_button.setText(f"Start tablet mode with {name}")
        self.stop_button.setEnabled(win.connected and active)
        self.rotation.setEnabled(idle)
        for widget in (self.pressure, self.curve, self.eraser_check):
            widget.setEnabled(idle and pressure_ok)
        self.tilt.setEnabled(idle and pressure_ok and (experimental or self._capability("tilt") is not False))
        self.eraser.setEnabled(idle and pressure_ok and self.eraser_check.isChecked())
        if active:
            running = APP_NAMES.get(str(win.art_state.get("application")), "the art application")
            self.mode_notice.setText(f"Tablet mode is running with {running}.")
            set_role(self.mode_notice, "ok")
        elif not extra:
            self.mode_notice.setText(win.unsupported_reason("art-display", "art tablet mode"))
            set_role(self.mode_notice, "warn")
        elif opt_in and not self.mac_pen_ack.isChecked():
            self.mode_notice.setText("Tick the experimental pen box above to start with pen pressure, "
                                     "or choose Automatic to use the plain pointer.")
            set_role(self.mode_notice, "warn")
        else:
            self.mode_notice.setText("Tablet mode is off.")
            set_role(self.mode_notice, "muted")
        self._refresh_display(extra, active)
        self._refresh_touch(idle, active)

    def _refresh_touch(self, idle: bool, active: bool) -> None:
        """Touch is native multi-touch where the bridge has it, else gestures through Krita's canvas."""
        native_ok = self._native_touch() is True and self.input_method() != "mouse"
        display_on = self.application == "krita" and self.display_check.isChecked()
        can_gesture = self._capability("canvas_gestures") is True
        touch_ok = native_ok or (can_gesture and display_on)
        if native_ok and not self._touch_defaulted:
            self._touch_defaulted = True
            self.touch_check.setChecked(True)
        if not touch_ok and not active and self.touch_check.isChecked():
            self.touch_check.setChecked(False)
        self.touch_check.setEnabled(idle and touch_ok)
        if native_ok:
            note = "Touch reaches this computer as native multi-touch."
        elif can_gesture and display_on:
            note = "Touch gestures go to Krita through the canvas display, not to other applications."
        elif can_gesture:
            note = ("This computer has no native multi-touch for this input method. Touch gestures work "
                    "in Krita once the canvas display below is switched on.")
        else:
            note = "Touch gestures are not available on this computer."
        self.touch_note.setText(note)

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
        can_probe = callable(getattr(service, "art_plugin_probe", None))
        self.plugin_check_button.setVisible(can_probe)
        self.plugin_probe_note.setVisible(can_probe)
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
            if self.host() == "mac" and self.bridge().get("mac_input_permission") is False:
                reason = ("macOS has not yet allowed PaperDesk to send input, which the canvas display "
                          "needs. Allow it in System Settings › Privacy & Security, then reopen this page.")
            else:
                reason = self._host_problem() or ("The art bridge reports that canvas display is not "
                                                  "available on this computer.")
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

    def check_plugin(self) -> None:
        """One read-only question to Krita's plugin on this computer; never repeated by itself."""
        if self.win.run("art-plugin-probe", lambda s: s.art_plugin_probe(), needs_connection=False,
                        label="Check the Krita plugin", on_done=self._plugin_probed,
                        on_error=self._plugin_probe_failed):
            self.plugin_probe_note.setText("Checking…")
            set_role(self.plugin_probe_note, "muted")

    def _plugin_probed(self, result: Any) -> None:
        info = result if isinstance(result, dict) else {}
        failed = info.get("ok") is False
        text = str(info.get("message") or ("The plugin did not confirm it is ready." if failed
                                           else "Krita's plugin answered."))
        if info.get("simulated"):
            text += " Nothing was asked of a real Krita."
        elif not failed:
            text += " Tablet mode asks again when it starts."
        self.plugin_probe_note.setText(text)
        set_role(self.plugin_probe_note, "error" if failed else "muted" if info.get("simulated") else "ok")

    def _plugin_probe_failed(self, exc: BaseException) -> None:
        self.plugin_probe_note.setText(f"The plugin did not answer. {describe_error(exc)}")
        set_role(self.plugin_probe_note, "error")

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
        if self.needs_pen_opt_in() and not self.mac_pen_ack.isChecked():
            self.win.notify("warn", "Not started: tablet mode. Experimental macOS pen pressure needs "
                                    "your explicit tick on the Art page.")
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
