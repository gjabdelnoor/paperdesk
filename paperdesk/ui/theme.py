"""Ecru-and-ink visual system: one palette, one stylesheet, system fonts."""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QWidget

ECRU = "#F3EEE2"
PAPER = "#FCFAF4"
SIDEBAR = "#E9E2D1"
WHITE = "#FFFFFF"
INK = "#22201C"
MUTED = "#625C51"
FAINT = "#958E7F"
HAIRLINE = "#D6CDB9"
EDGE = "#B5AB94"
ACCENT = "#27496D"
ACCENT_DARK = "#1B3552"
ACCENT_SOFT = "#DCE4EC"
OK = "#3C6A3A"
WARN = "#8A5A00"
ERR = "#9A2B1E"
WARN_BG = "#F6E9C8"
ERR_BG = "#F5DDD6"


def base_point_size() -> float:
    size = QApplication.font().pointSizeF()
    return size if size > 0 else 10.0


def mono_font() -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSizeF(base_point_size() * 1.05)
    return font


def palette() -> QPalette:
    pal = QPalette()
    roles = QPalette.ColorRole
    for role, color in (
        (roles.Window, ECRU), (roles.WindowText, INK), (roles.Base, WHITE),
        (roles.AlternateBase, "#FAF7F0"), (roles.Text, INK), (roles.Button, PAPER),
        (roles.ButtonText, INK), (roles.Highlight, ACCENT), (roles.HighlightedText, WHITE),
        (roles.ToolTipBase, INK), (roles.ToolTipText, ECRU), (roles.PlaceholderText, FAINT),
        (roles.Link, ACCENT),
    ):
        pal.setColor(role, QColor(color))
    for role in (roles.WindowText, roles.Text, roles.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(FAINT))
    return pal


def stylesheet() -> str:
    b = base_point_size()
    return f"""
QMainWindow, QDialog {{ background: {ECRU}; }}
QFrame#sidebar {{ background: {SIDEBAR}; border: none; border-right: 1px solid {HAIRLINE}; }}
QFrame#activity {{ background: {SIDEBAR}; border: none; border-top: 1px solid {HAIRLINE}; }}
QFrame#card {{ background: {PAPER}; border: 1px solid {HAIRLINE}; border-radius: 10px; }}
QFrame#well {{ background: {WHITE}; border: 1px solid {HAIRLINE}; border-radius: 6px; }}
QFrame#rule {{ background: {HAIRLINE}; border: none; max-height: 1px; }}

QLabel {{ color: {INK}; background: transparent; }}
QLabel[role="wordmark"] {{ font-size: {b * 1.45:.1f}pt; font-weight: 600; }}
QLabel[role="title"] {{ font-size: {b * 1.8:.1f}pt; font-weight: 600; }}
QLabel[role="heading"] {{ font-size: {b * 1.18:.1f}pt; font-weight: 600; }}
QLabel[role="strong"] {{ font-weight: 600; }}
QLabel[role="muted"] {{ color: {MUTED}; }}
QLabel[role="caption"] {{ color: {MUTED}; font-size: {b * 0.92:.1f}pt; }}
QLabel[role="ok"] {{ color: {OK}; font-weight: 600; }}
QLabel[role="warn"] {{ color: {WARN}; font-weight: 600; }}
QLabel[role="error"] {{ color: {ERR}; font-weight: 600; }}
QLabel[role="step"] {{ color: {ACCENT}; font-weight: 700; font-size: {b * 1.18:.1f}pt; }}
QLabel[role="fingerprint"] {{ background: {WHITE}; border: 1px solid {EDGE};
    border-radius: 6px; padding: 10px 12px; }}

QPushButton {{ background: {PAPER}; color: {INK}; border: 1px solid {EDGE};
    border-radius: 6px; padding: 6px 14px; }}
QPushButton:hover {{ background: {WHITE}; border-color: {INK}; }}
QPushButton:pressed {{ background: #EDE6D6; }}
QPushButton:focus {{ border: 2px solid {ACCENT}; padding: 5px 13px; }}
QPushButton:checked {{ background: {ACCENT_SOFT}; border: 2px solid {ACCENT}; padding: 5px 13px;
    font-weight: 600; }}
QPushButton:disabled {{ color: {FAINT}; border-color: {HAIRLINE}; background: #F1ECE0; }}
QPushButton[role="primary"] {{ background: {ACCENT}; color: {WHITE}; border-color: {ACCENT};
    font-weight: 600; }}
QPushButton[role="primary"]:hover {{ background: {ACCENT_DARK}; border-color: {ACCENT_DARK}; }}
QPushButton[role="primary"]:focus {{ border: 2px solid {INK}; }}
QPushButton[role="primary"]:disabled {{ background: #B4BCC6; color: {ECRU}; border-color: #B4BCC6; }}
QPushButton[role="danger"] {{ color: {ERR}; border-color: {ERR}; }}
QPushButton[role="danger"]:disabled {{ color: {FAINT}; border-color: {HAIRLINE}; }}
QPushButton[role="link"] {{ background: transparent; border: none; color: {ACCENT};
    text-decoration: underline; padding: 2px 2px; text-align: left; }}
QPushButton[role="link"]:focus {{ border: 1px dotted {ACCENT}; padding: 1px 1px; }}
QPushButton[role="segment"] {{ background: transparent; border: none;
    border-bottom: 2px solid transparent; border-radius: 0; padding: 7px 12px; color: {MUTED}; }}
QPushButton[role="segment"]:hover {{ color: {INK}; }}
QPushButton[role="segment"]:focus {{ border: 1px dotted {ACCENT}; border-bottom: 2px solid {ACCENT_SOFT};
    padding: 6px 11px 7px 11px; }}
QPushButton[role="segment"]:checked {{ color: {INK}; font-weight: 600; background: transparent;
    border: none; border-bottom: 2px solid {ACCENT}; padding: 7px 12px; }}
QPushButton[role="key"] {{ padding: 6px 2px; min-width: 14px; border-radius: 5px; }}
QPushButton[role="key"]:focus {{ padding: 5px 1px; }}
QPushButton[role="key"]:checked {{ padding: 5px 1px; }}
QPushButton[role="banner"] {{ background: transparent; color: {ECRU}; border: 1px solid {ECRU}; }}
QPushButton[role="banner"]:hover {{ background: #3A3731; }}
QPushButton[role="banner"]:focus {{ border: 2px solid {WHITE}; }}

QLineEdit, QPlainTextEdit, QTextEdit {{ background: {WHITE}; color: {INK}; border: 1px solid {EDGE};
    border-radius: 6px; padding: 5px 8px; selection-background-color: {ACCENT};
    selection-color: {WHITE}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border: 2px solid {ACCENT}; padding: 4px 7px; }}
QLineEdit:disabled, QPlainTextEdit:disabled {{ background: #F1ECE0; color: {FAINT}; }}

QTreeWidget {{ background: {WHITE}; alternate-background-color: #FAF7F0; border: 1px solid {HAIRLINE};
    border-radius: 6px; outline: 0; }}
QTreeWidget:focus {{ border: 1px solid {ACCENT}; }}
QTreeWidget::item {{ padding: 4px 2px; }}
QTreeWidget::item:selected {{ background: {ACCENT}; color: {WHITE}; }}
QHeaderView::section {{ background: #EFE9DB; color: {INK}; border: none;
    border-bottom: 1px solid {HAIRLINE}; padding: 5px 8px; font-weight: 600; }}

QProgressBar {{ background: {WHITE}; color: {INK}; border: 1px solid {EDGE}; border-radius: 6px;
    text-align: center; min-height: 20px; }}
QProgressBar::chunk {{ background: {ACCENT_SOFT}; border-radius: 5px; }}

QScrollArea {{ border: none; background: {ECRU}; }}
QWidget#pageBody {{ background: {ECRU}; }}
QFrame[banner="sim"] {{ background: {INK}; border: none; }}
QFrame[banner="sim"] QLabel {{ color: {ECRU}; }}
QFrame[banner="warn"] {{ background: {WARN_BG}; border: none; border-bottom: 1px solid #DCC88F; }}
QFrame[banner="error"] {{ background: {ERR_BG}; border: none; border-bottom: 1px solid #DDB3A8; }}
QFrame[banner="info"] {{ background: {ACCENT_SOFT}; border: none; border-bottom: 1px solid #BCCBDA; }}
QToolTip {{ background: {INK}; color: {ECRU}; border: none; padding: 4px 6px; }}
"""


def apply(window: QWidget) -> None:
    window.setPalette(palette())
    window.setStyleSheet(stylesheet())
