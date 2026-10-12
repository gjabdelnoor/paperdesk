"""Shared building blocks: cards, banners, flowing rows, data views."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QFrame, QHBoxLayout, QLabel, QLayout,
                               QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from . import theme
from .icons import paint_icon


def restyle(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def label(text: str = "", role: str | None = None, wrap: bool = True, selectable: bool = False) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(wrap)
    if role:
        lab.setProperty("role", role)
    if selectable:
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                    | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    return lab


def set_role(lab: QWidget, role: str) -> None:
    if lab.property("role") != role:
        lab.setProperty("role", role)
        restyle(lab)


def button(text: str, role: str | None = None, tip: str = "", name: str = "") -> QPushButton:
    btn = QPushButton(text)
    if role:
        btn.setProperty("role", role)
    if tip:
        btn.setToolTip(tip)
    btn.setAccessibleName(name or text.replace("&", "").rstrip("…"))
    btn.setCursor(Qt.CursorShape.PointingHandCursor if role == "link" else Qt.CursorShape.ArrowCursor)
    return btn


def rule() -> QFrame:
    line = QFrame()
    line.setObjectName("rule")
    line.setFixedHeight(1)
    return line


class FlowLayout(QLayout):
    """Lays children out left to right and wraps them, so rows reflow when narrow."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 8):
        super().__init__(parent)
        self._items: list = []
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _layout(self, rect: QRect, apply: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line = area.x(), area.y(), 0
        for item in self._items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            if x > area.x() and x + hint.width() > area.right() + 1:
                x = area.x()
                y += line + self._gap
                line = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._gap
            line = max(line, hint.height())
        return y + line - rect.y() + margins.bottom()


def flow(*widgets: QWidget, spacing: int = 8) -> QWidget:
    holder = QWidget()
    layout = FlowLayout(holder, spacing)
    for widget in widgets:
        layout.addWidget(widget)
    policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    policy.setHeightForWidth(True)
    holder.setSizePolicy(policy)
    return holder


class Card(QFrame):
    def __init__(self, title: str = "", subtitle: str = ""):
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(10)
        self.title_label = None
        if title:
            self.title_label = label(title, "heading")
            self.body.addWidget(self.title_label)
        self.subtitle_label = None
        if subtitle:
            self.subtitle_label = label(subtitle, "muted")
            self.body.addWidget(self.subtitle_label)

    def add(self, widget: QWidget) -> QWidget:
        self.body.addWidget(widget)
        return widget


class StateDot(QWidget):
    """A small filled or hollow circle; always paired with text that says the same thing."""

    def __init__(self, color: str = theme.FAINT, filled: bool = False):
        super().__init__()
        self._color, self._filled = QColor(color), filled
        self.setFixedSize(12, 12)

    def set_state(self, color: str, filled: bool = True) -> None:
        self._color, self._filled = QColor(color), filled
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(self._color, 1.6))
        painter.setBrush(self._color if self._filled else Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(2, 2, 8, 8))


class Banner(QFrame):
    """A full-width strip above the pages for states the user must not miss."""

    def __init__(self, kind: str, name: str):
        super().__init__()
        self.setProperty("banner", kind)
        self.setAccessibleName(name)
        row = QHBoxLayout(self)
        row.setContentsMargins(20, 9, 14, 9)
        row.setSpacing(10)
        self.text = label("", None)
        row.addWidget(self.text, 1)
        self._row, self._kind = row, kind
        self.hide()

    def add_action(self, text: str, callback, tip: str = "") -> QPushButton:
        btn = button(text, "banner" if self._kind == "sim" else None, tip)
        btn.clicked.connect(callback)
        self._row.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)
        return btn

    def show_text(self, text: str) -> None:
        self.text.setText(text)
        self.show()


class NavButton(QAbstractButton):
    """Sidebar entry with a painted glyph, a clear selected state and a focus ring."""

    def __init__(self, page_id: str, text: str, shortcut_hint: str = ""):
        super().__init__()
        self.page_id = page_id
        self.setText(text)
        self.setCheckable(True)
        self.setAccessibleName(f"{text} page")
        if shortcut_hint:
            self.setToolTip(f"{text} ({shortcut_hint})")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._ring = False

    def sizeHint(self) -> QSize:
        return QSize(170, max(38, self.fontMetrics().height() + 20))

    def focusInEvent(self, event) -> None:
        # The ring is for keyboard users; a mouse click already shows the selected state.
        self._ring = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                                        Qt.FocusReason.ShortcutFocusReason)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._ring = False
        super().focusOutEvent(event)

    def keyPressEvent(self, event) -> None:
        self._ring = True
        super().keyPressEvent(event)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(8, 2, -8, -2)
        checked = self.isChecked()
        if checked:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.PAPER))
            painter.drawRoundedRect(rect, 7, 7)
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawRoundedRect(QRectF(rect.left(), rect.top() + 7, 3.5, rect.height() - 14), 1.5, 1.5)
        elif self.underMouse():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 110))
            painter.drawRoundedRect(rect, 7, 7)
        if self.hasFocus() and self._ring:
            painter.setPen(QPen(QColor(theme.ACCENT), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)
        ink = QColor(theme.INK if checked else theme.MUTED)
        paint_icon(painter, self.page_id, QRectF(rect.left() + 14, rect.center().y() - 9, 18, 18),
                   QColor(theme.ACCENT) if checked else ink)
        font = self.font()
        font.setWeight(font.Weight.DemiBold if checked else font.Weight.Normal)
        painter.setFont(font)
        painter.setPen(ink)
        painter.drawText(rect.adjusted(44, 0, -6, 0),
                         int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self.text())


class Sections(QWidget):
    """A row of section tabs over a stack; the row wraps when the window is narrow."""

    changed = Signal(str)

    def __init__(self, name: str):
        super().__init__()
        self.setAccessibleName(name)
        self._ids: list[str] = []
        self._buttons: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._bar = QWidget()
        self._bar_layout = FlowLayout(self._bar, 2)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self._bar.setSizePolicy(policy)
        outer.addWidget(self._bar)
        outer.addWidget(rule())
        outer.addSpacing(14)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

    def add(self, section_id: str, text: str, widget: QWidget) -> QWidget:
        btn = button(text, "segment", name=f"{text} section")
        btn.setCheckable(True)
        bold = btn.font()
        bold.setWeight(bold.Weight.DemiBold)
        btn.setMinimumWidth(QFontMetrics(bold).horizontalAdvance(text) + 30)  # selected text is bolder
        btn.clicked.connect(lambda _=False, s=section_id: self.select(s))
        self._group.addButton(btn)
        self._bar_layout.addWidget(btn)
        self._buttons[section_id] = btn
        self._ids.append(section_id)
        self.stack.addWidget(widget)
        if len(self._ids) == 1:
            btn.setChecked(True)
        return widget

    @property
    def current(self) -> str:
        return self._ids[self.stack.currentIndex()] if self._ids else ""

    def ids(self) -> list[str]:
        return list(self._ids)

    def visible_ids(self) -> list[str]:
        return [section for section in self._ids if not self._buttons[section].isHidden()]

    def set_section_visible(self, section_id: str, visible: bool) -> None:
        """Hide a tab that does not apply right now; leaving it selected moves to the first tab."""
        btn = self._buttons.get(section_id)
        if btn is None or btn.isHidden() != visible:
            return
        btn.setVisible(visible)
        self._bar_layout.invalidate()
        if not visible and self.current == section_id:
            self.select(self._ids[0])

    def select(self, section_id: str) -> None:
        if section_id not in self._buttons:
            return
        self._buttons[section_id].setChecked(True)
        index = self._ids.index(section_id)
        if index != self.stack.currentIndex():
            self.stack.setCurrentIndex(index)
            self.changed.emit(section_id)


class DataView(QTreeWidget):
    """Shows whatever structure the backend returned, without reinterpreting it."""

    def __init__(self, name: str):
        super().__init__()
        self.setAccessibleName(name)
        self.setColumnCount(2)
        self.setHeaderLabels(["Item", "Reported value"])
        self.setAlternatingRowColors(True)
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setMinimumHeight(170)
        self.header().setStretchLastSection(True)
        self.data: Any = None

    def set_data(self, data: Any) -> None:
        self.data = data
        self.clear()
        if data is None:
            return
        self._fill(self.invisibleRootItem(), data)
        self.expandToDepth(1)
        self.resizeColumnToContents(0)
        self.setColumnWidth(0, min(max(self.columnWidth(0) + 12, 150), 320))

    def _fill(self, parent: QTreeWidgetItem, value: Any) -> None:
        if isinstance(value, dict):
            pairs = [(pretty_key(str(k)), v) for k, v in value.items()]
        elif isinstance(value, (list, tuple)):
            pairs = [(str(i + 1), v) for i, v in enumerate(value)]
        else:
            pairs = [("Value", value)]
        for key, item in pairs:
            if isinstance(item, (dict, list, tuple)) and item:
                node = QTreeWidgetItem(parent, [key, summarize(item)])
                self._fill(node, item)
            else:
                node = QTreeWidgetItem(parent, [key, plain(item)])
                node.setToolTip(1, plain(item))


def pretty_key(key: str) -> str:
    text = key.replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:] if text else key


def plain(value: Any) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    if value is None or value == "" or value == [] or value == {}:
        return "—"
    return str(value)


def summarize(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("message", "summary", "description", "name", "path"):
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
        return ""
    return f"{len(value)} item" + ("" if len(value) == 1 else "s")


class ElidedLabel(QLabel):
    """One line of text that shortens with an ellipsis; the full text stays in the tooltip."""

    def __init__(self, name: str):
        super().__init__()
        self._full = ""
        self.setAccessibleName(name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(60)

    def set_full_text(self, text: str) -> None:
        self._full = " ".join(text.split())
        self.setToolTip(self._full)
        self.setAccessibleDescription(self._full)
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight,
                                                   max(40, self.width() - 4)))


class Page(QWidget):
    """A scrolling page with a title, an introduction and a column of cards."""

    def __init__(self, win, title: str, intro: str = ""):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        inner = QWidget()
        self.col = QVBoxLayout(inner)
        self.col.setContentsMargins(28, 22, 28, 24)
        self.col.setSpacing(14)
        self.title_label = label(title, "title")
        self.col.addWidget(self.title_label)
        self.intro_label = label(intro, "muted")
        self.col.addWidget(self.intro_label)
        self.intro_label.setVisible(bool(intro))
        outer.addWidget(scroll_page(inner))

    def refresh_state(self) -> None:  # overridden by pages
        pass


def scroll_page(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    inner.setObjectName("pageBody")
    area.setWidget(inner)
    return area
