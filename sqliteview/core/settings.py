"""Persistent settings with the upstream defaults (port of ``src/Settings.{h,cpp}``)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings

ORG_NAME = "sqliteview"
APP_NAME = "sqliteview"

DEFAULTS: dict[tuple[str, str], Any] = {
    ("db", "defaultencoding"): "UTF-8",
    ("db", "defaultlocation"): str(Path.home()),
    ("db", "foreignkeys"): True,
    ("db", "prefetchsize"): 50000,
    ("db", "defaultsqltext"): "",
    ("db", "hideschemalinebreaks"): True,
    ("exportcsv", "firstrowheader"): True,
    ("exportcsv", "separator"): ",",
    ("exportcsv", "quotecharacter"): '"',
    ("exportcsv", "newlinecharacters"): "\r\n",
    ("importcsv", "firstrowheader"): True,
    ("importcsv", "trimfields"): True,
    ("importcsv", "separatetables"): False,
    ("importcsv", "separator"): ",",
    ("importcsv", "quotecharacter"): '"',
    ("importcsv", "encoding"): "UTF-8",
    ("importcsv", "notypedetection"): False,
    ("exportsql", "insertcolnames"): False,
    ("exportsql", "insertmultiple"): False,
    ("exportsql", "keeporiginal"): False,
    ("exportsql", "oldschema"): 0,
    ("exportjson", "prettyprint"): True,
    ("General", "recentFileList"): [],
    ("General", "maxRecentFiles"): 10,
    ("General", "theme"): "system",  # system | light | dark
    ("General", "uiDensity"): "comfortable",  # comfortable | compact
    ("General", "fontsize"): 0,  # 0 = platform default
    ("General", "DBFileExtensions"): "SQLite database files (*.db *.sqlite *.sqlite3 *.db3)",
    ("databrowser", "font"): "",  # "" = interface font
    ("databrowser", "fontsize"): 0,  # 0 = interface size
    ("databrowser", "symbol_limit"): 5000,
    ("databrowser", "rows_limit"): 10_000_000,
    ("databrowser", "complete_threshold"): 1000,
    ("databrowser", "image_preview"): False,
    ("databrowser", "null_text"): "NULL",
    ("databrowser", "blob_text"): "BLOB",
    ("databrowser", "filter_escape"): "\\",
    ("databrowser", "filter_delay"): 200,
    ("databrowser", "cell_word_wrap"): True,
    ("editor", "font"): "",  # "" = best available monospace font
    ("editor", "fontsize"): 10,
    ("editor", "tabsize"): 4,
    ("editor", "wrap_lines"): False,
    ("editor", "auto_completion"): True,
    ("editor", "upper_keywords"): True,
    ("editor", "error_indicators"): True,
    ("editor", "identifier_quotes"): 0,
    ("extensions", "list"): [],
    ("extensions", "disableregex"): False,
    ("extensions", "enable_load_extension"): False,
    ("MainWindow", "geometry"): None,
    ("MainWindow", "windowState"): None,
}


class Settings:
    _qs: QSettings | None = None
    _cache: dict[tuple[str, str], Any] = {}

    @classmethod
    def _store(cls) -> QSettings:
        if cls._qs is None:
            cls._qs = QSettings(ORG_NAME, APP_NAME)
        return cls._qs

    @classmethod
    def use_file(cls, path: str) -> None:
        """Store settings in an INI file instead of the platform default (used by tests)."""
        cls._qs = QSettings(path, QSettings.IniFormat)
        cls._cache.clear()

    @classmethod
    def get(cls, group: str, name: str) -> Any:
        key = (group, name)
        if key in cls._cache:
            return cls._cache[key]
        default = DEFAULTS.get(key)
        raw = cls._store().value(f"{group}/{name}", default)
        value = cls._coerce(raw, default)
        cls._cache[key] = value
        return value

    @classmethod
    def set(cls, group: str, name: str, value: Any) -> None:
        cls._cache[(group, name)] = value
        cls._store().setValue(f"{group}/{name}", value)

    @classmethod
    def reset(cls) -> None:
        cls._store().clear()
        cls._cache.clear()

    @staticmethod
    def _coerce(raw: Any, default: Any) -> Any:
        # QSettings on Windows returns strings for everything stored via the registry
        if default is None or raw is None:
            return raw
        if isinstance(default, bool):
            if isinstance(raw, str):
                return raw.lower() in ("true", "1", "yes")
            return bool(raw)
        if isinstance(default, int):
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default
        if isinstance(default, list):
            if raw in ("", None):
                return []
            return list(raw) if isinstance(raw, (list, tuple)) else [raw]
        return raw
