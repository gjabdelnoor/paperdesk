"""Home: what is connected, what it can do, and the next sensible step."""
from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from . import theme
from .services import FEATURES
from .widgets import Card, Page, StateDot, button, flow, label, set_role


class HomePage(Page):
    def __init__(self, win):
        super().__init__(win, "No tablet connected")
        self.connect_button = button("Connect to tablet", "primary",
                                     "Connect with the details from Setup (Ctrl+K)")
        self.setup_button = button("First-time setup", None, "Step-by-step guide for a new tablet")
        self.sim_button = button("Try the simulator", None,
                                 "Explore PaperDesk with a pretend tablet; no device or network is used")
        self.disconnect_button = button("Disconnect")
        self.refresh_button = button("Refresh status", None, "Ask the tablet for its current state (F5)")
        self.redraw_button = button("Redraw tablet screen", None,
                                    "Ask the tablet to refresh its display (Shift+F5)")
        self.redraw_button.clicked.connect(win.redraw_tablet)
        self.connect_button.clicked.connect(win.request_connect)
        self.setup_button.clicked.connect(lambda: win.navigate("setup", "developer"))
        self.sim_button.clicked.connect(win.start_simulation)
        self.disconnect_button.clicked.connect(lambda: win.disconnect_tablet())
        self.refresh_button.clicked.connect(win.refresh_status)
        self.col.addWidget(flow(self.connect_button, self.setup_button, self.sim_button,
                                self.refresh_button, self.redraw_button, self.disconnect_button))

        self.device_card = Card("Tablet")
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        self.fields: dict[str, object] = {}
        for row, (key, text) in enumerate((("model", "Model"), ("firmware", "Firmware"),
                                           ("host", "Address"), ("transport", "Connection"),
                                           ("document", "Open document"), ("message", "Tablet says"))):
            grid.addWidget(label(text, "muted", wrap=False), row, 0)
            value = label("—", None, selectable=True)
            value.setAccessibleName(text)
            grid.addWidget(value, row, 1)
            self.fields[key] = value
        self.device_card.body.addLayout(grid)
        self.col.addWidget(self.device_card)

        self.caps_card = Card("What this tablet can do",
                              "Document actions need nothing installed. Extras depend on the tablet's "
                              "firmware and are added in Setup.")
        self.cap_rows: dict[str, tuple[StateDot, object]] = {}
        rows = [("basic", "Documents, upload, download and PDF export"),
                ("screenshot", "Tablet screenshots")] + [(f, n) for f, n, _ in FEATURES]
        for key, text in rows:
            line = QHBoxLayout()
            line.setSpacing(9)
            dot = StateDot()
            state = label("", "muted", wrap=False)
            state.setAccessibleName(f"{text} availability")
            line.addWidget(dot)
            line.addWidget(label(text, None, wrap=False))
            line.addStretch(1)
            line.addWidget(state)
            self.caps_card.body.addLayout(line)
            self.cap_rows[key] = (dot, state)
        self.col.addWidget(self.caps_card)

        go = Card("Where to next")
        links = QGridLayout()
        links.setHorizontalSpacing(24)
        links.setVerticalSpacing(10)
        entries = (("library", "Library", "Send, fetch and export documents."),
                   ("type", "Type", "Write on the tablet from this keyboard."),
                   ("art", "Art", "Draw in Krita, Inkscape or Blender."),
                   ("setup", "Setup", "Connect, add extras, diagnose, undo."))
        for index, (page_id, text, about) in enumerate(entries):
            cell = QWidget()
            box = QVBoxLayout(cell)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(1)
            link = button(text, "link", name=f"Go to {text}")
            link.clicked.connect(lambda _=False, p=page_id: win.navigate(p))
            box.addWidget(link)
            box.addWidget(label(about, "caption"))
            links.addWidget(cell, index // 2, index % 2)
        links.setColumnStretch(0, 1)
        links.setColumnStretch(1, 1)
        go.body.addLayout(links)
        self.col.addWidget(go)
        self.col.addStretch(1)

    def refresh_state(self) -> None:
        win, mode, status = self.win, self.win.connection_mode, self.win.status
        connected = win.connected
        if mode == "unavailable":
            title, intro = "Device backend unavailable", win.backend_error
        elif mode == "connecting":
            title, intro = "Connecting…", "Waiting for the tablet to answer. Nothing else is started meanwhile."
        elif mode == "simulation":
            title = f"Simulation — {win.device_name()}" if connected else "Simulation — not started"
            intro = ("You are looking at a pretend tablet that lives in a temporary folder on this "
                     "computer. Everything works as it would on a tablet, and nothing leaves this machine.")
        elif connected:
            title, intro = f"{win.device_name()} connected", "Ready. PaperDesk only acts when you ask it to."
        elif win.connection_lost:
            title = "Connection lost"
            intro = "Nothing was retried. When the tablet is awake and plugged in, connect again."
        else:
            title = "No tablet connected"
            intro = ("PaperDesk starts disconnected and never connects, installs or retries on its own. "
                     "New here? First-time setup walks you through it, or try the simulator first.")
        self.title_label.setText(title)
        self.intro_label.setText(intro)
        self.intro_label.setVisible(True)

        sim = mode == "simulation"
        self.connect_button.setVisible(not connected and mode != "unavailable")
        self.connect_button.setEnabled(mode != "connecting")
        self.connect_button.setText("Start simulated tablet" if sim else "Connect to tablet")
        self.setup_button.setVisible(mode in ("disconnected", "connecting"))
        self.sim_button.setVisible(mode in ("disconnected", "unavailable"))
        self.refresh_button.setVisible(connected)
        self.redraw_button.setVisible(connected)
        self.redraw_button.setEnabled(win.allows("refresh"))
        self.disconnect_button.setVisible(connected and not sim)
        self.layout().activate()

        self.device_card.setVisible(connected)
        if connected:
            for key in ("model", "firmware", "host", "transport", "message"):
                self.fields[key].setText(str(status.get(key) or "—"))
            doc = status.get("active_document") or {}
            if isinstance(doc, dict) and doc.get("name"):
                where = f" — page {doc.get('page')} of {doc.get('pages')}" if doc.get("pages") else ""
                self.fields["document"].setText(f"{doc['name']}{where}")
            else:
                self.fields["document"].setText("None open")

        for key, (dot, state) in self.cap_rows.items():
            if not connected:
                dot.set_state(theme.FAINT, False)
                state.setText("Connect to find out")
                set_role(state, "muted")
            elif key == "basic" or (key == "screenshot" and win.allows(key)) or win.supports(key):
                dot.set_state(theme.OK, True)
                state.setText("Ready")
                set_role(state, "ok")
            else:
                dot.set_state(theme.WARN, False)
                state.setText("Not available yet" if key in win.capabilities else "Not reported")
                set_role(state, "warn")
