"""Application bootstrap (port of upstream ``Application``/``main.cpp``)."""
from __future__ import annotations

import argparse
import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from . import APP_DISPLAY_NAME, __version__
from .core import sqlb
from .core.settings import APP_NAME, ORG_NAME, Settings


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="sqliteview", description=f"{APP_DISPLAY_NAME} — SQLite database browser")
    p.add_argument("database", nargs="?", help="database file or project file (.sqbpro) to open")
    p.add_argument("-R", "--read-only", action="store_true", help="open the database read-only")
    p.add_argument("-s", "--sql", metavar="FILE", help="open a SQL file in an editor tab")
    p.add_argument("-t", "--table", metavar="TABLE", help="browse this table after opening")
    p.add_argument("-q", "--quit", action="store_true", help="exit after running the SQL file (with --sql)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setOrganizationName(ORG_NAME)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(__version__)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("sqliteview")
        except (AttributeError, OSError):
            pass

    from .ui.theme import theme
    from .ui.main_window import MainWindow

    sqlb.set_identifier_quoting(sqlb.EscapeQuoting(int(Settings.get("editor", "identifier_quotes"))))
    theme.apply()
    # Follow the OS light/dark switch when the theme is set to "system"
    try:
        QGuiApplication.styleHints().colorSchemeChanged.connect(
            lambda *_: Settings.get("General", "theme") == "system" and theme.apply())
    except AttributeError:
        pass

    win = MainWindow()
    win.show()

    def startup() -> None:
        if args.database:
            if win.open_database(os.path.abspath(args.database), args.read_only) and args.table:
                win.open_table(sqlb.ObjectIdentifier("main", args.table))
        if args.sql:
            tab = win.open_sql_tab(path=os.path.abspath(args.sql))
            if win.db.is_open():
                tab.execute_all()
                if args.quit:
                    def done(running: bool) -> None:
                        if not running:
                            win.db.release_all_savepoints()
                            app.quit()
                    tab.runningChanged.connect(done)

    QTimer.singleShot(0, startup)
    return app.exec()
