"""Inline SVG icon set (stroke icons on a 24px grid), tinted to the current theme."""
from __future__ import annotations

import os
import tempfile
from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_P = {
    "database": '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    "database-plus": '<ellipse cx="11" cy="5" rx="7" ry="3"/><path d="M4 5v13c0 1.6 3 2.8 7 3"/><path d="M18 5v6"/><path d="M4 11.5c0 1.6 3 2.9 7 3"/><path d="M18 15v6M15 18h6"/>',
    "folder-open": '<path d="M3 19V6a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v2"/><path d="M3 19l2.6-7.2A2 2 0 0 1 7.5 10.5H21l-2.8 7.6a2 2 0 0 1-1.9 1.4H3"/>',
    "file": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    "file-code": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M10 12l-2 2 2 2M14 12l2 2-2 2"/>',
    "save": '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "check-circle": '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l3 3 5-6"/>',
    "undo": '<path d="M4 9h11a5 5 0 0 1 0 10H8"/><path d="M8 5L4 9l4 4"/>',
    "table": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M3 15h18M9 10v10"/>',
    "table-plus": '<path d="M12 20H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v6"/><path d="M3 10h18M9 10v10"/><path d="M18 15v6M15 18h6"/>',
    "table-edit": '<path d="M11 20H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v5"/><path d="M3 10h18M9 10v10"/><path d="M19.5 14.5l2 2-5 5H14.5v-2z"/>',
    "view": '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "index": '<path d="M4 6h10M4 12h16M4 18h7"/><path d="M18 3v6M15.5 6.5L18 9l2.5-2.5"/>',
    "trigger": '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
    "column": '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M9 3v18M15 3v18" opacity=".45"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M10.8 12.2L20 3M16 7l3 3M18 5l2 2"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "play": '<path d="M7 4.5v15a1 1 0 0 0 1.5.9l12-7.5a1 1 0 0 0 0-1.8l-12-7.5A1 1 0 0 0 7 4.5z"/>',
    "play-line": '<path d="M5 5.5v13l9-6.5z"/><path d="M17 6v12M20 6v12"/>',
    "play-cursor": '<path d="M5 5.5v13l9-6.5z"/><path d="M18 5h3M19.5 5v14M18 19h3"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    "filter": '<path d="M3 5h18l-7 8.5V19l-4 2v-7.5z"/>',
    "filter-x": '<path d="M3 5h18l-7 8.5V19l-4 2v-7.5z"/><path d="M17 15l4 4M21 15l-4 4"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14.6-4.5L4 8"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.6 4.5L20 16"/><path d="M20 20v-4h-4"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4h6v3"/>',
    "copy": '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
    "clipboard": '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3h6v1"/><path d="M9 10h6M9 14h4"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "terminal": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/>',
    "download": '<path d="M12 4v11M7 10l5 5 5-5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    "upload": '<path d="M12 15V4M7 9l5-5 5 5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "row-plus": '<rect x="3" y="4" width="18" height="7" rx="1.5"/><path d="M12 14v7M8.5 17.5h7"/>',
    "row-minus": '<rect x="3" y="4" width="18" height="7" rx="1.5"/><path d="M8.5 17.5h7"/>',
    "duplicate": '<rect x="3" y="3" width="13" height="13" rx="2"/><path d="M8 21h11a2 2 0 0 0 2-2V8"/><path d="M9.5 6.5v6M6.5 9.5h6"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "moon": '<path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"/>',
    "command": '<path d="M9 6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3z"/>',
    "chevron-down": '<path d="M6 9l6 6 6-6"/>',
    "chevron-right": '<path d="M9 6l6 6-6 6"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
    "alert": '<path d="M12 3l9.5 17h-19z"/><path d="M12 10v4.5M12 17.5v.5"/>',
    "more": '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
    "panel-left": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    "panel-right": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
    "panel-bottom": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 15h18"/>',
    "maximize": '<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>',
    "minimize": '<path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/>',
    "float": '<rect x="8" y="3" width="13" height="13" rx="2"/><path d="M16 16v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2h3"/>',
    "attach": '<path d="M20 11.5l-8.3 8.3a5 5 0 0 1-7.1-7.1l8.3-8.3a3.3 3.3 0 0 1 4.7 4.7L9.3 17.4a1.7 1.7 0 0 1-2.4-2.4l7.6-7.6"/>',
    "wrench": '<path d="M14.7 6.3a4 4 0 0 0 5 5l-9.4 9.4a2.1 2.1 0 0 1-3-3z"/><path d="M14.7 6.3L17 4a4 4 0 0 1 3 3l-2.3 2.3"/>',
    "shield": '<path d="M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
    "broom": '<path d="M19 3l-7.5 7.5"/><path d="M9 9l6 6-2.5 2.5c-1.7 1.7-5 3-8.5 3.5.5-3.5 1.8-6.8 3.5-8.5z"/><path d="M7.5 15.5L5 18"/>',
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "image": '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
    "braces": '<path d="M8 3H7a2 2 0 0 0-2 2v4a2 2 0 0 1-2 2 2 2 0 0 1 2 2v4a2 2 0 0 0 2 2h1M16 3h1a2 2 0 0 1 2 2v4a2 2 0 0 0 2 2 2 2 0 0 0-2 2v4a2 2 0 0 1-2 2h-1"/>',
    "binary": '<rect x="4" y="4" width="5" height="7" rx="1.5"/><path d="M15 4v7M13 4h2M14 20v-7M12 13h2"/><rect x="17" y="13" width="4" height="7" rx="1.5"/><path d="M6 13v7M4 13h2"/>',
    "text": '<path d="M4 7V5h16v2M12 5v14M9 19h6"/>',
    "null": '<circle cx="12" cy="12" r="8"/><path d="M6.5 17.5l11-11"/>',
    "list": '<path d="M9 6h12M9 12h12M9 18h12"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
    "history": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3.5 2"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "memory": '<rect x="4" y="6" width="16" height="12" rx="2"/><path d="M8 6V3M12 6V3M16 6V3M8 21v-3M12 21v-3M16 21v-3"/><rect x="8" y="10" width="8" height="4" rx="1"/>',
    "sparkle": '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 15l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"/>',
    "keyboard": '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>',
    "grip": '<circle cx="9" cy="6" r="1"/><circle cx="15" cy="6" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="9" cy="18" r="1"/><circle cx="15" cy="18" r="1"/>',
    "arrow-up": '<path d="M12 19V5M6 11l6-6 6 6"/>',
    "arrow-down": '<path d="M12 5v14M6 13l6 6 6-6"/>',
    "wrap": '<path d="M3 6h18M3 12h15a3 3 0 0 1 0 6h-4"/><path d="M16 15l-3 3 3 3"/><path d="M3 18h7"/>',
    "find-replace": '<circle cx="10" cy="10" r="6"/><path d="M14.5 14.5L20 20"/><path d="M7.5 10h5"/>',
    "format": '<path d="M4 6h16M4 10h10M4 14h16M4 18h10"/>',
    "comment": '<path d="M8 7l-4 5 4 5M16 7l4 5-4 5M13.5 5l-3 14"/>',
}

ICON_NAMES = sorted(_P)

_FILLED = {"play", "play-line", "stop", "trigger"}


def svg_markup(name: str, color: str, stroke_width: float = 1.8) -> str:
    body = _P.get(name, _P["info"])
    fill = color if name in _FILLED else "none"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="{fill}" stroke="{color}" '
            f'stroke-width="{stroke_width}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')


def render_pixmap(name: str, color: str, size: int = 16, dpr: float = 2.0) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg_markup(name, color).encode("utf-8")))
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p, QRectF(0, 0, size * dpr, size * dpr))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


@lru_cache(maxsize=1024)
def _icon(name: str, color: str, disabled: str, active: str) -> QIcon:
    ic = QIcon()
    for sz in (16, 20, 24, 32):
        ic.addPixmap(render_pixmap(name, color, sz), QIcon.Normal, QIcon.Off)
        ic.addPixmap(render_pixmap(name, disabled, sz), QIcon.Disabled, QIcon.Off)
        ic.addPixmap(render_pixmap(name, active, sz), QIcon.Normal, QIcon.On)
        ic.addPixmap(render_pixmap(name, active, sz), QIcon.Active, QIcon.On)
    return ic


def icon(name: str, color: str | None = None) -> QIcon:
    """Theme-tinted icon. ``color`` may be a hex colour or a token name like 'accent'."""
    from .theme import tokens

    t = tokens()
    c = getattr(t, color) if color and hasattr(t, color) else (color or t.text_muted)
    return _icon(name, c, t.text_faint, t.accent)


ICON_SIZE = QSize(16, 16)

_qss_dir: str | None = None


def register_qss_resources(t) -> str:
    """Write the few SVGs that the style sheet references to a cache folder; returns that folder."""
    global _qss_dir
    folder = os.path.join(tempfile.gettempdir(), "sqliteview-pyside-icons", t.name)
    os.makedirs(folder, exist_ok=True)
    for fname, name, color, sw in (("check.svg", "check", t.accent_text if t.name == "dark" else "#ffffff", 3.0),
                                   ("x.svg", "x", t.text_muted, 2.0), ("float.svg", "float", t.text_muted, 2.0),
                                   ("chevron-down.svg", "chevron-down", t.text_muted, 2.2)):
        path = os.path.join(folder, fname)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg_markup(name, color, sw))
    _qss_dir = folder.replace("\\", "/")
    return _qss_dir


# --------------------------------------------------------------------------- theme refresh
_bindings: list = []


def bind_icon(obj, name: str, color: str | None = None):
    """Set an icon on a QAction/QAbstractButton and keep it in sync with theme changes."""
    import weakref

    obj.setIcon(icon(name, color))
    _bindings.append((weakref.ref(obj), name, color))
    return obj


def refresh_bound_icons() -> None:
    from shiboken6 import isValid

    alive = []
    for ref, name, color in _bindings:
        obj = ref()
        if obj is None or not isValid(obj):
            continue
        obj.setIcon(icon(name, color))
        alive.append((ref, name, color))
    _bindings[:] = alive
