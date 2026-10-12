"""Type › Figure as ink: an experimental figure drawn as pen strokes, behind explicit checks.

Nothing here moves the pen without three deliberate steps in order: a local
preview, a check of the tablet's pen, and a short calibration mark the user
has looked at on the physical page.  A stopped drawing is reported as partial
and is never replayed.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLineEdit, QProgressBar,
                               QSpinBox, QVBoxLayout, QWidget)

from .services import INK_PREPARE, INK_PROBE, INK_STREAM, accepts, describe_error, method
from .widgets import Card, button, flow, label, set_role

FIGURE_FILTER = "Figures (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.svg);;All files (*)"
MODES = [("diagram", "Diagram — outlines of dark shapes"), ("photo", "Photo — edges only"),
         ("hatch", "Hatch — horizontal shading lines")]
EDGES = ("Left", "Top", "Right", "Bottom")
PARTIAL_HELP = ("Marks already drawn stay on the page; undo on the tablet removes them stroke by stroke. "
                "PaperDesk does not draw them again.")


def local_prepare():
    """The bounded local converter.  It reads a file and writes a preview; no tablet is involved."""
    from ..figure_tools import prepare_ink
    return prepare_ink


class InkPanel(QWidget):
    progressed = Signal(int, int)  # emitted on the worker thread, delivered here on the GUI thread

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.source = ""
        self.prepared: dict | None = None
        self._prepared_key: tuple | None = None
        self.axis_ratio: float | None = None
        self.calibrated = False
        self.drawing = False
        self._cancel = threading.Event()
        self._generation = win.simulation_generation
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)

        figure = Card("Figure as ink — experimental",
                      "Turns a simple figure into pen strokes on the page open on the tablet. The strokes "
                      "are real ink: they cannot be taken back in one step, and a drawing that stops "
                      "part-way leaves the marks it had made.")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.source_edit = QLineEdit()
        self.source_edit.setReadOnly(True)
        self.source_edit.setPlaceholderText("No figure chosen")
        self.source_edit.setAccessibleName("Figure file")
        self.choose_button = button("Choose figure…", None, "A PNG, JPEG or simple outline SVG")
        self.choose_button.clicked.connect(self.choose_source)
        row.addWidget(self.source_edit, 1)
        row.addWidget(self.choose_button)
        figure.body.addLayout(row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.addWidget(label("Placement", None, wrap=False), 0, 0)
        self.edges: list[QSpinBox] = []
        place = QHBoxLayout()
        place.setSpacing(6)
        for name, value in zip(EDGES, (1000, 1000, 9000, 9000)):
            spin = QSpinBox()
            spin.setRange(0, 10000)
            spin.setSingleStep(250)
            spin.setValue(value)
            spin.setAccessibleName(f"Placement {name.lower()} edge")
            spin.setToolTip(f"{name} edge of the area, from 0 to 10000 across the page")
            spin.valueChanged.connect(self._changed)
            place.addWidget(label(name, "caption", wrap=False))
            place.addWidget(spin)
            self.edges.append(spin)
        place.addStretch(1)
        grid.addLayout(place, 0, 1)
        tune = QHBoxLayout()
        tune.setSpacing(6)
        self.threshold = QSpinBox()
        self.threshold.setRange(1, 254)
        self.threshold.setValue(160)
        self.threshold.setAccessibleName("Darkness threshold")
        self.threshold.setToolTip("Pixels darker than this become ink (1 to 254)")
        self.resolution = QSpinBox()
        self.resolution.setRange(16, 256)
        self.resolution.setValue(160)
        self.resolution.setAccessibleName("Tracing resolution")
        self.resolution.setToolTip("Size the figure is traced at (16 to 256); lower means fewer strokes")
        self.mode = QComboBox()
        self.mode.setAccessibleName("Tracing mode")
        for value, text in MODES:
            self.mode.addItem(text, value)
        self.threshold.valueChanged.connect(self._changed)
        self.resolution.valueChanged.connect(self._changed)
        self.mode.currentIndexChanged.connect(self._changed)
        for text, widget in (("Threshold", self.threshold), ("Resolution", self.resolution)):
            tune.addWidget(label(text, "caption", wrap=False))
            tune.addWidget(widget)
        tune.addWidget(self.mode, 1)
        grid.addWidget(label("Tracing", None, wrap=False), 1, 0)
        grid.addLayout(tune, 1, 1)
        grid.setColumnStretch(1, 1)
        figure.body.addLayout(grid)
        figure.add(label("Placement runs from 0 to 10000 across and down the page. The figure keeps its "
                         "shape and is centred inside that area. SVG files must be plain black outlines.",
                         "caption"))
        self.prepare_button = button("Prepare preview", "primary",
                                     "Trace the figure on this computer; nothing is sent to the tablet")
        self.prepare_button.clicked.connect(self.prepare)
        figure.add(flow(self.prepare_button))
        self.preview = label("", None)
        self.preview.setAccessibleName("Ink preview")
        self.preview.setVisible(False)
        figure.add(self.preview)
        self.preview_note = figure.add(label("", "muted"))
        self.preview_note.setAccessibleName("Preview state")
        column.addWidget(figure)

        ink = Card("Check the pen and the page, then draw",
                   "Three steps, in order. Open the right page on the tablet, choose its pen tool, and "
                   "keep the placement area empty.")
        self.probe_button = button("1  Check the tablet's pen…", None,
                                   "Copies PaperDesk's pen helper into its own folder on the tablet and "
                                   "reads the pen's range. Draws nothing.")
        self.probe_button.clicked.connect(self.probe)
        self.probe_note = label("", "muted")
        self.probe_note.setAccessibleName("Pen check result")
        self.mark_button = button("2  Draw calibration mark…", None,
                                  "Draws one small corner mark at the top-left of the placement area")
        self.mark_button.clicked.connect(self.draw_mark)
        self.mark_note = label("", "muted")
        self.mark_note.setAccessibleName("Calibration mark result")
        self.confirm = QCheckBox("I looked at the tablet: the corner mark sits at the top-left of the "
                                 "area where I want the figure, on the right page")
        self.confirm.setAccessibleName("Calibration mark inspected on the tablet")
        self.confirm.toggled.connect(self.refresh_state)
        self.draw_button = button("3  Draw figure as ink…", "primary")
        self.draw_button.clicked.connect(self.draw)
        self.cancel_button = button("Stop drawing", "danger",
                                    "Stops sending strokes. Marks already drawn stay on the page.")
        self.cancel_button.clicked.connect(self.cancel)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("Ink progress")
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        self.outcome = label("", "muted")
        self.outcome.setAccessibleName("Ink outcome")
        for widget in (flow(self.probe_button), self.probe_note, flow(self.mark_button), self.mark_note,
                       self.confirm, flow(self.draw_button, self.cancel_button), self.progress,
                       self.outcome):
            ink.add(widget)
        self.notice = ink.add(label("", "warn"))
        self.notice.setAccessibleName("Figure as ink availability")
        column.addWidget(ink)
        column.addStretch(1)
        self.progressed.connect(self._on_progress)

    # -- state ---------------------------------------------------------
    def placement(self) -> tuple[int, int, int, int]:
        return tuple(spin.value() for spin in self.edges)  # type: ignore[return-value]

    def _key(self) -> tuple:
        return (self.source, self.placement(), self.threshold.value(), self.resolution.value(),
                self.mode.currentData(), self.axis_ratio)

    def preview_current(self) -> bool:
        return self.prepared is not None and self._prepared_key == self._key()

    def _placement_error(self) -> str:
        left, top, right, bottom = self.placement()
        return "" if left < right and top < bottom else \
            "The right edge must be past the left edge, and the bottom edge below the top edge."

    def _changed(self, *_args) -> None:
        if not self.preview_current():
            self._drop_calibration()
        self.refresh_state()

    def _drop_calibration(self) -> None:
        self.calibrated = False
        if self.confirm.isChecked():
            self.confirm.setChecked(False)

    def reset(self) -> None:
        """Forget everything measured on a tablet; used when the tablet behind the window changes."""
        self.axis_ratio = None
        self.prepared, self._prepared_key = None, None
        self._drop_calibration()
        self.preview.setVisible(False)
        for note in (self.probe_note, self.mark_note, self.outcome):
            note.setText("")

    def refresh_state(self, *_args) -> None:
        win = self.win
        if not win.connected or self._generation != win.simulation_generation:
            self._generation = win.simulation_generation
            if self.axis_ratio is not None or self.calibrated:
                self.reset()
        service = win.service
        can_probe = method(service, *INK_PROBE) is not None
        stream = method(service, *INK_STREAM)
        idle = not self.drawing
        bad_place = self._placement_error()
        for widget in (self.choose_button, self.threshold, self.resolution, self.mode, *self.edges):
            widget.setEnabled(idle)
        self.prepare_button.setEnabled(idle and bool(self.source) and not bad_place
                                       and service is not None)
        current = self.preview_current()
        if bad_place:
            note, role = bad_place, "error"
        elif not self.source:
            note, role = "Choose a figure, then prepare a preview. Preparing happens on this computer.", "muted"
        elif self.prepared is None:
            note, role = "No preview yet.", "muted"
        elif not current:
            note, role = "The settings changed. Prepare the preview again before drawing.", "warn"
        else:
            note, role = self._stats_text(), "muted"
        self.preview_note.setText(note)
        set_role(self.preview_note, role)

        if not win.connected:
            reason = "Connect to a tablet to check its pen and draw. The preview works without one."
        elif not can_probe or stream is None:
            reason = ("This build's device backend offers no figure-as-ink calls, so only the preview is "
                      "available.")
        else:
            reason = ""
        self.notice.setText(reason)
        self.notice.setVisible(bool(reason))
        usable = not reason and idle
        self.probe_button.setEnabled(usable)
        self.mark_button.setEnabled(usable and self.axis_ratio is not None and current
                                    and not self.calibrated)
        self.confirm.setEnabled(usable and self.calibrated and current)
        self.draw_button.setEnabled(usable and self.calibrated and current and self.confirm.isChecked())
        self.cancel_button.setVisible(self.drawing)
        self.cancel_button.setEnabled(self.drawing and stream is not None and accepts(stream, "cancelled")
                                      and not self._cancel.is_set())
        if win.connected and not reason and not self.probe_note.text():
            self.probe_note.setText(("Simulation: the pen check is simulated. " if win.simulated else "")
                                    + "The pen has not been checked in this session.")
            set_role(self.probe_note, "muted")

    def _stats_text(self) -> str:
        stats = (self.prepared or {}).get("stats")
        if not isinstance(stats, dict):
            return "Preview ready."
        seconds = stats.get("minimum_seconds")
        timing = f", at least {seconds:.0f} s to draw" if isinstance(seconds, (int, float)) else ""
        return (f"Preview ready: {stats.get('strokes', '?')} strokes, {stats.get('points', '?')} points"
                f"{timing}. The red outline is the placement area."
                + ("" if self.axis_ratio is not None else
                   " It uses a typical pen shape until the tablet's pen has been checked."))

    # -- actions -------------------------------------------------------
    def choose_source(self) -> None:
        path = self.win.dialogs.open_file("Choose a figure to draw as ink", FIGURE_FILTER, "pictures")
        if path:
            self.source = path
            self.source_edit.setText(path)
            self._changed()

    def prepare(self) -> bool:
        if not self.source or self._placement_error():
            return False
        key = self._key()
        source, ratio = self.source, self.axis_ratio
        target = str(self.win.temp_dir() / f"ink-preview-{uuid.uuid4().hex[:10]}.png")
        options = {"placement": self.placement(), "threshold": self.threshold.value(),
                   "resolution": self.resolution.value(), "mode": str(self.mode.currentData())}

        def call(service: Any) -> Any:
            prepare = method(service, *INK_PREPARE) or local_prepare()
            extra = {name: value for name, value in options.items() if accepts(prepare, name)}
            if ratio is not None and accepts(prepare, "axis_ratio"):
                extra["axis_ratio"] = ratio
            return prepare(source, target, **extra)
        return self.win.run("ink-prepare", call, needs_connection=False,
                            label=f"Prepare an ink preview of {Path(source).name}",
                            on_done=lambda result: self._prepared(result, key, target))

    def _prepared(self, result: Any, key: tuple, target: str) -> None:
        info = result if isinstance(result, dict) else {}
        if info.get("ok") is False or not info.get("strokes"):
            self.prepared, self._prepared_key = None, None
            self.preview.setVisible(False)
        else:
            self.prepared, self._prepared_key = info, key
            pixmap = QPixmap(str(info.get("preview") or target))
            if not pixmap.isNull():
                self.preview.setPixmap(pixmap.scaled(QSize(300, 300), Qt.AspectRatioMode.KeepAspectRatio,
                                                     Qt.TransformationMode.SmoothTransformation))
            self.preview.setVisible(not pixmap.isNull())
        self._drop_calibration()
        self.refresh_state()

    def probe(self) -> None:
        if not self.win.connected:
            return
        where = "the simulated tablet" if self.win.simulated else "the tablet"
        if not self.win.dialogs.confirm(
                "Check the tablet's pen?",
                f"PaperDesk copies its small pen helper into its own folder on {where} and reads the "
                "range of the physical pen. Nothing is drawn. It refuses on any tablet or firmware "
                "that has not been verified for experimental ink.", "Check pen"):
            return
        self.win.run("ink-probe", lambda s: method(s, *INK_PROBE)(), mutation=True,
                     label="Check the tablet's pen for experimental ink",
                     on_done=self._probed, on_error=self._probe_failed)

    def _probed(self, result: Any) -> None:
        info = result if isinstance(result, dict) else {}
        ratio = info.get("axis_ratio")
        if info.get("ok") is False or not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
            self.axis_ratio = None
            self.probe_note.setText(str(info.get("message") or "The pen check returned no pen shape, "
                                                               "so drawing stays off."))
            set_role(self.probe_note, "error")
        else:
            self.axis_ratio = float(ratio)
            self.probe_note.setText(("Simulation: " if info.get("simulated") or self.win.simulated else "")
                                    + f"Pen checked. {info.get('message') or ''}".strip())
            set_role(self.probe_note, "ok")
        self._drop_calibration()
        self.refresh_state()
        if self.axis_ratio is not None and self.source and not self.preview_current():
            self.prepare()  # local only: redraw the preview with the measured pen shape

    def _probe_failed(self, exc: BaseException) -> None:
        self.axis_ratio = None
        self.probe_note.setText(f"The pen could not be checked, so drawing stays off. {describe_error(exc)}")
        set_role(self.probe_note, "error")
        self.refresh_state()

    def _ready_to_stream(self) -> bool:
        return (self.win.connected and self.axis_ratio is not None and self.preview_current()
                and not self.drawing)

    def draw_mark(self) -> None:
        if not self._ready_to_stream() or self.calibrated:
            return
        left, top, right, bottom = self.placement()
        arm_x, arm_y = min(left + 500, right), min(top + 500, bottom)
        if not self.win.dialogs.confirm(
                "Draw the calibration mark?",
                "PaperDesk draws one small corner mark at the top-left of the placement area, on the "
                "page that is open on the tablet now. Choose the pen tool on the tablet first. "
                "The mark is real ink.", "Draw mark"):
            return
        self._stream([[(left, arm_y), (left, top), (arm_x, top)]], "Draw the ink calibration mark",
                     self._mark_done, self._mark_failed)

    def _mark_done(self, result: Any) -> None:
        self._finish()
        info = result if isinstance(result, dict) else {}
        if info.get("ok") is False:
            self.mark_note.setText(str(info.get("message") or "The mark was not confirmed as drawn."))
            set_role(self.mark_note, "error")
        else:
            self.calibrated = True
            self.mark_note.setText("Mark sent. Look at the tablet now: if it is not where the red "
                                   "outline's top-left corner should be, change the placement and "
                                   "start again. " + str(info.get("message") or ""))
            set_role(self.mark_note, "ok")
        self.refresh_state()

    def _mark_failed(self, exc: BaseException) -> None:
        self._finish()
        self.mark_note.setText("The calibration mark did not finish; part of it may be on the page. "
                               f"{describe_error(exc)}")
        set_role(self.mark_note, "error")
        self.refresh_state()

    def draw(self) -> None:
        """The final ink.  Never reachable without a current preview, a pen check and an inspected mark."""
        if not (self._ready_to_stream() and self.calibrated and self.confirm.isChecked()):
            self.win.notify("warn", "Not started: draw figure as ink. Check the pen, draw the "
                                    "calibration mark and confirm you inspected it first.")
            return
        strokes = (self.prepared or {}).get("strokes")
        stats = (self.prepared or {}).get("stats") or {}
        if not self.win.dialogs.confirm(
                "Draw this figure as ink?",
                f"PaperDesk sends {stats.get('strokes', 'the')} strokes to the page open on the tablet. "
                "This is experimental. It is sent once and never repeated; if it stops part-way, the "
                "marks already made stay on the page.", "Draw figure"):
            return
        self._stream(strokes, f"Draw {Path(self.source).name} as ink", self._drawn, self._draw_failed)

    def _stream(self, strokes: Any, text: str, on_done, on_error) -> None:
        self._cancel.clear()
        cancel = self._cancel

        def call(service: Any) -> Any:
            stream = method(service, *INK_STREAM)
            extra: dict[str, Any] = {}
            if accepts(stream, "cancelled"):
                extra["cancelled"] = cancel.is_set
            if accepts(stream, "progress"):
                extra["progress"] = self.progressed.emit
            return stream(strokes, **extra)
        self.outcome.setText("")
        if self.win.run("ink-stream", call, mutation=True, partial=True, key="ink-stream", label=text,
                        on_done=on_done, on_error=on_error):
            self.drawing = True
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.progress.setFormat("Waiting for the tablet")
            self.progress.setVisible(True)
            self.refresh_state()

    def cancel(self) -> None:
        if self.drawing:
            self._cancel.set()
            self.outcome.setText("Stopping after the stroke in progress…")
            set_role(self.outcome, "warn")
            self.refresh_state()

    def _on_progress(self, done: int, total: int) -> None:
        if self.drawing and total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.progress.setFormat(f"{done} of {total} pen records sent")

    def _finish(self) -> None:
        self.drawing = False
        self.progress.setVisible(False)

    def _drawn(self, result: Any) -> None:
        self._finish()
        info = result if isinstance(result, dict) else {}
        failed = info.get("ok") is False
        self.outcome.setText(str(info.get("message") or ("The tablet reported that the drawing did not "
                                                         "complete." if failed else "Ink sent once."))
                             + " Each new drawing needs a new calibration mark.")
        set_role(self.outcome, "error" if failed else "ok")
        self._drop_calibration()
        self.mark_note.setText("")
        self.refresh_state()

    def _draw_failed(self, exc: BaseException) -> None:
        self._finish()
        stopped = self._cancel.is_set()
        self.outcome.setText(("Stopped at your request. " if stopped else "The drawing did not finish. ")
                             + f"{describe_error(exc)} {PARTIAL_HELP}")
        set_role(self.outcome, "error")
        self._drop_calibration()
        self.mark_note.setText("")
        self.refresh_state()
