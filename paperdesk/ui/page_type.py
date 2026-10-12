"""Type: send text and keys, clipboard, screenshots, notebook pages and touch."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QFrame, QGridLayout, QHBoxLayout, QPlainTextEdit,
                               QSizePolicy, QVBoxLayout, QWidget)

from . import theme
from .icons import paint_template
from .page_ink import InkPanel
from .services import STICKER, method
from .widgets import Card, Page, Sections, button, flow, label, set_role

US_CHARS = {chr(code) for code in range(0x20, 0x7F)} | {"\n", "\t"}
KEY_LABELS = {"backspace": "Backspace", "erase-five": "Erase five", "enter": "Enter", "tab": "Tab",
              "escape": "Escape", "copy": "Copy", "paste": "Paste", "close": "Close document",
              "left": "Left", "right": "Right", "up": "Up", "down": "Down"}
TEMPLATES = [("cornell", "Cornell", "Cues, notes, summary"), ("grid", "Grid", "Squared paper"),
             ("lined", "Lined", "Ruled lines"), ("blank", "Blank", "An empty page")]
UNICODE_NOTE = (
    "Text is typed on the tablet as presses of a US keyboard. Letters, digits and the punctuation "
    "printed on a US keyboard arrive exactly. Curly quotes, long dashes, the ellipsis character and "
    "non-breaking spaces are typed as their plain US equivalents. Accented letters, emoji and other "
    "scripts have no key: PaperDesk refuses the whole text before sending any of it, so you can "
    "replace them.")

# (unshifted, shifted or key id, width in key units).  Every row is 15 units wide.
_ROWS = [
    [("`", "~"), ("1", "!"), ("2", "@"), ("3", "#"), ("4", "$"), ("5", "%"), ("6", "^"), ("7", "&"),
     ("8", "*"), ("9", "("), ("0", ")"), ("-", "_"), ("=", "+"), ("Bksp", "key:backspace", 2)],
    [("Tab", "key:tab", 1.5)] + [(c, c.upper()) for c in "qwertyuiop"]
    + [("[", "{"), ("]", "}"), ("\\", "|", 1.5)],
    [("Caps", "mod:caps", 1.75)] + [(c, c.upper()) for c in "asdfghjkl"]
    + [(";", ":"), ("'", '"'), ("Enter", "key:enter", 2.25)],
    [("Shift", "mod:shift", 2.25)] + [(c, c.upper()) for c in "zxcvbnm"]
    + [(",", "<"), (".", ">"), ("/", "?"), ("Shift", "mod:shift", 2.75)],
    [("Esc", "key:escape", 1.5), ("Space", "text: ", 8.5), ("←", "key:left", 1.25),
     ("↑", "key:up", 1.25), ("↓", "key:down", 1.25), ("→", "key:right", 1.25)],
]
_SPOKEN = {"Bksp": "Backspace", "Esc": "Escape", "`": "Backtick", "-": "Minus", "=": "Equals", "[": "Left bracket", "]": "Right bracket",
           "\\": "Backslash", ";": "Semicolon", "'": "Apostrophe", ",": "Comma", ".": "Period",
           "/": "Slash", "←": "Left arrow", "↑": "Up arrow", "↓": "Down arrow", "→": "Right arrow"}


def outside_us_keyboard(text: str) -> list[str]:
    return sorted({ch for ch in text if ch not in US_CHARS})


class OnScreenKeyboard(QWidget):
    """A clickable US keyboard.  Shift applies to the next key; Caps stays on."""

    text = Signal(str)
    key = Signal(str)

    def __init__(self):
        super().__init__()
        self.setAccessibleName("On-screen US keyboard")
        self.shift = False
        self.caps = False
        self._chars: list[tuple[QAbstractButton, str, str]] = []
        self._shift_buttons: list[QAbstractButton] = []
        self._caps_button: QAbstractButton | None = None
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(5)
        for column in range(60):
            grid.setColumnStretch(column, 1)
        for row, keys in enumerate(_ROWS):
            column = 0
            for spec in keys:
                base, other = spec[0], spec[1]
                span = int(round((spec[2] if len(spec) > 2 else 1) * 4))
                btn = button(base, "key", name=_SPOKEN.get(base, base))
                btn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                btn.setMinimumHeight(34)
                btn.setAutoRepeat(False)
                if other == "mod:shift":
                    btn.setCheckable(True)
                    btn.setAccessibleName("Shift")
                    btn.clicked.connect(lambda checked: self.set_shift(checked))
                    self._shift_buttons.append(btn)
                elif other == "mod:caps":
                    btn.setCheckable(True)
                    btn.setAccessibleName("Caps lock")
                    btn.clicked.connect(lambda checked: self.set_caps(checked))
                    self._caps_button = btn
                elif other.startswith("key:"):
                    btn.clicked.connect(lambda _=False, k=other[4:]: self.key.emit(k))
                elif other.startswith("text:"):
                    btn.clicked.connect(lambda _=False, t=other[5:]: self._emit_text(t))
                else:
                    self._chars.append((btn, base, other))
                    btn.clicked.connect(lambda _=False, b=base, o=other: self._press(b, o))
                grid.addWidget(btn, row, column, 1, span)
                column += span
        self._relabel()

    def char_for(self, base: str, shifted: str) -> str:
        upper = self.shift != self.caps if base.isalpha() else self.shift
        return shifted if upper else base

    def _press(self, base: str, shifted: str) -> None:
        self._emit_text(self.char_for(base, shifted))

    def _emit_text(self, value: str) -> None:
        self.text.emit(value)
        if self.shift:
            self.set_shift(False)

    def set_shift(self, on: bool) -> None:
        self.shift = bool(on)
        for btn in self._shift_buttons:
            btn.setChecked(self.shift)
        self._relabel()

    def set_caps(self, on: bool) -> None:
        self.caps = bool(on)
        if self._caps_button is not None:
            self._caps_button.setChecked(self.caps)
        self._relabel()

    def _relabel(self) -> None:
        for btn, base, shifted in self._chars:
            shown = self.char_for(base, shifted)
            btn.setText("&&" if shown == "&" else shown)
            btn.setAccessibleName(_SPOKEN.get(shown, shown))


class LiveCapture(QFrame):
    """While it has focus and live mode is on, key presses go to the tablet."""

    text = Signal(str)
    key = Signal(str)
    clipboard = Signal()
    stopped = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("well")
        self.setAccessibleName("Live keyboard capture area")
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setMinimumHeight(76)
        self.live = False
        box = QVBoxLayout(self)
        box.setContentsMargins(14, 12, 14, 12)
        self.caption = label("", None)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(self.caption)
        self._describe()

    def start(self) -> None:
        self.live = True
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self._describe()

    def stop(self) -> None:
        if self.live:
            self.live = False
            self._describe()
            self.stopped.emit()

    def _describe(self) -> None:
        if self.live:
            self.caption.setText("<b>Live.</b> Keys you press now are sent to the tablet as you type. "
                                 "Shift+Esc or clicking elsewhere stops it.")
            self.setStyleSheet(f"QFrame#well {{ border: 2px solid {theme.ACCENT}; "
                               f"background: {theme.ACCENT_SOFT}; }}")
        else:
            self.caption.setText("Live keyboard is off. Nothing you type here is sent.")
            self.setStyleSheet("")

    def event(self, event) -> bool:
        if self.live and event.type() == QEvent.Type.KeyPress and event.key() in (
                Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def focusOutEvent(self, event) -> None:
        self.stop()
        super().focusOutEvent(event)

    def keyPressEvent(self, event) -> None:
        if not self.live:
            super().keyPressEvent(event)
            return
        key, mods = event.key(), event.modifiers()
        if key == Qt.Key.Key_Escape and mods & Qt.KeyboardModifier.ShiftModifier:
            self.stop()
            return
        named = {Qt.Key.Key_Backspace: "backspace", Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter",
                 Qt.Key.Key_Tab: "tab", Qt.Key.Key_Escape: "escape", Qt.Key.Key_Left: "left",
                 Qt.Key.Key_Right: "right", Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down"}
        if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            if key == Qt.Key.Key_V:
                self.clipboard.emit()
                return
            combo = {Qt.Key.Key_C: "copy", Qt.Key.Key_P: "paste", Qt.Key.Key_W: "close"}.get(key)
            if combo:
                self.key.emit(combo)
            return
        if key in named:
            self.key.emit(named[key])
            return
        typed = event.text()
        if typed and typed.isprintable():
            self.text.emit(typed)


class TemplateButton(QAbstractButton):
    def __init__(self, template: str, title: str, about: str):
        super().__init__()
        self.template, self.title, self.about = template, title, about
        self.setAccessibleName(f"Add {title} page")
        self.setAccessibleDescription(about)
        self.setToolTip(f"Add a {title.lower()} page to the open notebook")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:
        return QSize(132, 186)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        enabled = self.isEnabled()
        frame = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.hasFocus() or (self.underMouse() and enabled):
            painter.setPen(QPen(QColor(theme.ACCENT), 2 if self.hasFocus() else 1))
            painter.setBrush(QColor(theme.ACCENT_SOFT) if self.isDown() else Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(frame, 7, 7)
        ink = QColor(theme.INK if enabled else theme.FAINT)
        paint_template(painter, self.template, QRectF(21, 12, 90, 120), ink,
                       QColor("#9FB0C2" if enabled else theme.HAIRLINE))
        font = self.font()
        font.setWeight(font.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(ink)
        painter.drawText(QRectF(0, 138, self.width(), 22), int(Qt.AlignmentFlag.AlignCenter), self.title)
        font.setWeight(font.Weight.Normal)
        font.setPointSizeF(font.pointSizeF() * 0.86)
        painter.setFont(font)
        painter.setPen(QColor(theme.MUTED if enabled else theme.FAINT))
        painter.drawText(QRectF(2, 158, self.width() - 4, 24), int(Qt.AlignmentFlag.AlignCenter),
                         painter.fontMetrics().elidedText(self.about, Qt.TextElideMode.ElideRight,
                                                          self.width() - 6))


class TouchSurface(QWidget):
    """A captured contact forwards normalized down/move/up, including release."""

    tapped = Signal(float, float)
    contacted = Signal(str, float, float)
    cancelled = Signal(float, float)

    def __init__(self):
        super().__init__()
        self.setAccessibleName("Touch surface")
        self.setAccessibleDescription("Click or drag to touch the tablet. Arrow keys move the cursor; Enter presses or releases; Escape releases.")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(240, 320)
        self.last: tuple[float, float] | None = None
        self._contact = False

    def _position(self, event) -> tuple[float, float]:
        return (min(max(event.position().x() / self.width(), 0.0), 1.0),
                min(max(event.position().y() / self.height(), 0.0), 1.0))

    def mousePressEvent(self, event) -> None:
        if (self.isEnabled() and event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            continuing = self._contact
            self._contact = True
            self.last = self._position(event)
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.grabMouse()
            self.update()
            self.contacted.emit("move" if continuing else "down", *self.last)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._contact:
            self.last = self._position(event)
            self.update()
            self.contacted.emit("move", *self.last)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.end_contact(cancelled=True)
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down) and self.isEnabled():
            step = .05 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else .01
            dx, dy = {Qt.Key.Key_Left: (-step, 0), Qt.Key.Key_Right: (step, 0),
                      Qt.Key.Key_Up: (0, -step), Qt.Key.Key_Down: (0, step)}[event.key()]
            x, y = self.last or (0.5, 0.5)
            self.last = (min(max(x + dx, 0.0), 1.0), min(max(y + dy, 0.0), 1.0))
            self.update()
            if self._contact:
                self.contacted.emit("move", *self.last)
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.isEnabled():
            if self._contact:
                self.end_contact()
            else:
                self._contact = True
                self.last = self.last or (0.5, 0.5)
                self.contacted.emit("down", *self.last)
        elif event.key() == Qt.Key.Key_Space and self.isEnabled():
            if self._contact:
                self.end_contact()
            else:
                self._tap(*(self.last or (0.5, 0.5)))
        else:
            super().keyPressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._contact and event.button() == Qt.MouseButton.LeftButton:
            self.last = self._position(event)  # releasing outside the sheet still releases
            self.end_contact()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def end_contact(self, *, emit: bool = True, cancelled: bool = False) -> None:
        if not self._contact:
            return
        self._contact = False  # releaseMouse's UngrabMouse cannot release twice
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()
        if emit:
            if cancelled:
                self.cancelled.emit(*(self.last or (0.5, 0.5)))
            else:
                self.contacted.emit("up", *(self.last or (0.5, 0.5)))

    def focusOutEvent(self, event) -> None:
        self.end_contact(cancelled=True)
        super().focusOutEvent(event)

    def event(self, event) -> bool:
        if event.type() in (QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate,
                             QEvent.Type.Hide, QEvent.Type.Close):
            self.end_contact(cancelled=True)
        return super().event(event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self.end_contact(cancelled=True)
        super().changeEvent(event)

    def _tap(self, x: float, y: float) -> None:
        self.last = (min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0))
        self.update()
        self.tapped.emit(*self.last)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        enabled = self.isEnabled()
        painter.setPen(QPen(QColor(theme.ACCENT if self.hasFocus() else theme.EDGE), 2 if self.hasFocus() else 1))
        painter.setBrush(QColor(theme.WHITE if enabled else "#F1ECE0"))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)
        if self.last and enabled:
            painter.setPen(QPen(QColor(theme.ACCENT), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(self.last[0] * self.width(), self.last[1] * self.height()), 7, 7)


class TypePage(Page):
    def __init__(self, win):
        super().__init__(win, "Type", "Write on the page that is open on the tablet.")
        self._pending: list[tuple[str, str, bool]] = []
        self._feeding = False
        self._feeding_live = False
        self._live_cancel_requested = False
        self._touch_pending: list[tuple[str, float, float]] = []
        self._touch_sending = False
        self._touch_outstanding = False
        self._touch_release_requested = False
        self.sections = Sections("Type sections")
        self.col.addWidget(self.sections, 1)
        self.sections.add("write", "Write", self._build_write())
        self.sections.add("keyboard", "Keyboard", self._build_keyboard())
        self.sections.add("clipboard", "Clipboard and capture", self._build_clipboard())
        self.sections.add("pages", "Pages", self._build_pages())
        self.sections.add("touch", "Touch", self._build_touch())
        self.ink = InkPanel(win)
        self.sections.add("ink", "Figure as ink", self.ink)
        self.sections.changed.connect(lambda _s: self.live.stop())

    @staticmethod
    def _column() -> tuple[QWidget, QVBoxLayout]:
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)
        return holder, column

    # -- sections ------------------------------------------------------
    def _build_write(self) -> QWidget:
        holder, column = self._column()
        self.keyboard_notice = label("", "warn")
        self.keyboard_notice.setAccessibleName("Keyboard forwarding availability")
        column.addWidget(self.keyboard_notice)

        card = Card("Send text", "Write or paste here, then send it in one go. Ctrl+Enter sends.")
        self.text_edit = QPlainTextEdit()
        self.text_edit.setAccessibleName("Text to send to the tablet")
        self.text_edit.setPlaceholderText("Text to type on the tablet")
        self.text_edit.setTabChangesFocus(True)
        self.text_edit.setMinimumHeight(96)
        self.text_edit.setMaximumHeight(170)
        self.text_edit.textChanged.connect(self._text_changed)
        self.text_edit.installEventFilter(self)
        card.add(self.text_edit)
        self.unicode_warning = card.add(label("", "warn"))
        self.unicode_warning.setAccessibleName("Characters outside the US keyboard")
        self.send_button = button("Send text", "primary", "Type this text on the tablet (Ctrl+Enter)")
        self.send_button.clicked.connect(self.send_text)
        clear = button("Clear")
        clear.clicked.connect(self.text_edit.clear)
        card.add(flow(self.send_button, clear))
        card.add(label(UNICODE_NOTE, "caption"))
        column.addWidget(card)

        keys = Card("Quick keys")
        self.key_buttons: dict[str, QAbstractButton] = {}
        row = []
        for key_id in ("erase-five", "backspace", "enter", "tab", "copy", "paste", "close", "escape"):
            btn = button(KEY_LABELS[key_id], None, {
                "erase-five": "Remove the last five characters",
                "copy": "Send the tablet's copy shortcut", "paste": "Send the tablet's paste shortcut",
                "close": "Send the shortcut that closes the open document"}.get(key_id, ""))
            btn.clicked.connect(lambda _=False, k=key_id: self.feed("key", k))
            self.key_buttons[key_id] = btn
            row.append(btn)
        keys.add(flow(*row))
        column.addWidget(keys)

        live = Card("Live keyboard",
                    "For typing straight onto the page. It is off until you start it and stops by itself "
                    "when you click anywhere else. Dead keys and input methods are not forwarded.")
        self.live = LiveCapture()
        self.live.text.connect(lambda t: self.feed("text", t, live=True))
        self.live.key.connect(lambda k: self.feed("key", k, live=True))
        self.live.clipboard.connect(self.paste_desktop_clipboard)
        self.live.stopped.connect(self._live_stopped)
        live.add(self.live)
        self.live_button = button("Start live keyboard", None, "Send each key press as you type")
        self.live_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.live_button.clicked.connect(self.toggle_live)
        live.add(flow(self.live_button))
        column.addWidget(live)
        column.addStretch(1)
        return holder

    def _build_keyboard(self) -> QWidget:
        holder, column = self._column()
        card = Card("On-screen keyboard",
                    "Click a key to press it on the tablet. Shift applies to the next key only; Caps stays on.")
        self.osk = OnScreenKeyboard()
        self.osk.text.connect(lambda t: self.feed("text", t))
        self.osk.key.connect(lambda k: self.feed("key", k))
        card.add(self.osk)
        self.osk_notice = card.add(label("", "warn"))
        column.addWidget(card)
        column.addStretch(1)
        return holder

    def _build_clipboard(self) -> QWidget:
        holder, column = self._column()
        clip = Card("From this computer's clipboard")
        self.clip_text_button = button("Type clipboard text", None,
                                       "Type the text on this computer's clipboard onto the tablet")
        self.clip_image_button = button("Paste clipboard image", None,
                                        "Place the image on this computer's clipboard on the open page")
        self.file_image_button = button("Paste image file…", None, "Choose an image and place it on the page")
        self.clip_text_button.clicked.connect(self.send_clipboard_text)
        self.clip_image_button.clicked.connect(self.paste_clipboard_image)
        self.file_image_button.clicked.connect(self.paste_image_file)
        clip.add(flow(self.clip_text_button, self.clip_image_button, self.file_image_button))
        self.paste_notice = clip.add(label("", "warn"))
        self.paste_notice.setAccessibleName("Image paste availability")
        column.addWidget(clip)

        sticker = Card("Image sticker",
                       "Makes a new notebook with the image on its first page as a native image you can "
                       "move and resize on the tablet, and uploads it. It needs no extras and changes no "
                       "existing document.")
        self.sticker_button = button("Upload image sticker…", None,
                                     "Choose an image and upload it as a sticker notebook (Ctrl+I)")
        self.sticker_clip_button = button("Upload clipboard image as sticker", None,
                                          "Use the image on this computer's clipboard")
        self.sticker_button.clicked.connect(self.upload_sticker)
        self.sticker_clip_button.clicked.connect(self.upload_clipboard_sticker)
        sticker.add(flow(self.sticker_button, self.sticker_clip_button))
        self.sticker_notice = sticker.add(label("", "warn"))
        self.sticker_notice.setAccessibleName("Image sticker availability")
        column.addWidget(sticker)

        shot = Card("Tablet screenshot", "Capture what the tablet is showing right now.")
        self.shot_copy_button = button("Copy to clipboard", None, "Ctrl+Shift+C")
        self.shot_save_button = button("Save as…", None, "Ctrl+Shift+S")
        self.shot_copy_button.clicked.connect(self.copy_screenshot)
        self.shot_save_button.clicked.connect(self.save_screenshot)
        shot.add(flow(self.shot_copy_button, self.shot_save_button))
        self.shot_notice = shot.add(label("", "warn"))
        self.shot_notice.setAccessibleName("Screenshot availability")
        self.shot_preview = label("", "caption")
        self.shot_preview.setAccessibleName("Screenshot preview")
        self.shot_preview.setVisible(False)
        shot.add(self.shot_preview)
        column.addWidget(shot)
        column.addStretch(1)
        return holder

    def _build_pages(self) -> QWidget:
        holder, column = self._column()
        card = Card("Add a notebook page", "Adds a page with this layout to the notebook open on the tablet.")
        self.template_buttons: dict[str, TemplateButton] = {}
        row = []
        for template, title, about in TEMPLATES:
            btn = TemplateButton(template, title, about)
            btn.clicked.connect(lambda _=False, t=template: self.add_page(t))
            self.template_buttons[template] = btn
            row.append(btn)
        card.add(flow(*row, spacing=12))
        self.pages_notice = card.add(label("", "warn"))
        self.pages_notice.setAccessibleName("Notebook page availability")
        column.addWidget(card)
        column.addStretch(1)
        return holder

    def _build_touch(self) -> QWidget:
        holder, column = self._column()
        card = Card("Touch surface", "Click or drag the sheet to touch the same spot on the tablet's screen. "
                                     "Releasing the mouse or changing focus releases the touch. "
                                     "Arrows move the cursor; Enter presses or releases, and Escape releases.")
        self.touch_notice = card.add(label("", "warn"))
        self.touch_notice.setAccessibleName("Touch forwarding availability")
        self.touch = TouchSurface()
        self.touch.tapped.connect(self.tap)
        self.touch.contacted.connect(self._touch_event)
        self.touch.cancelled.connect(self._cancel_touch)
        row = QHBoxLayout()
        row.addWidget(self.touch)
        row.addStretch(1)
        card.body.addLayout(row)
        column.addWidget(card)
        column.addStretch(1)
        return holder

    # -- state ---------------------------------------------------------
    def touch_method(self) -> str:
        """Name of the backend's touch call, if it has one: touch(phase, x, y) or tap(x, y)."""
        for name in ("touch", "tap"):
            if callable(getattr(self.win.service, name, None)):
                return name
        return ""

    def _basic_reason(self, what: str) -> str:
        if not self.win.connected:
            return f"Connect to a tablet to use {what}."
        return (f"This tablet reports that {what} are not available right now. "
                "Setup › Features shows which extra provides them on this firmware.")

    def refresh_state(self) -> None:
        win, connected = self.win, self.win.connected
        can_type = win.supports("keyboard")
        notice = "" if can_type else win.unsupported_reason("keyboard", "keyboard forwarding")
        self.keyboard_notice.setText(notice)
        self.keyboard_notice.setVisible(bool(notice))
        self.osk_notice.setText(notice)
        self.osk_notice.setVisible(bool(notice))
        if not can_type:
            self.live.stop()
            self._pending.clear()
        for widget in (self.send_button, self.live_button, self.live, self.osk, self.clip_text_button,
                       *self.key_buttons.values()):
            widget.setEnabled(can_type)
        self._text_changed()

        can_paste = win.supports("direct-paste")
        self.clip_image_button.setEnabled(can_paste)
        self.file_image_button.setEnabled(can_paste)
        self.paste_notice.setText("" if can_paste else win.unsupported_reason("direct-paste", "image paste"))
        self.paste_notice.setVisible(not can_paste)
        if not connected:
            sticker_notice = "Connect to a tablet to upload an image sticker."
        elif method(win.service, *STICKER) is None:
            sticker_notice = "This build's device backend offers no sticker upload."
        elif not win.allows("upload"):
            sticker_notice = "This tablet reports that uploads are not available right now."
        else:
            sticker_notice = ""
        self.sticker_button.setEnabled(not sticker_notice)
        self.sticker_clip_button.setEnabled(not sticker_notice)
        self.sticker_notice.setText(sticker_notice)
        self.sticker_notice.setVisible(bool(sticker_notice))
        self.ink.refresh_state()
        can_shoot, can_page = win.allows("screenshot"), win.allows("pages")
        self.shot_copy_button.setEnabled(can_shoot)
        self.shot_save_button.setEnabled(can_shoot)
        self.shot_notice.setText("" if can_shoot else self._basic_reason("screenshots"))
        self.shot_notice.setVisible(not can_shoot)
        for btn in self.template_buttons.values():
            btn.setEnabled(can_page)
        self.pages_notice.setText("" if can_page else self._basic_reason("adding notebook pages"))
        self.pages_notice.setVisible(not can_page)

        if not win.supports("touch"):
            touch_notice = win.unsupported_reason("touch", "touch forwarding")
        elif not self.touch_method():
            touch_notice = ("The tablet reports touch forwarding, but this build's backend offers no "
                            "touch call, so the surface stays off.")
        else:
            touch_notice = ""
        self.touch.setEnabled(not touch_notice and not self._touch_release_requested)
        self.touch_notice.setText(touch_notice)
        self.touch_notice.setVisible(bool(touch_notice))

    def _text_changed(self) -> None:
        text = self.text_edit.toPlainText()
        odd = outside_us_keyboard(text)
        if odd:
            shown = " ".join(odd[:12]) + (" …" if len(odd) > 12 else "")
            self.unicode_warning.setText(
                f"{len(odd)} character{'' if len(odd) == 1 else 's'} here {'is' if len(odd) == 1 else 'are'} "
                f"not on a US keyboard and may not arrive: {shown}")
        self.unicode_warning.setVisible(bool(odd))
        self.send_button.setEnabled(bool(text) and self.win.supports("keyboard"))

    def eventFilter(self, watched, event) -> bool:
        if watched is self.text_edit and event.type() == QEvent.Type.KeyPress \
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.send_text()
            return True
        return super().eventFilter(watched, event)

    # -- typing --------------------------------------------------------
    def send_text(self) -> None:
        text = self.text_edit.toPlainText()
        if not text or not self._can("keyboard", "keyboard forwarding"):
            return
        count = len(text)

        def done(result: Any) -> None:
            if not (isinstance(result, dict) and result.get("ok") is False) \
                    and self.text_edit.toPlainText() == text:
                self.text_edit.clear()
        self.win.run("type", lambda s: s.type_text(text), mutation=True, on_done=done,
                     label=f"Type {count} character{'' if count == 1 else 's'}")

    def send_clipboard_text(self) -> None:
        text = QGuiApplication.clipboard().text()
        if not text:
            self.win.notify("info", "The clipboard holds no text, so nothing was sent.")
            return
        if not self._can("keyboard", "keyboard forwarding"):
            return
        odd = outside_us_keyboard(text)
        note = f" ({len(odd)} not on a US keyboard)" if odd else ""
        self.win.run("type-clipboard", lambda s: s.type_text(text), mutation=True,
                     label=f"Type {len(text)} clipboard characters{note}")

    def toggle_live(self) -> None:
        if self.live.live:
            self.live.stop()
        elif self._can("keyboard", "keyboard forwarding"):
            self.live.start()
            self._live_changed()

    def _live_changed(self) -> None:
        self.live_button.setText("Stop live keyboard" if self.live.live else "Start live keyboard")

    def _live_stopped(self) -> None:
        self._live_changed()
        # Focus loss/Shift+Esc cannot flush stale captured keys into a later
        # tablet control. Independently clicked on-screen keys stay separate.
        self._pending = [item for item in self._pending if not item[2]]
        if self._feeding_live:
            self._live_cancel_requested = True
        live_jobs = {"live-type-key", "live-send-key", "live-paste-image"}
        current = self.win.runner.current
        if current is not None and current.name in live_jobs:
            self.win.runner.cancel_current()
            self.win.cancel_service_inputs()
        self.win.runner.cancel_waiting(lambda job: job.name in live_jobs)

    def stop_forwarding(self) -> None:
        """Disconnect/close drops all pending input before signaling stop."""
        self._pending.clear()
        self.live.stop()
        self._touch_pending = [item for item in self._touch_pending if item[0] != "move"]
        self.touch.end_contact(cancelled=True)
        if self._touch_outstanding and not self._touch_release_requested:
            self._touch_event("up", *(self.touch.last or (0.5, 0.5)))

    def paste_desktop_clipboard(self) -> None:
        """Ctrl+V/Shift+V preserves live input ordering and uses host content."""
        image = QGuiApplication.clipboard().image()
        if not image.isNull():
            if not self._can("direct-paste", "image paste"):
                return
            path = self.win.temp_dir() / f"clipboard-{uuid.uuid4().hex[:10]}.png"
            if not image.save(str(path), "PNG"):
                self.win.notify("error", "The clipboard image could not be prepared for sending.")
                return
            self.feed("image", str(path), live=True)
        else:
            text = QGuiApplication.clipboard().text()
            if text:
                self.feed("text", text, live=True)
            else:
                self.win.notify("info", "The clipboard holds no text or image, so nothing was sent.")

    def feed(self, kind: str, value: str, *, live: bool = False) -> None:
        """Queue one key or some text; order is kept and a failure drops the rest."""
        if not self._can("direct-paste" if kind == "image" else "keyboard",
                         "image paste" if kind == "image" else "keyboard forwarding"):
            return
        merging = kind == "text" and self._pending and self._pending[-1][0] == "text" and self._pending[-1][2] == live
        pending_text = sum(len(item[1]) for item in self._pending if item[0] == "text")
        if (len(self._pending) >= 32 and not merging) or (kind == "text" and pending_text + len(value) > 100000):
            self.win.notify("warn", "Input backlog is full. The later input was not queued; wait for the tablet before sending more.")
            if live:
                self.live.stop()
            return
        if merging:
            self._pending[-1] = ("text", self._pending[-1][1] + value, live)
        else:
            self._pending.append((kind, value, live))
        self._pump()

    def _pump(self) -> None:
        if self._feeding or not self._pending or self.win.uncertain is not None:
            return
        kind, value, live = self._pending.pop(0)
        self._feeding_live = live
        self._live_cancel_requested = False
        if kind == "text":
            started = self.win.run("live-type-key" if live else "type-key", lambda s: s.type_text(value), mutation=True, dedupe=False,
                                   label=f"Type {len(value)} character{'' if len(value) == 1 else 's'}",
                                   on_done=self._fed, on_error=self._feed_failed)
        elif kind == "image":
            started = self.win.run("live-paste-image" if live else "paste-image", lambda s: s.paste_image(value),
                                   mutation=True, label="Paste the clipboard image",
                                   on_done=self._fed, on_error=self._feed_failed)
        else:
            started = self.win.run("live-send-key" if live else "send-key", lambda s: s.send_key(value), mutation=True, dedupe=False,
                                   label=f"Press {KEY_LABELS.get(value, value)}",
                                   on_done=self._fed, on_error=self._feed_failed)
        self._feeding = started
        if not started:
            self._feeding_live = False
            self._pending.clear()
            self.live.stop()

    def _fed(self, result: Any) -> None:
        self._feeding = False
        self._feeding_live = False
        self._live_cancel_requested = False
        if isinstance(result, dict) and result.get("ok") is False:
            self._pending.clear()
            self.live.stop()
            return
        self._pump()

    def _feed_failed(self, _exc: BaseException) -> None:
        self._feeding = False
        keep_clicked = self._feeding_live and self._live_cancel_requested
        self._feeding_live = False
        self._live_cancel_requested = False
        dropped = len(self._pending)
        if keep_clicked:
            self._pending = [item for item in self._pending if not item[2]]
            dropped -= len(self._pending)
        else:
            self._pending.clear()
        self.live.stop()
        if dropped:
            self.win.notify("warn", f"{dropped} later key press{'' if dropped == 1 else 'es'} "
                                    "were discarded and not sent, because the one before failed.")
        if keep_clicked and self.win.uncertain is None:
            self._pump()  # a live job cancelled before dispatch sent no input

    # -- clipboard, capture, pages, touch --------------------------------
    def paste_clipboard_image(self) -> None:
        image = QGuiApplication.clipboard().image()
        if image.isNull():
            self.win.notify("info", "The clipboard holds no image, so nothing was sent.")
            return
        if not self._can("direct-paste", "image paste"):
            return
        path = self.win.temp_dir() / f"clipboard-{uuid.uuid4().hex[:10]}.png"
        if not image.save(str(path), "PNG"):
            self.win.notify("error", "The clipboard image could not be prepared for sending.")
            return
        self.win.run("paste-image", lambda s: s.paste_image(str(path)), mutation=True,
                     label="Paste the clipboard image")

    def paste_image_file(self) -> None:
        if not self._can("direct-paste", "image paste"):
            return
        path = self.win.dialogs.open_file("Paste image file", "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp)",
                                          "pictures")
        if path:
            self.win.run("paste-image", lambda s: s.paste_image(path), mutation=True,
                         label=f"Paste {Path(path).name}")

    def _sticker_ready(self) -> bool:
        if not self._connected("upload an image sticker"):
            return False
        if method(self.win.service, *STICKER) is None:
            self.win.notify("warn", "This build's device backend offers no sticker upload.")
            return False
        return True

    def _send_sticker(self, path: str, text: str) -> None:
        folder = str(self.win.pages["library"].folder_combo.currentData() or "")
        self.win.run("sticker", lambda s: s.create_sticker(path, folder), mutation=True,
                     key=f"sticker:{path}", label=text,
                     on_done=lambda result: None if isinstance(result, dict) and result.get("ok") is False
                     else self.win.reload_documents(quiet=True))

    def upload_sticker(self) -> None:
        if not self._sticker_ready():
            return
        path = self.win.dialogs.open_file("Upload image sticker",
                                          "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp)", "pictures")
        if path:
            self._send_sticker(path, f"Upload {Path(path).name} as a sticker notebook")

    def upload_clipboard_sticker(self) -> None:
        image = QGuiApplication.clipboard().image()
        if image.isNull():
            self.win.notify("info", "The clipboard holds no image, so nothing was sent.")
            return
        if not self._sticker_ready():
            return
        path = self.win.temp_dir() / f"sticker-{uuid.uuid4().hex[:10]}.png"
        if not image.save(str(path), "PNG"):
            self.win.notify("error", "The clipboard image could not be prepared for sending.")
            return
        self._send_sticker(str(path), "Upload the clipboard image as a sticker notebook")

    def copy_screenshot(self) -> None:
        if not self._connected("take a screenshot"):
            return
        path = self.win.temp_dir() / f"screenshot-{uuid.uuid4().hex[:10]}.png"
        self.win.run("screenshot", lambda s: s.screenshot(str(path)), label="Capture the tablet screen",
                     on_done=lambda result: self._shot_done(result, str(path), copy=True))

    def save_screenshot(self) -> None:
        if not self._connected("take a screenshot"):
            return
        path = self.win.dialogs.save_file("Save tablet screenshot", "tablet-screenshot.png",
                                          "PNG images (*.png)", "pictures")
        if path:
            self.win.run("screenshot", lambda s: s.screenshot(path), label="Save a tablet screenshot",
                         on_done=lambda result: self._shot_done(result, path, copy=False))

    def _shot_done(self, result: Any, requested: str, copy: bool) -> None:
        if isinstance(result, dict) and result.get("ok") is False:
            return
        path = str(result.get("path") or requested) if isinstance(result, dict) else requested
        image = QImage(path)
        if image.isNull():
            self.shot_preview.setVisible(True)
            self.shot_preview.setText("The tablet reported a screenshot, but the file could not be read "
                                      f"as an image: {path}")
            self.win.notify("error", "The screenshot file could not be read as an image; nothing was copied.")
            return
        if copy:
            QGuiApplication.clipboard().setImage(image)
            self.win.notify("ok", f"Done: screenshot copied to the clipboard ({image.width()} × "
                                  f"{image.height()}).")
        pixmap = QPixmap.fromImage(image.scaled(QSize(220, 280), Qt.AspectRatioMode.KeepAspectRatio,
                                                Qt.TransformationMode.SmoothTransformation))
        self.shot_preview.setPixmap(pixmap)
        self.shot_preview.setVisible(True)
        self.shot_preview.setToolTip(("Simulator output. " if self.win.simulated else "")
                                     + ("Copied to the clipboard." if copy else f"Saved to {path}"))

    def add_page(self, template: str) -> None:
        if not self._connected("add a page"):
            return
        title = dict((t, n) for t, n, _ in TEMPLATES).get(template, template)
        self.win.run("add-page", lambda s: s.add_page(template), mutation=True, key=f"add-page:{template}",
                     label=f"Add a {title.lower()} page",
                     on_done=lambda _r: self.win.refresh_status_quietly())

    def tap(self, x: float, y: float) -> None:
        if self.touch_method() == "tap":
            if self._can("touch", "touch forwarding"):
                self.win.run("tap", lambda service: service.tap(x, y), mutation=True,
                             label=f"Tap at {x:.0%} across, {y:.0%} down")
            return
        self._touch_event("down", x, y)
        self._touch_event("up", x, y)

    def _touch_event(self, phase: str, x: float, y: float) -> None:
        if phase == "down":
            if self._touch_outstanding or self.touch_method() != "touch" or not self._can("touch", "touch forwarding"):
                self.touch.end_contact(emit=False)
                if self._touch_outstanding:
                    self.win.notify("warn", "Wait for the previous touch to release before starting another contact.")
                return
            self._touch_outstanding = True
            self._touch_release_requested = False
            self._touch_pending.append((phase, x, y))
        elif not self._touch_outstanding or self._touch_release_requested:
            return
        elif phase == "move":
            # At most one unsent move survives; slow transport cannot build a
            # delayed drag that keeps changing the tablet after mouse release.
            if self._touch_pending and self._touch_pending[-1][0] == "move":
                self._touch_pending[-1] = (phase, x, y)
            else:
                self._touch_pending.append((phase, x, y))
        elif phase == "up":
            self._touch_release_requested = True
            self._touch_pending.append((phase, x, y))
            self.touch.setEnabled(False)
        self._touch_pump()

    def _cancel_touch(self, x: float, y: float) -> None:
        self._touch_pending = [item for item in self._touch_pending if item[0] != "move"]
        self._touch_event("up", x, y)

    def _touch_pump(self) -> None:
        if self._touch_sending or not self._touch_pending:
            return
        phase, x, y = self._touch_pending.pop(0)
        self._touch_sending = True
        def call(service):
            try:
                return service.touch(phase, x, y)
            except Exception:
                # An uncertain release is never resent. Closing the helper's
                # input stream releases its kernel touch device instead.
                cancel = getattr(service, "cancel_current", None)
                if callable(cancel):
                    cancel()
                raise
        started = self.win.run(f"touch-{phase}", call, mutation=True, cleanup=phase == "up",
                               needs_connection=phase != "up", label="Release touch" if phase == "up" else "Forward touch",
                               on_done=lambda result: self._touch_done(phase, result), on_error=self._touch_failed)
        if not started:
            self._touch_failed(RuntimeError("Touch forwarding stopped before dispatch."))

    def _touch_done(self, phase: str, result: Any) -> None:
        self._touch_sending = False
        if isinstance(result, dict) and result.get("ok") is False:
            self._touch_failed(RuntimeError("The touch action was not confirmed."))
        elif phase == "up":
            self._touch_outstanding = self._touch_release_requested = False
            self._touch_pending.clear()
            self.refresh_state()
        else:
            self._touch_pump()

    def _touch_failed(self, _exc: BaseException) -> None:
        self._touch_pending.clear()
        self._touch_sending = self._touch_outstanding = self._touch_release_requested = False
        self.touch.end_contact(emit=False)
        self.win.cancel_service_inputs()
        self.refresh_state()

    def _can(self, feature: str, what: str) -> bool:
        if self.win.supports(feature):
            return True
        self.win.notify("warn", self.win.unsupported_reason(feature, what))
        return False

    def _connected(self, what: str) -> bool:
        if self.win.connected:
            return True
        self.win.notify("warn", f"Connect to a tablet first to {what}.")
        return False
