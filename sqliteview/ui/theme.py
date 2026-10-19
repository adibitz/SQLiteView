"""Design tokens and the application style sheet (light and dark)."""
from __future__ import annotations

import os
from dataclasses import dataclass, fields

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from ..core.settings import Settings
from ..core.table_model import SqliteTableModel


@dataclass(frozen=True)
class Tokens:
    name: str
    bg: str
    surface: str
    surface_alt: str
    sunken: str
    border: str
    border_strong: str
    text: str
    text_muted: str
    text_faint: str
    accent: str
    accent_hover: str
    accent_soft: str
    accent_text: str
    danger: str
    danger_soft: str
    success: str
    warning: str
    warning_soft: str
    selection: str
    selection_text: str
    grid: str
    header: str
    # syntax colours
    syn_keyword: str
    syn_function: str
    syn_table: str
    syn_string: str
    syn_number: str
    syn_comment: str
    syn_current_line: str
    # categorical chart series, in fixed order (validated for colour-blind separation per mode)
    chart_series: tuple[str, ...]


LIGHT = Tokens(
    name="light",
    bg="#f4f5f7", surface="#ffffff", surface_alt="#f7f8fa", sunken="#eef0f3",
    border="#e3e6ea", border_strong="#cdd2d9",
    text="#1d2127", text_muted="#5f6773", text_faint="#9aa1ab",
    accent="#4a63e7", accent_hover="#3b53d4", accent_soft="#e9edfd", accent_text="#ffffff",
    danger="#d13c3c", danger_soft="#fdeeee", success="#1f9d5a", warning="#b7791f", warning_soft="#fdf4e3",
    selection="#dfe6ff", selection_text="#1d2127",
    grid="#eef0f3", header="#f7f8fa",
    syn_keyword="#3048c9", syn_function="#8a3fb8", syn_table="#0f7b6c", syn_string="#b4581e",
    syn_number="#0a7aa8", syn_comment="#8a929c", syn_current_line="#f3f5fb",
    chart_series=("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"),
)

DARK = Tokens(
    name="dark",
    bg="#141518", surface="#1b1c20", surface_alt="#212329", sunken="#111215",
    border="#2a2d33", border_strong="#3a3e46",
    text="#e5e7eb", text_muted="#9aa1ad", text_faint="#6b7280",
    accent="#7b8ff7", accent_hover="#93a4fa", accent_soft="#252c4d", accent_text="#0f1220",
    danger="#f0706b", danger_soft="#3a1f22", success="#4cc38a", warning="#e3a948", warning_soft="#3a2f1a",
    selection="#2b3666", selection_text="#f1f3f8",
    grid="#24262c", header="#1f2126",
    syn_keyword="#8fa2ff", syn_function="#d19af0", syn_table="#5fd4bf", syn_string="#f0a56b",
    syn_number="#67c7f0", syn_comment="#6f7782", syn_current_line="#20232b",
    chart_series=("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"),
)

UI_FONT_CANDIDATES = ("DM Sans", "Segoe UI Variable Text", "Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue", "Ubuntu", "Noto Sans")
MONO_FONT_CANDIDATES = ("Cascadia Mono", "Cascadia Code", "JetBrains Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono")


def _first_available(candidates: tuple[str, ...], fallback: str) -> str:
    families = set(QFontDatabase.families())
    for c in candidates:
        if c in families:
            return c
    return fallback


FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts")
_bundled_loaded = False


def load_bundled_fonts() -> list[str]:
    """Register the fonts shipped with the app (DM Sans, SIL Open Font License); returns their families."""
    global _bundled_loaded
    families: list[str] = []
    if _bundled_loaded or not os.path.isdir(FONTS_DIR):
        return families
    _bundled_loaded = True
    for name in sorted(os.listdir(FONTS_DIR)):
        if name.lower().endswith((".ttf", ".otf")):
            fid = QFontDatabase.addApplicationFont(os.path.join(FONTS_DIR, name))
            if fid >= 0:
                families.extend(QFontDatabase.applicationFontFamilies(fid))
    return families


def ui_font_family() -> str:
    return _first_available(UI_FONT_CANDIDATES, QApplication.font().family())


def mono_font_family() -> str:
    return _first_available(MONO_FONT_CANDIDATES, QFontDatabase.systemFont(QFontDatabase.FixedFont).family())


def mono_font(size: int | None = None) -> QFont:
    chosen = Settings.get("editor", "font")
    f = QFont(chosen if chosen and chosen in QFontDatabase.families() else mono_font_family())
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(size or int(Settings.get("editor", "fontsize")))
    return f


def build_stylesheet(t: Tokens, compact: bool, icon_dir: str) -> str:
    pad_v = 3 if compact else 5
    pad_h = 8 if compact else 10
    row_pad = 2 if compact else 4
    return f"""
* {{ outline: none; }}
QWidget {{ color: {t.text}; selection-background-color: {t.selection}; selection-color: {t.selection_text}; }}
QMainWindow, QDialog {{ background: {t.bg}; }}
QMainWindow::separator {{ background: {t.border}; width: 1px; height: 1px; }}
QToolTip {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border_strong}; padding: 6px 8px; border-radius: 6px; }}

/* ---------- surfaces ---------- */
QFrame[role="surface"], QWidget[role="surface"] {{ background: {t.surface}; }}
QFrame[role="card"] {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px; }}
QFrame[role="toolbar"] {{ background: {t.surface}; border-bottom: 1px solid {t.border}; }}
QFrame[role="sep"] {{ background: {t.border}; max-width: 1px; min-width: 1px; }}
QFrame[role="hsep"] {{ background: {t.border}; max-height: 1px; min-height: 1px; }}
QLabel[role="title"] {{ font-size: 15pt; font-weight: 600; }}
QLabel[role="h2"] {{ font-size: 11.5pt; font-weight: 600; }}
QLabel[role="muted"] {{ color: {t.text_muted}; }}
QLabel[role="faint"] {{ color: {t.text_faint}; }}
QLabel[role="section"] {{ color: {t.text_muted}; font-size: 8pt; font-weight: 600; letter-spacing: 0.6px; }}
QLabel[role="chip"] {{ background: {t.sunken}; color: {t.text_muted}; border-radius: 9px; padding: 1px 8px; font-size: 8.5pt; }}
QLabel[role="chip-accent"] {{ background: {t.accent_soft}; color: {t.accent}; border-radius: 9px; padding: 1px 8px; font-size: 8.5pt; font-weight: 600; }}
QLabel[role="chip-warning"] {{ background: {t.warning_soft}; color: {t.warning}; border-radius: 9px; padding: 1px 8px; font-size: 8.5pt; font-weight: 600; }}
QLabel[role="chip-danger"] {{ background: {t.danger_soft}; color: {t.danger}; border-radius: 9px; padding: 1px 8px; font-size: 8.5pt; font-weight: 600; }}
QLabel[role="error"] {{ color: {t.danger}; }}
QLabel[role="success"] {{ color: {t.success}; }}

/* ---------- buttons ---------- */
QPushButton {{ background: {t.surface}; border: 1px solid {t.border_strong}; border-radius: 6px; padding: {pad_v}px {pad_h + 4}px; min-height: 18px; }}
QPushButton:hover {{ background: {t.surface_alt}; border-color: {t.text_faint}; }}
QPushButton:pressed {{ background: {t.sunken}; }}
QPushButton:disabled {{ color: {t.text_faint}; border-color: {t.border}; background: {t.surface_alt}; }}
QPushButton[kind="primary"], QPushButton:default {{ background: {t.accent}; color: {t.accent_text}; border: 1px solid {t.accent}; font-weight: 600; }}
QPushButton[kind="primary"]:hover, QPushButton:default:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QPushButton[kind="primary"]:disabled {{ background: {t.accent_soft}; color: {t.text_faint}; border-color: {t.accent_soft}; }}
QPushButton[kind="danger"] {{ color: {t.danger}; }}
QPushButton[kind="danger"]:hover {{ background: {t.danger_soft}; border-color: {t.danger}; }}
QPushButton[kind="ghost"] {{ background: transparent; border: 1px solid transparent; }}
QPushButton[kind="ghost"]:hover {{ background: {t.sunken}; }}
QPushButton[kind="link"] {{ background: transparent; border: none; color: {t.accent}; padding: 0; text-align: left; }}
QPushButton[kind="link"]:hover {{ text-decoration: underline; }}
QPushButton[kind="search"] {{ background: {t.sunken}; border: 1px solid transparent; border-radius: 8px; min-height: 22px; }}
QPushButton[kind="search"]:hover {{ border-color: {t.border_strong}; background: {t.surface}; }}
QPushButton[kind="tile"] {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px; padding: 14px; text-align: left; font-weight: 600; }}
QPushButton[kind="tile"]:hover {{ border-color: {t.accent}; background: {t.accent_soft}; }}

QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: {pad_v}px {max(4, pad_h - 4)}px; }}
QToolButton:hover {{ background: {t.sunken}; }}
QToolButton:pressed, QToolButton:checked {{ background: {t.accent_soft}; color: {t.accent}; }}
QToolButton:disabled {{ color: {t.text_faint}; }}
QToolButton[kind="primary"] {{ background: {t.accent}; color: {t.accent_text}; font-weight: 600; padding: {pad_v}px {pad_h + 2}px; }}
QToolButton[kind="primary"]:hover {{ background: {t.accent_hover}; }}
QToolButton[kind="primary"]:disabled {{ background: {t.sunken}; color: {t.text_faint}; }}
QToolButton[kind="warn"] {{ background: {t.warning_soft}; color: {t.warning}; font-weight: 600; }}
QToolButton[kind="warn"]:hover {{ border-color: {t.warning}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QToolButton[kind="chip"] {{ background: {t.accent_soft}; color: {t.accent}; border-radius: 11px; padding: 2px 10px; font-size: 8.5pt; }}
QToolButton[kind="chip"]:hover {{ border-color: {t.accent}; }}
QFrame[role="chipsbar"] {{ background: {t.surface}; border-bottom: 1px solid {t.border}; }}
QFrame[role="selectionbar"] {{ background: {t.accent_soft}; border-radius: 8px; }}

/* segmented control */
QFrame[role="segmented"] {{ background: {t.sunken}; border-radius: 7px; padding: 2px; }}
QFrame[role="segmented"] QToolButton {{ border-radius: 5px; padding: 3px 12px; color: {t.text_muted}; }}
QFrame[role="segmented"] QToolButton:checked {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border}; font-weight: 600; }}

/* ---------- inputs ---------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateTimeEdit {{
    background: {t.surface}; border: 1px solid {t.border_strong}; border-radius: 6px; padding: {pad_v - 1}px 8px; min-height: 18px;
}}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{ border-color: {t.text_faint}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {t.accent}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {t.text_faint}; background: {t.surface_alt}; }}
QLineEdit[role="search"] {{ background: {t.sunken}; border: 1px solid transparent; border-radius: 7px; padding-left: 6px; }}
QLineEdit[role="search"]:focus {{ background: {t.surface}; border-color: {t.accent}; }}
QLineEdit[role="filter"] {{ border-radius: 4px; padding: 1px 5px; background: {t.surface}; border: 1px solid {t.border}; font-size: 8.5pt; }}
QLineEdit[role="filter"][active="true"] {{ border-color: {t.accent}; background: {t.accent_soft}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{ image: url({icon_dir}/chevron-down.svg); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {t.surface}; border: 1px solid {t.border_strong}; border-radius: 6px; padding: 4px; selection-background-color: {t.accent_soft}; selection-color: {t.text}; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; border: none; }}
QPlainTextEdit, QTextEdit {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 6px; }}
QPlainTextEdit[role="editor"] {{ border: none; border-radius: 0; }}
QCheckBox, QRadioButton {{ spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QCheckBox::indicator {{ border: 1px solid {t.border_strong}; border-radius: 4px; background: {t.surface}; }}
QCheckBox::indicator:hover {{ border-color: {t.accent}; }}
QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; image: url({icon_dir}/check.svg); }}
QAbstractItemView::indicator {{ width: 15px; height: 15px; border: 1px solid {t.border_strong}; border-radius: 4px; background: {t.surface}; }}
QAbstractItemView::indicator:hover {{ border-color: {t.accent}; }}
QAbstractItemView::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; image: url({icon_dir}/check.svg); }}
QAbstractItemView::indicator:disabled {{ background: {t.sunken}; border-color: {t.border}; }}
QRadioButton::indicator {{ border: 1px solid {t.border_strong}; border-radius: 8px; background: {t.surface}; }}
QRadioButton::indicator:checked {{ border: 5px solid {t.accent}; }}
QGroupBox {{ border: 1px solid {t.border}; border-radius: 8px; margin-top: 14px; padding: 10px 8px 8px 8px; background: {t.surface}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {t.text_muted}; font-weight: 600; }}

/* ---------- views ---------- */
QAbstractItemView {{ background: {t.surface}; alternate-background-color: {t.surface_alt}; border: none; }}
QTreeView {{ show-decoration-selected: 1; }}
QTreeView::item {{ padding: {row_pad}px 2px; border-radius: 5px; }}
QTreeView::item:hover {{ background: {t.sunken}; }}
QTreeView::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
QListView::item {{ padding: {row_pad + 2}px 6px; border-radius: 6px; }}
QListView::item:hover {{ background: {t.sunken}; }}
QListView::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
QTableView {{ gridline-color: {t.grid}; selection-background-color: {t.selection}; selection-color: {t.selection_text}; }}
QTableView QTableCornerButton::section {{ background: {t.header}; border: none; border-right: 1px solid {t.border}; border-bottom: 1px solid {t.border}; }}
QHeaderView {{ background: {t.header}; }}
QHeaderView::section {{ background: {t.header}; color: {t.text_muted}; border: none; border-right: 1px solid {t.grid}; border-bottom: 1px solid {t.border}; padding: {row_pad}px 8px; font-weight: 600; }}
QHeaderView::section:vertical {{ padding: 0 6px; font-weight: 400; color: {t.text_faint}; border-right: 1px solid {t.border}; border-bottom: 1px solid {t.grid}; }}
QHeaderView::section:hover {{ background: {t.sunken}; color: {t.text}; }}

/* ---------- tabs ---------- */
QTabWidget::pane {{ border: none; background: {t.surface}; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {t.text_muted}; padding: {pad_v + 2}px 12px; margin-right: 2px; border: none; border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {t.text}; }}
QTabBar::tab:selected {{ color: {t.text}; border-bottom: 2px solid {t.accent}; font-weight: 600; }}
QTabWidget[role="documents"] > QTabBar {{ background: {t.bg}; }}
QTabWidget[role="documents"] > QTabBar::tab {{ background: {t.bg}; border: 1px solid transparent; border-bottom: none; border-top-left-radius: 8px; border-top-right-radius: 8px; padding: {pad_v + 3}px 10px {pad_v + 3}px 12px; margin-top: 4px; min-width: 60px; }}
QTabWidget[role="documents"] > QTabBar::tab:hover {{ background: {t.sunken}; }}
QTabWidget[role="documents"] > QTabBar::tab:selected {{ background: {t.surface}; border-color: {t.border}; color: {t.text}; font-weight: 600; }}
QTabBar::close-button {{ image: url({icon_dir}/x.svg); subcontrol-position: right; border-radius: 4px; margin: 2px; }}
QTabBar::close-button:hover {{ background: {t.sunken}; }}

/* ---------- docks ---------- */
QDockWidget {{ titlebar-close-icon: url({icon_dir}/x.svg); titlebar-normal-icon: url({icon_dir}/float.svg); }}
QDockWidget::title {{ background: {t.surface}; padding: 7px 10px; text-align: left; border-bottom: 1px solid {t.border}; font-weight: 600; color: {t.text_muted}; }}
QDockWidget::close-button, QDockWidget::float-button {{ border: none; background: transparent; border-radius: 4px; padding: 2px; }}
QDockWidget::close-button:hover, QDockWidget::float-button:hover {{ background: {t.sunken}; }}

/* ---------- menus / bars ---------- */
QMenuBar {{ background: {t.surface}; border-bottom: 1px solid {t.border}; padding: 1px 4px; }}
QMenuBar::item {{ background: transparent; padding: 4px 9px; border-radius: 5px; }}
QMenuBar::item:selected {{ background: {t.sunken}; }}
QMenu {{ background: {t.surface}; border: 1px solid {t.border_strong}; border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 6px 26px 6px 10px; border-radius: 5px; }}
QMenu::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
QMenu::item:disabled {{ color: {t.text_faint}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 6px; }}
QMenu::icon {{ padding-left: 8px; }}
QToolBar {{ background: {t.surface}; border: none; border-bottom: 1px solid {t.border}; spacing: 4px; padding: 4px 8px; }}
QToolBar::separator {{ background: {t.border}; width: 1px; margin: 4px 6px; }}
QStatusBar {{ background: {t.surface}; border-top: 1px solid {t.border}; color: {t.text_muted}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {t.text_muted}; padding: 0 6px; }}
QSplitter::handle {{ background: {t.border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QProgressBar {{ background: {t.sunken}; border: none; border-radius: 3px; height: 6px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 3px; }}

/* ---------- scrollbars ---------- */
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle {{ background: {t.border_strong}; border-radius: 4px; min-height: 28px; min-width: 28px; margin: 2px; }}
QScrollBar::handle:hover {{ background: {t.text_faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QAbstractScrollArea::corner {{ background: transparent; }}

/* command palette */
QFrame[role="palette"] {{ background: {t.surface}; border: 1px solid {t.border_strong}; border-radius: 12px; }}
QFrame[role="palette"] QLineEdit {{ border: none; border-bottom: 1px solid {t.border}; border-radius: 0; padding: 12px 14px; font-size: 11.5pt; background: transparent; }}
QFrame[role="palette"] QListView {{ background: transparent; padding: 6px; }}
QFrame[role="toast"] {{ background: {t.text}; border-radius: 8px; }}
QFrame[role="toast"] QLabel {{ color: {t.surface}; }}
QFrame[role="banner"] {{ background: {t.warning_soft}; border-bottom: 1px solid {t.border}; }}
QFrame[role="banner-danger"] {{ background: {t.danger_soft}; border-bottom: 1px solid {t.border}; }}
"""


class ThemeManager(QObject):
    """Owns the current tokens and applies them app-wide."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.tokens: Tokens = LIGHT

    def resolve_mode(self) -> str:
        mode = Settings.get("General", "theme")
        if mode in ("light", "dark"):
            return mode
        hints = QGuiApplication.styleHints()
        try:
            return "dark" if hints.colorScheme() == Qt.ColorScheme.Dark else "light"
        except AttributeError:
            return "light"

    def apply(self) -> None:
        app = QApplication.instance()
        self.tokens = DARK if self.resolve_mode() == "dark" else LIGHT
        t = self.tokens
        app.setStyle("Fusion")
        pal = QPalette()
        for role, color in (
            (QPalette.Window, t.bg), (QPalette.WindowText, t.text), (QPalette.Base, t.surface),
            (QPalette.AlternateBase, t.surface_alt), (QPalette.Text, t.text), (QPalette.Button, t.surface),
            (QPalette.ButtonText, t.text), (QPalette.Highlight, t.selection), (QPalette.HighlightedText, t.selection_text),
            (QPalette.ToolTipBase, t.surface), (QPalette.ToolTipText, t.text), (QPalette.PlaceholderText, t.text_faint),
            (QPalette.Link, t.accent), (QPalette.Mid, t.border), (QPalette.Midlight, t.border), (QPalette.Dark, t.border_strong),
            (QPalette.Light, t.surface), (QPalette.Shadow, t.border_strong),
        ):
            pal.setColor(role, QColor(color))
        pal.setColor(QPalette.Disabled, QPalette.Text, QColor(t.text_faint))
        pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(t.text_faint))
        pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(t.text_faint))
        app.setPalette(pal)

        load_bundled_fonts()
        font = QFont(ui_font_family())
        size = int(Settings.get("General", "fontsize") or 0)
        font.setPointSizeF(size if size > 0 else 9.5)
        app.setFont(font)

        from .icons import register_qss_resources
        icon_dir = register_qss_resources(t)
        app.setStyleSheet(build_stylesheet(t, Settings.get("General", "uiDensity") == "compact", icon_dir))

        c = SqliteTableModel.colors
        c.reg_fg = QColor(t.text)
        c.null_fg = QColor(t.text_faint)
        c.bin_fg = QColor(t.syn_function)
        c.loading_fg = QColor(t.text_faint)
        c.null_bg = QColor()
        c.bin_bg = QColor()
        c.fmt_fg = QColor(t.syn_table)
        c.fmt_bg = QColor()
        self.changed.emit()


theme = ThemeManager()


def tokens() -> Tokens:
    return theme.tokens


def token_names() -> list[str]:
    return [f.name for f in fields(Tokens)]
