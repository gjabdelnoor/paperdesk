"""Small vector glyphs drawn with QPainter so they stay crisp at any scale."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF


def _pen(color: QColor, width: float = 1.6) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def paint_icon(painter: QPainter, name: str, rect: QRectF, color: QColor) -> None:
    """Draw the named glyph inside rect; glyphs are designed on a 20x20 grid."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.translate(rect.topLeft())
    painter.scale(rect.width() / 20.0, rect.height() / 20.0)
    painter.setPen(_pen(color))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if name == "home":
        painter.drawPolyline(QPolygonF([QPointF(2.5, 10), QPointF(10, 3), QPointF(17.5, 10)]))
        painter.drawPolyline(QPolygonF([QPointF(4.5, 9), QPointF(4.5, 17), QPointF(15.5, 17),
                                        QPointF(15.5, 9)]))
        painter.drawPolyline(QPolygonF([QPointF(8.5, 17), QPointF(8.5, 12.5), QPointF(11.5, 12.5),
                                        QPointF(11.5, 17)]))
    elif name == "library":
        path = QPainterPath()
        path.moveTo(5, 2.5)
        path.lineTo(12, 2.5)
        path.lineTo(15.5, 6)
        path.lineTo(15.5, 17.5)
        path.lineTo(5, 17.5)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawPolyline(QPolygonF([QPointF(12, 2.5), QPointF(12, 6), QPointF(15.5, 6)]))
        painter.drawLine(QPointF(7.5, 10), QPointF(13, 10))
        painter.drawLine(QPointF(7.5, 13.5), QPointF(13, 13.5))
    elif name == "type":
        painter.drawRoundedRect(QRectF(2, 5, 16, 10.5), 2, 2)
        painter.setPen(_pen(color, 1.8))
        for x in (5.2, 8.4, 11.6, 14.8):
            painter.drawPoint(QPointF(x, 8.4))
        painter.setPen(_pen(color))
        painter.drawLine(QPointF(6.5, 12.2), QPointF(13.5, 12.2))
    elif name == "art":
        painter.drawLine(QPointF(15.5, 3), QPointF(7, 11.5))
        painter.drawLine(QPointF(17, 4.5), QPointF(8.5, 13))
        painter.drawLine(QPointF(15.5, 3), QPointF(17, 4.5))
        path = QPainterPath()
        path.moveTo(7, 11.5)
        path.cubicTo(4, 12, 4.5, 15, 2.5, 17)
        path.cubicTo(6, 17.5, 8.5, 16, 8.5, 13)
        painter.drawPath(path)
    elif name == "setup":
        for y, knob in ((5, 13), (10, 7), (15, 11.5)):
            painter.drawLine(QPointF(2.5, y), QPointF(17.5, y))
            painter.save()
            painter.setBrush(color)
            painter.drawEllipse(QPointF(knob, y), 1.9, 1.9)
            painter.restore()
    painter.restore()


def paint_template(painter: QPainter, template: str, rect: QRectF, ink: QColor, rule: QColor) -> None:
    """Draw a miniature notebook page for the given template id."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(ink, 1.2))
    painter.setBrush(QColor("#FFFFFF"))
    painter.drawRoundedRect(rect, 3, 3)
    inner = rect.adjusted(5, 6, -5, -6)
    painter.setPen(QPen(rule, 1.0))
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    step = max(6.0, inner.height() / 9.0)
    if template in ("lined", "cornell"):
        y = inner.top() + step
        while y < inner.bottom():
            painter.drawLine(QPointF(inner.left(), y), QPointF(inner.right(), y))
            y += step
    if template == "grid":
        y = inner.top()
        while y <= inner.bottom():
            painter.drawLine(QPointF(inner.left(), y), QPointF(inner.right(), y))
            y += step
        x = inner.left()
        while x <= inner.right():
            painter.drawLine(QPointF(x, inner.top()), QPointF(x, inner.bottom()))
            x += step
    if template == "cornell":
        painter.setPen(QPen(ink, 1.2))
        cue = inner.left() + inner.width() * 0.3
        summary = inner.bottom() - inner.height() * 0.22
        painter.drawLine(QPointF(cue, inner.top()), QPointF(cue, summary))
        painter.drawLine(QPointF(inner.left(), summary), QPointF(inner.right(), summary))
    painter.restore()
