"""Dark theme for the application: palette, stylesheet and small chrome widgets."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QLabel, QFrame, QHBoxLayout, QWidget

BG = "#0b0f14"
PANEL = "#121820"
PANEL_HI = "#18202b"
BORDER = "#222c38"
TEXT = "#e6edf3"
MUTED = "#8b98a9"
ACCENT = "#38bdf8"
ACCENT_DIM = "#1e6f8f"
GOOD = "#34d399"
WARN = "#fbbf24"
BAD = "#f87171"
TRACE = "#38bdf8"
PEAK = "#fb923c"
MARK = "#facc15"

MONO = "Consolas, 'Cascadia Mono', monospace"

STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: 'Segoe UI', 'Inter', sans-serif;
    font-size: 12px;
}}
QMainWindow, QTabWidget::pane {{ background: {BG}; }}

/* these are drawn on top of panels, so they must not paint the window ground */
QLabel, QCheckBox, QRadioButton, QGroupBox > QWidget {{ background: transparent; }}

QGroupBox {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 10px;
    margin-top: 14px;
    padding: 14px 10px 10px 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: {ACCENT};
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
}}

QPushButton {{
    background: {PANEL_HI};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 7px 10px;
    color: {TEXT};
    font-weight: 600;
}}
QPushButton:hover  {{ background: #1f2a38; border-color: {ACCENT_DIM}; }}
QPushButton:pressed {{ background: #0f1720; }}
QPushButton:disabled {{ background: #10161d; color: #4c5866; border-color: #1a222c; }}
QPushButton:checked {{ background: {BAD}; border-color: {BAD}; color: #1a0d0d; }}

QPushButton#primary {{
    background: {ACCENT}; border: none; color: #04202c;
}}
QPushButton#primary:hover {{ background: #7dd3fc; }}
QPushButton#primary:disabled {{ background: #17313d; color: #4c5866; }}
QPushButton#danger {{ background: #7f1d1d; border: none; color: #fee2e2; }}
QPushButton#danger:hover {{ background: #991b1b; }}

QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
    background: {PANEL_HI};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 7px;
    selection-background-color: {ACCENT_DIM};
}}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: {ACCENT_DIM}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {PANEL_HI};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DIM};
    outline: none;
}}

QCheckBox, QRadioButton {{ spacing: 7px; padding: 2px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px; height: 15px;
    border: 1px solid #3a4756; background: {PANEL_HI};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT};
}}

QTabBar::tab {{
    background: transparent;
    color: {MUTED};
    padding: 9px 20px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 600;
}}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}

QTableWidget, QListWidget, QPlainTextEdit {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
    gridline-color: #1b2430;
    selection-background-color: {ACCENT_DIM};
    outline: none;
}}
QHeaderView::section {{
    background: {PANEL_HI};
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    font-weight: 600;
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 0.6px;
}}
QTableWidget::item {{ padding: 4px 6px; }}
QListWidget::item {{ padding: 4px 6px; border-radius: 4px; }}
QListWidget::item:hover {{ background: {PANEL_HI}; }}

/* Styling ::item switches off Qt's own selection painting, so the selected
   row has to be drawn explicitly - including when the widget loses focus,
   which is exactly when you are reaching for the Tune button. */
QListWidget::item:selected, QListWidget::item:selected:active,
QListWidget::item:selected:!active {{
    background: {ACCENT_DIM};
    color: #ffffff;
}}
QTableWidget::item:selected, QTableWidget::item:selected:active,
QTableWidget::item:selected:!active {{
    background: {ACCENT_DIM};
    color: #ffffff;
}}
QListWidget::item:selected:hover, QTableWidget::item:selected:hover {{
    background: {ACCENT};
    color: #04202c;
}}

QProgressBar {{
    background: {PANEL_HI}; border: 1px solid {BORDER};
    border-radius: 7px; height: 14px; text-align: center;
    color: {MUTED}; font-size: 10px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 6px; }}

QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QSplitter::handle {{ background: {BORDER}; height: 2px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #2b3643; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3a4756; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #2b3643; border-radius: 5px; min-width: 30px; }}
QToolTip {{
    background: {PANEL_HI}; color: {TEXT};
    border: 1px solid {BORDER}; padding: 5px; border-radius: 5px;
}}
"""


class Pill(QLabel):
    """Small rounded status chip."""

    def __init__(self, text="", color=MUTED, parent=None):
        super().__init__(text, parent)
        self.setFont(QFont("Consolas", 9))
        self.set_color(color)

    def set_color(self, color):
        self._color = color
        self.setStyleSheet(
            f"background: {PANEL_HI}; color: {color};"
            f"border: 1px solid {BORDER}; border-radius: 10px;"
            f"padding: 3px 10px; font-weight: 600;"
        )

    def set(self, text, color=None):
        self.setText(text)
        if color:
            self.set_color(color)


class Collapsible(QWidget):
    """
    A disclosure section. Everything that is not needed to press Start lives
    in one of these, closed, so the panel reads as a handful of controls
    rather than a wall of them.
    """

    def __init__(self, title, content, opened=False, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QToolButton, QVBoxLayout
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self.button = QToolButton()
        self.button.setText(title)
        self.button.setCheckable(True)
        self.button.setChecked(opened)
        self.button.setArrowType(Qt.DownArrow if opened else Qt.RightArrow)
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.setStyleSheet(
            f"QToolButton {{ background: transparent; border: none; color: {MUTED};"
            f" font-weight: 600; font-size: 11px; text-transform: uppercase;"
            f" letter-spacing: 1px; padding: 4px 2px; }}"
            f"QToolButton:hover {{ color: {ACCENT}; }}")
        self.button.clicked.connect(self._toggle)
        lay.addWidget(self.button)

        self.content = content
        self.content.setVisible(opened)
        lay.addWidget(self.content)

    def _toggle(self, on):
        self.button.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.content.setVisible(on)

    def set_open(self, on):
        self.button.setChecked(on)
        self._toggle(on)


class Header(QFrame):
    """Title bar with the app name and a row of status pills."""

    def __init__(self, title, subtitle, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame {{ background: {PANEL}; border: 1px solid {BORDER};"
            f"border-radius: 10px; }}"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 9, 14, 9)
        lay.setSpacing(10)

        name = QLabel(title)
        f = QFont("Segoe UI", 13)
        f.setWeight(QFont.DemiBold)
        name.setFont(f)
        name.setStyleSheet(f"color: {TEXT}; border: none;")
        lay.addWidget(name)

        sub = QLabel(subtitle)
        sub.setStyleSheet(f"color: {MUTED}; border: none;")
        lay.addWidget(sub)
        lay.addStretch(1)

        self.pill_dev = Pill("no device", BAD)
        self.pill_state = Pill("idle", MUTED)
        for p in (self.pill_dev, self.pill_state):
            lay.addWidget(p)


def apply(app):
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
