"""Main window. Replaces upstream ``MainWindow`` with a document-tab workspace:

* left: schema explorer, right: cell inspector, bottom: SQL log and plot (all dockable)
* centre: welcome page, or tabs for tables, SQL editors and the database info page
* top: a compact toolbar with the transaction state (Write / Revert) always visible
* Ctrl+K command palette for every action, table and column
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QByteArray, QModelIndex, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDockWidget, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMainWindow,
                               QMenu, QMessageBox, QProgressDialog, QPushButton, QSizePolicy, QStackedWidget,
                               QTabWidget, QToolBar, QToolButton, QWidget)

from .. import APP_DISPLAY_NAME, __version__
from ..core import project as sqbpro
from ..core import sqlb
from ..core.db import DBBrowserDB
from ..core.settings import Settings
from .command_palette import CommandPalette, PaletteEntry
from .db_info_tab import DbInfoTab
from .explorer import SchemaExplorer
from .icons import app_icon, bind_icon, icon, refresh_bound_icons
from .inspector import CellInspector
from .log_panel import LogPanel
from .plot_dock import PlotPanel
from .sql_tab import SqlTab
from .schema_tab import SchemaTab
from .table_tab import TableTab
from .theme import theme
from .welcome import WelcomePage
from .widgets import Toast, label, make_action

DB_FILTER_ALL = "All files (*)"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db = DBBrowserDB(self)
        self.db.busy_handler = self._ask_interrupt
        self.db.collation_handler = self._ask_collation
        self.db.warning_handler = lambda msg: QMessageBox.warning(self, APP_DISPLAY_NAME, msg)
        self.project_path = ""  # current .sqbpro file, if the database was opened from or saved to one
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        self.setDockNestingEnabled(True)
        self.resize(1360, 860)
        self._build_actions()
        self._build_central()
        self._build_docks()
        self._build_toolbar()
        self._build_menus()
        self._build_statusbar()
        self.palette = CommandPalette(self, self._palette_entries)
        self.toast = Toast(self)
        self.db.sqlExecuted.connect(self.log_panel.log)
        self.db.dbChanged.connect(self._update_state)
        self.db.structureUpdated.connect(self._on_structure_updated)
        self.db.databaseInUseChanged.connect(self._on_busy_changed)
        theme.changed.connect(refresh_bound_icons)
        theme.changed.connect(self._on_theme_changed)
        self._restore_window()
        self._update_state()

    # ================================================================== construction
    def _build_actions(self) -> None:
        A = make_action
        self.act_new = A(self, "&New Database…", "database-plus", QKeySequence.New, self.new_database)
        self.act_new_memory = A(self, "New &In-Memory Database", "memory", "", self.new_memory_database)
        self.act_open = A(self, "&Open Database…", "folder-open", QKeySequence.Open, lambda: self.open_database_dialog())
        self.act_open_ro = A(self, "Open Database &Read-Only…", "lock", "Ctrl+Shift+O", lambda: self.open_database_dialog(True))
        self.act_attach = A(self, "&Attach Database…", "attach", "", self.attach_database)
        self.act_close = A(self, "&Close Database", "x", "Ctrl+F4", self.close_database)
        self.act_write = A(self, "&Write Changes", "check", QKeySequence.Save, self.write_changes,
                           tip="Commit all pending changes to the database file")
        self.act_revert = A(self, "&Revert Changes", "undo", "", self.revert_changes,
                            tip="Discard all changes since the last write")
        self.act_save_as = A(self, "Save Database &As…", "save", "Ctrl+Alt+S", self.save_database_as)
        self.act_open_project = A(self, "Open &Project…", "folder-open", "", self.open_project_dialog,
                                  tip="Open a database together with its tabs, filters and layout")
        self.act_save_project = A(self, "Save Pro&ject As…", "save", "", self.save_project_as,
                                  tip="Save the database path, open tabs, filters and layout in a project file")
        self.act_import_sql = A(self, "Database from &SQL file…", "file-code", "", self.import_sql)
        self.act_import_csv = A(self, "Table from &CSV file…", "upload", "", lambda: self.import_csv())
        self.act_export_sql = A(self, "Database to &SQL file…", "file-code", "", lambda: self.export_sql())
        self.act_export_csv = A(self, "Table(s) as &CSV file…", "download", "", lambda: self.export_data("csv"))
        self.act_export_json = A(self, "Table(s) to &JSON…", "braces", "", lambda: self.export_data("json"))
        self.act_exit = A(self, "E&xit", "", QKeySequence.Quit, self.close)

        self.act_create_table = A(self, "Create &Table…", "table-plus", "Ctrl+Shift+T", lambda: self.edit_table(None))
        self.act_modify_table = A(self, "&Modify Table…", "table-edit", "", self.modify_current_table)
        self.act_delete_object = A(self, "&Delete Object…", "trash", "", self.delete_current_object)
        self.act_create_index = A(self, "Create &Index…", "index", "", lambda: self.create_index(None))
        self.act_prefs = A(self, "&Preferences…", "settings", "Ctrl+,", self.show_preferences)

        self.act_new_sql = A(self, "New &SQL Editor", "terminal", "Ctrl+T", lambda: self.open_sql_tab())
        self.act_open_sql = A(self, "Open SQL &File…", "file-code", "", self.open_sql_file)
        self.act_execute = A(self, "&Execute SQL", "play", ["Ctrl+Return", "F5", "Ctrl+R"], self._execute_or_refresh)
        self.act_execute_current = A(self, "Execute &Current Statement", "play-cursor", "Shift+F5",
                                     lambda: self._sql_tab_call("execute_current"))
        self.act_execute_line = A(self, "Execute &Line", "play-line", "Shift+F6", lambda: self._sql_tab_call("execute_line"))
        self.act_stop = A(self, "S&top Execution", "stop", "", lambda: self._sql_tab_call("stop"))
        self.act_close_tab = A(self, "Close &Tab", "x", QKeySequence.Close, self.close_current_tab)
        self.act_info = A(self, "Database &Info && Pragmas", "info", "Ctrl+I", self.open_info_tab)
        self.act_schema = A(self, "Database &Schema", "file-code", "Ctrl+Shift+D", lambda: self.open_schema_tab(None))
        self.act_palette = A(self, "Command &Palette…", "command", ["Ctrl+K", "Ctrl+Shift+P"], lambda: self.palette.open())

        self.act_vacuum = A(self, "&Compact Database (VACUUM)…", "broom", "", self.compact_database)
        self.act_integrity = A(self, "&Integrity Check", "shield", "", lambda: self.run_check("PRAGMA integrity_check;"))
        self.act_quick_check = A(self, "&Quick Integrity Check", "shield", "", lambda: self.run_check("PRAGMA quick_check;"))
        self.act_fk_check = A(self, "&Foreign-Key Check", "link", "", lambda: self.run_check("PRAGMA foreign_key_check;"))
        self.act_optimize = A(self, "&Optimize", "sparkle", "", lambda: self.run_check("PRAGMA optimize;"))
        self.act_load_ext = A(self, "&Load Extension…", "wrench", "", self.load_extension)

        self.act_about = A(self, "&About", "info", "", self.show_about)
        self.act_shortcuts = A(self, "&Keyboard Shortcuts", "keyboard", "F1", self.show_shortcuts)
        self.act_theme_toggle = A(self, "Toggle Light/Dark Theme", "moon", "Ctrl+Shift+L", self.toggle_theme)
        self.act_full_screen = A(self, "Full Screen Table / Results", "maximize", "F11", self._toggle_current_full_screen)
        self.act_exit_full_screen = A(self, "Exit Full Screen", "minimize", "Esc", lambda: self._set_full_screen(False))
        self.act_exit_full_screen.setEnabled(False)
        self.addAction(self.act_full_screen)
        self.addAction(self.act_exit_full_screen)

        for a in (self.act_execute, self.act_execute_current, self.act_execute_line, self.act_palette, self.act_close_tab,
                  self.act_new_sql, self.act_info, self.act_theme_toggle):
            a.setShortcutContext(Qt.WindowShortcut)
            self.addAction(a)

    def _build_central(self) -> None:
        self.stack = QStackedWidget()
        self.welcome = WelcomePage()
        self.welcome.newRequested.connect(self.new_database)
        self.welcome.openRequested.connect(lambda: self.open_database_dialog())
        self.welcome.openReadOnlyRequested.connect(lambda: self.open_database_dialog(True))
        self.welcome.memoryRequested.connect(self.new_memory_database)
        self.welcome.recentRequested.connect(self.open_database)
        self.welcome.paletteRequested.connect(lambda: self.palette.open())
        self.stack.addWidget(self.welcome)

        self.tabs = QTabWidget()
        self.tabs.setProperty("role", "documents")
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setElideMode(Qt.ElideMiddle)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._tab_context_menu)
        new_tab_btn = QToolButton()
        bind_icon(new_tab_btn, "plus")
        new_tab_btn.setToolTip("New SQL editor (Ctrl+T)")
        new_tab_btn.setAutoRaise(True)
        new_tab_btn.clicked.connect(lambda: self.open_sql_tab())
        self.tabs.setCornerWidget(new_tab_btn, Qt.TopRightCorner)
        self.stack.addWidget(self.tabs)
        self.setCentralWidget(self.stack)

    def _make_dock(self, title: str, name: str, widget: QWidget, area: Qt.DockWidgetArea) -> QDockWidget:
        d = QDockWidget(title, self)
        d.setObjectName(name)
        d.setWidget(widget)
        d.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(area, d)
        return d

    def _build_docks(self) -> None:
        self.explorer = SchemaExplorer()
        self.explorer.set_db(self.db)
        self.explorer.openRequested.connect(self.open_table)
        self.explorer.structureRequested.connect(lambda ident: self.open_table(ident, view=1))
        self.explorer.modifyRequested.connect(self.edit_table)
        self.explorer.dropRequested.connect(self.drop_object)
        self.explorer.createTableRequested.connect(lambda: self.edit_table(None))
        self.explorer.createIndexRequested.connect(self.create_index)
        self.explorer.exportRequested.connect(lambda ident: self.export_data("csv", ident))
        self.explorer.insertSqlRequested.connect(lambda sql: self.open_sql_tab(sql))
        self.explorer.copySqlRequested.connect(lambda sql: self.open_sql_tab(sql))
        self.explorer.schemaRequested.connect(self.open_schema_tab)
        self.dock_explorer = self._make_dock("Explorer", "dock_explorer", self.explorer, Qt.LeftDockWidgetArea)
        self.inspector = CellInspector()
        self.inspector.statusMessage.connect(self.notify)
        self.dock_inspector = self._make_dock("Inspector", "dock_inspector", self.inspector, Qt.RightDockWidgetArea)
        self.log_panel = LogPanel()
        self.dock_log = self._make_dock("SQL Log", "dock_log", self.log_panel, Qt.BottomDockWidgetArea)
        self.dock_log.hide()
        self.plot = PlotPanel()
        self.dock_plot = self._make_dock("Plot", "dock_plot", self.plot, Qt.BottomDockWidgetArea)
        self.dock_plot.hide()
        self.explorer.setMinimumWidth(250)
        self.inspector.setMinimumWidth(260)
        QTimer.singleShot(0, lambda: self.resizeDocks([self.dock_explorer, self.dock_inspector], [270, 330], Qt.Horizontal)
                          if not Settings.get("MainWindow", "windowState") else None)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setObjectName("toolbar_main")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.setIconSize(tb.iconSize().scaled(16, 16, Qt.KeepAspectRatio))
        self.addToolBar(Qt.TopToolBarArea, tb)
        self.toolbar = tb

        self.db_button = QToolButton()
        self.db_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        bind_icon(self.db_button, "database", "accent")
        self.db_button.setPopupMode(QToolButton.InstantPopup)
        self.db_button.setText("No database")
        self.db_button.setStyleSheet("font-weight: 600;")
        tb.addWidget(self.db_button)
        tb.addSeparator()

        open_btn = QToolButton()
        open_btn.setDefaultAction(self.act_open)
        self.act_open.setIconText("Open")
        open_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.recent_menu = QMenu("Open &Recent", self)
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        open_btn.setMenu(self.recent_menu)
        open_btn.setPopupMode(QToolButton.MenuButtonPopup)
        tb.addWidget(open_btn)
        new_btn = QToolButton()
        new_btn.setDefaultAction(self.act_new)
        self.act_new.setIconText("New")
        new_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.addWidget(new_btn)
        tb.addSeparator()

        self.write_btn = QToolButton()
        self.write_btn.setDefaultAction(self.act_write)
        self.act_write.setIconText("Write Changes")
        self.write_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.addWidget(self.write_btn)
        revert_btn = QToolButton()
        revert_btn.setDefaultAction(self.act_revert)
        self.act_revert.setIconText("Revert")
        revert_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.addWidget(revert_btn)
        tb.addSeparator()
        sql_btn = QToolButton()
        sql_btn.setDefaultAction(self.act_new_sql)
        self.act_new_sql.setIconText("SQL")
        sql_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.addWidget(sql_btn)
        tbl_btn = QToolButton()
        tbl_btn.setDefaultAction(self.act_create_table)
        self.act_create_table.setIconText("Table")
        tbl_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.addWidget(tbl_btn)
        io_btn = QToolButton()
        io_btn.setText("Import / Export")
        bind_icon(io_btn, "download")
        io_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        io_btn.setPopupMode(QToolButton.InstantPopup)
        io_menu = QMenu(self)
        io_menu.addSection("Import")
        io_menu.addAction(self.act_import_csv)
        io_menu.addAction(self.act_import_sql)
        io_menu.addSection("Export")
        io_menu.addAction(self.act_export_csv)
        io_menu.addAction(self.act_export_json)
        io_menu.addAction(self.act_export_sql)
        io_btn.setMenu(io_menu)
        tb.addWidget(io_btn)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        search = QPushButton()
        search.setProperty("kind", "search")
        search.setMinimumWidth(240)
        search.setCursor(Qt.PointingHandCursor)
        sl = QHBoxLayout(search)
        sl.setContentsMargins(10, 0, 8, 0)
        sl.setSpacing(8)
        ic = QLabel()
        ic.setPixmap(icon("search").pixmap(14, 14))
        self._search_icon = ic
        sl.addWidget(ic)
        sl.addWidget(label("Search commands, tables, columns…", "faint"), 1)
        sl.addWidget(label("Ctrl K", "chip"))
        search.clicked.connect(lambda: self.palette.open())
        tb.addWidget(search)
        spacer2 = QWidget()
        spacer2.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer2)
        for act, name in ((self.dock_explorer.toggleViewAction(), "panel-left"), (self.dock_log.toggleViewAction(), "panel-bottom"),
                          (self.dock_plot.toggleViewAction(), "chart"), (self.dock_inspector.toggleViewAction(), "panel-right")):
            bind_icon(act, name)
            b = QToolButton()
            b.setDefaultAction(act)
            b.setToolButtonStyle(Qt.ToolButtonIconOnly)
            tb.addWidget(b)
        self.theme_btn = QToolButton()
        self.theme_btn.setDefaultAction(self.act_theme_toggle)
        self.theme_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        tb.addWidget(self.theme_btn)
        prefs_btn = QToolButton()
        prefs_btn.setDefaultAction(self.act_prefs)
        prefs_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        tb.addWidget(prefs_btn)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        for a in (self.act_new, self.act_new_memory, self.act_open, self.act_open_ro):
            m.addAction(a)
        m.addMenu(self.recent_menu)
        m.addAction(self.act_attach)
        m.addSeparator()
        m.addAction(self.act_write)
        m.addAction(self.act_revert)
        m.addAction(self.act_save_as)
        m.addAction(self.act_close)
        m.addSeparator()
        m.addAction(self.act_open_project)
        m.addAction(self.act_save_project)
        m.addSeparator()
        im = m.addMenu(icon("upload"), "&Import")
        im.addAction(self.act_import_sql)
        im.addAction(self.act_import_csv)
        em = m.addMenu(icon("download"), "&Export")
        em.addAction(self.act_export_sql)
        em.addAction(self.act_export_csv)
        em.addAction(self.act_export_json)
        m.addSeparator()
        m.addAction(self.act_exit)

        m = mb.addMenu("&Edit")
        for a in (self.act_create_table, self.act_modify_table, self.act_delete_object, self.act_create_index):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.act_prefs)

        m = mb.addMenu("&View")
        m.addAction(self.act_palette)
        m.addAction(self.act_full_screen)
        m.addAction(self.act_info)
        m.addAction(self.act_schema)
        m.addSeparator()
        for d in (self.dock_explorer, self.dock_inspector, self.dock_log, self.dock_plot):
            m.addAction(d.toggleViewAction())
        m.addSeparator()
        tm = m.addMenu("&Theme")
        grp = QActionGroup(self)
        for key, text in (("system", "Follow System"), ("light", "Light"), ("dark", "Dark")):
            a = tm.addAction(text)
            a.setCheckable(True)
            a.setChecked(Settings.get("General", "theme") == key)
            a.triggered.connect(lambda _c=False, k=key: self.set_theme(k))
            grp.addAction(a)
        self.theme_group = grp
        m.addAction(self.act_theme_toggle)

        m = mb.addMenu("&SQL")
        for a in (self.act_new_sql, self.act_open_sql, self.act_execute, self.act_execute_current, self.act_execute_line,
                  self.act_stop):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.act_close_tab)

        m = mb.addMenu("&Tools")
        for a in (self.act_vacuum, self.act_integrity, self.act_quick_check, self.act_fk_check, self.act_optimize):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.act_load_ext)

        m = mb.addMenu("&Help")
        m.addAction(self.act_shortcuts)
        m.addAction(self.act_about)

        db_menu = QMenu(self)
        for a in (self.act_info, self.act_attach, self.act_save_as, self.act_close):
            db_menu.addAction(a)
        db_menu.addSeparator()
        db_menu.addAction("Show in Folder", self._reveal_db)
        self.db_button.setMenu(db_menu)

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        sb.setSizeGripEnabled(True)
        self.status_busy = label("", "chip-accent")
        self.status_busy.hide()
        self.status_dirty = label("● Uncommitted changes", "chip-warning")
        self.status_dirty.hide()
        self.status_ro = label("Read-only", "chip-danger")
        self.status_ro.hide()
        self.status_encoding = label("", "faint")
        self.status_version = label(f"SQLite {DBBrowserDB.sqlite_version()}", "faint")
        for w in (self.status_busy, self.status_dirty, self.status_ro, self.status_encoding, self.status_version):
            sb.addPermanentWidget(w)

    # ================================================================== helpers
    def notify(self, msg: str) -> None:
        if msg:
            self.statusBar().showMessage(msg, 4000)
            self.toast.show_message(msg)

    def _ask_interrupt(self, user: str) -> bool:
        r = QMessageBox.question(self, APP_DISPLAY_NAME,
                                 f"The database is currently busy: {user}\n\nDo you want to abort that other operation?",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return r == QMessageBox.Yes

    def _ask_collation(self, name: str) -> bool:
        r = QMessageBox.question(
            self, APP_DISPLAY_NAME,
            f"The database uses the collation sequence '{name}', which this application doesn't provide.\n\n"
            "Continue with a substitute (binary comparison)? Sorting and comparisons using it may differ.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        return r == QMessageBox.Yes

    def _restore_window(self) -> None:
        geo = Settings.get("MainWindow", "geometry")
        state = Settings.get("MainWindow", "windowState")
        if geo:
            self.restoreGeometry(QByteArray(geo) if not isinstance(geo, QByteArray) else geo)
        if state:
            self.restoreState(QByteArray(state) if not isinstance(state, QByteArray) else state)
        self.welcome.set_recent(self._recent_files())

    def _recent_files(self) -> list[str]:
        return [f for f in (Settings.get("General", "recentFileList") or []) if f]

    def _add_recent(self, path: str) -> None:
        if path == ":memory:":
            return
        path = os.path.abspath(path)
        files = [f for f in self._recent_files() if os.path.normcase(f) != os.path.normcase(path)]
        files.insert(0, path)
        Settings.set("General", "recentFileList", files[: int(Settings.get("General", "maxRecentFiles"))])
        self.welcome.set_recent(self._recent_files())

    def _fill_recent_menu(self) -> None:
        self.recent_menu.clear()
        files = self._recent_files()
        for i, f in enumerate(files):
            a = self.recent_menu.addAction(icon("database"), f"&{i + 1}  {os.path.basename(f)}")
            a.setToolTip(f)
            a.setStatusTip(f)
            a.triggered.connect(lambda _c=False, p=f: self.open_database(p))
        if not files:
            self.recent_menu.addAction("No recent files").setEnabled(False)
        else:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("Clear Recent Files", lambda: (Settings.set("General", "recentFileList", []),
                                                                      self.welcome.set_recent([])))

    def _update_state(self, *_) -> None:
        is_open = self.db.is_open()
        dirty = self.db.dirty
        ro = self.db.read_only
        self.stack.setCurrentIndex(1 if is_open else 0)
        for a in (self.act_close, self.act_attach, self.act_info, self.act_schema, self.act_new_sql, self.act_open_sql, self.act_execute,
                  self.act_execute_current, self.act_execute_line, self.act_export_sql, self.act_export_csv,
                  self.act_export_json, self.act_integrity, self.act_quick_check, self.act_fk_check, self.act_load_ext,
                  self.act_save_as, self.act_save_project):
            a.setEnabled(is_open)
        for a in (self.act_create_table, self.act_create_index, self.act_import_csv, self.act_import_sql, self.act_vacuum,
                  self.act_optimize, self.act_modify_table, self.act_delete_object):
            a.setEnabled(is_open and not ro)
        self.act_import_sql.setEnabled(not ro)
        self.act_import_csv.setEnabled(not ro)
        self.act_write.setEnabled(is_open and dirty)
        self.act_revert.setEnabled(is_open and dirty)
        self.write_btn.setProperty("kind", "warn" if dirty else "")
        self.write_btn.style().unpolish(self.write_btn)
        self.write_btn.style().polish(self.write_btn)
        self.status_dirty.setVisible(dirty)
        self.status_ro.setVisible(is_open and ro)
        if is_open:
            name = "In-memory database" if self.db.cur_file == ":memory:" else os.path.basename(self.db.cur_file)
            self.db_button.setText(name)
            self.db_button.setToolTip(self.db.cur_file)
            self.setWindowTitle(f"{'● ' if dirty else ''}{name} — {APP_DISPLAY_NAME}")
        else:
            self.db_button.setText("No database")
            self.db_button.setToolTip("")
            self.setWindowTitle(APP_DISPLAY_NAME)
            self.status_encoding.setText("")
        self.db_button.setEnabled(is_open)

    def _on_busy_changed(self, busy: bool, user: str) -> None:
        self.status_busy.setVisible(busy)
        self.status_busy.setText(f"⏳ {user}" if busy else "")

    def _on_theme_changed(self) -> None:
        self.act_theme_toggle.setIcon(icon("sun" if theme.tokens.name == "dark" else "moon"))
        self._search_icon.setPixmap(icon("search").pixmap(14, 14))
        for i in range(self.tabs.count()):
            self._update_tab_icon(i)

    # ================================================================== database lifecycle
    def _confirm_close_db(self) -> bool:
        """Port of the save/discard prompt in ``DBBrowserDB::close``."""
        if not self.db.is_open():
            return True
        if self.db.dirty:
            if self.db.cur_file == ":memory:":
                r = QMessageBox.question(self, APP_DISPLAY_NAME,
                                         "Do you really want to close this temporary database? All data will be lost.",
                                         QMessageBox.Yes | QMessageBox.Cancel)
                if r == QMessageBox.Cancel:
                    return False
            else:
                r = QMessageBox.question(self, APP_DISPLAY_NAME,
                                         f"Do you want to save the changes made to the database file {self.db.cur_file}?",
                                         QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
                if r == QMessageBox.Cancel:
                    return False
                if r == QMessageBox.Save and not self.db.release_all_savepoints():
                    QMessageBox.warning(self, APP_DISPLAY_NAME, f"Writing changes failed:\n{self.db.last_error}")
                    return False
                if r == QMessageBox.Discard:
                    self.db.revert_all()
        # SQL tabs that are still running
        for tab in self._tabs_of(SqlTab):
            if tab.is_running():
                tab.stop()
        return True

    def close_database(self) -> bool:
        if not self._confirm_close_db():
            return False
        for i in reversed(range(self.tabs.count())):
            w = self.tabs.widget(i)
            if isinstance(w, SqlTab) and w.is_modified():
                continue  # keep unsaved SQL around; it doesn't depend on the DB
            self._remove_tab(i)
        self.inspector.clear()
        self.db.close(force=True)
        self.project_path = ""
        self._update_state()
        return True

    def open_database_dialog(self, read_only: bool = False) -> None:
        start = os.path.dirname(self._recent_files()[0]) if self._recent_files() else Settings.get("db", "defaultlocation")
        path, _ = QFileDialog.getOpenFileName(self, "Open Database" + (" Read-Only" if read_only else ""), start,
                                              f"{Settings.get('General', 'DBFileExtensions')};;{sqbpro.PROJECT_FILTER};;"
                                              f"{DB_FILTER_ALL}")
        if path:
            self.open_database(path, read_only)

    def open_database(self, path: str, read_only: bool = False) -> bool:
        if not path:
            return False
        if sqbpro.is_project_file(path):  # recent files, drag & drop and the command line can name a project
            return self.open_project(path, read_only)
        if self.db.is_open() and not self.close_database():
            return False
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok = self.db.open(path, read_only)
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            QMessageBox.warning(self, APP_DISPLAY_NAME, f"Could not open database file.\nReason: {self.db.last_error}")
            return False
        self._add_recent(path)
        self._after_open()
        return True

    def _after_open(self) -> None:
        enc = self.db.get_pragma("encoding")
        self.status_encoding.setText(str(enc or ""))
        self._update_state()
        self.explorer.populate()
        self._refresh_sql_schema()
        if not self.tabs.count():
            self.open_info_tab()
        self.notify(f"Opened {os.path.basename(self.db.cur_file) or 'database'}"
                    + (" (read-only)" if self.db.read_only else ""))

    def new_database(self) -> None:
        if self.db.is_open() and not self.close_database():
            return
        path, _ = QFileDialog.getSaveFileName(self, "Create New Database", Settings.get("db", "defaultlocation"),
                                              f"{Settings.get('General', 'DBFileExtensions')};;{DB_FILTER_ALL}")
        if not path:
            return
        if os.path.exists(path):
            try:
                os.remove(path)  # the save dialog already asked to overwrite
            except OSError as e:
                QMessageBox.warning(self, APP_DISPLAY_NAME, f"Couldn't replace the file:\n{e}")
                return
        if not self.db.create(path):
            QMessageBox.warning(self, APP_DISPLAY_NAME, f"Creating the database failed:\n{self.db.last_error}")
            return
        self._add_recent(path)
        self._after_open()
        self.edit_table(None)

    def new_memory_database(self) -> None:
        if self.db.is_open() and not self.close_database():
            return
        if not self.db.open(":memory:"):
            QMessageBox.warning(self, APP_DISPLAY_NAME, self.db.last_error)
            return
        self._after_open()

    def write_changes(self) -> None:
        if not self.db.is_open():
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok = self.db.release_all_savepoints()
        finally:
            QApplication.restoreOverrideCursor()
        if ok:
            self.notify("All changes written to disk")
        else:
            QMessageBox.warning(self, APP_DISPLAY_NAME, f"Writing changes failed:\n{self.db.last_error}")
        self._update_state()

    def revert_changes(self) -> None:
        if not self.db.dirty:
            return
        if QMessageBox.question(self, APP_DISPLAY_NAME,
                                "Are you sure you want to undo all changes made to the database file since the last save?",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.db.revert_all()
        self.db.update_schema()
        self._reload_table_tabs()
        self.notify("Changes reverted")

    def save_database_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Database As", "", f"{Settings.get('General', 'DBFileExtensions')};;{DB_FILTER_ALL}")
        if not path:
            return
        if self.db.dirty and QMessageBox.question(self, APP_DISPLAY_NAME, "Pending changes will be written first. Continue?") != QMessageBox.Yes:
            return
        self.db.release_all_savepoints()
        if os.path.exists(path):
            os.remove(path)
        if self.db.save_as(path):
            self._add_recent(path)
            self._update_state()
            self.notify(f"Saved as {os.path.basename(path)}")
        else:
            QMessageBox.warning(self, APP_DISPLAY_NAME, f"Saving failed:\n{self.db.last_error}")

    def attach_database(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Attach Database", "", f"{Settings.get('General', 'DBFileExtensions')};;{DB_FILTER_ALL}")
        if not path:
            return
        name, ok = QInputDialog.getText(self, "Attach Database",
                                        "Please specify the database name under which you want to access the attached database",
                                        text=os.path.splitext(os.path.basename(path))[0])
        if not ok or not name.strip():
            return
        if self.db.attach(path, name.strip()):
            self.notify(f"Attached as '{name.strip()}'")
        else:
            QMessageBox.warning(self, APP_DISPLAY_NAME, self.db.last_error)

    def _reveal_db(self) -> None:
        if self.db.cur_file and os.path.exists(self.db.cur_file):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(os.path.abspath(self.db.cur_file))))

    # ================================================================== project files
    def open_project_dialog(self) -> None:
        start = os.path.dirname(self.project_path) if self.project_path else Settings.get("db", "defaultlocation")
        path, _ = QFileDialog.getOpenFileName(self, "Choose a project file to open", start,
                                              f"{sqbpro.PROJECT_FILTER};;{DB_FILTER_ALL}")
        if path:
            self.open_project(path)

    def open_project(self, path: str, read_only: bool = False) -> bool:
        """Open the database a project names and restore its tabs and layout (port of ``loadProject``)."""
        try:
            project = sqbpro.load_project(path)
        except sqbpro.ProjectError as e:
            QMessageBox.warning(self, APP_DISPLAY_NAME, str(e))
            return False
        db_path = sqbpro.resolve_db_path(project.db_path, path)
        if not db_path or sqbpro.is_project_file(db_path):
            QMessageBox.warning(self, APP_DISPLAY_NAME, "The project file doesn't name a database file.")
            return False
        # Like upstream, the current database and every tab are closed first
        if self.db.is_open() and not self.close_database():
            return False
        for i in reversed(range(self.tabs.count())):
            self.close_tab(i)
        if self.tabs.count():  # the user cancelled closing an editor
            return False
        if not self.open_database(db_path, read_only or project.read_only):
            return False
        self._apply_project(project, path)
        self.project_path = os.path.abspath(path)
        self._add_recent(path)
        self.notify(f"Opened project {os.path.basename(path)}")
        return True

    def _apply_project(self, project: sqbpro.Project, path: str) -> None:
        for name, value in project.pragmas.items():
            if str(self.db.get_pragma(name)) != value:
                self.db.set_pragma(name, value)
        failed = [schema for schema, file in project.attached
                  if schema not in self.db.schemata and not self.db.attach(sqbpro.resolve_db_path(file, path), schema)]
        if failed:
            QMessageBox.warning(self, APP_DISPLAY_NAME, f"Could not attach {', '.join(failed)}:\n{self.db.last_error}")
        if project.geometry:
            self.restoreGeometry(QByteArray(project.geometry))
        if project.window_state:
            self.restoreState(QByteArray(project.window_state))

        docs = project.documents or ([sqbpro.DocumentRef("table", i) for i in range(len(project.tables))]
                                     + [sqbpro.DocumentRef("sql", i) for i in range(len(project.sql_tabs))])
        if not docs:
            return  # keep the Database Info page that opening the database showed
        for i in reversed(range(self.tabs.count())):
            self._remove_tab(i)
        opened: list[Optional[QWidget]] = []
        for d in docs:
            w: Optional[QWidget] = None
            if d.kind == "table" and 0 <= d.index < len(project.tables):
                ident = project.tables[d.index]
                w = self.open_table(ident)
                if w is not None and ident in project.table_settings:
                    w.apply_browse_settings(project.table_settings[ident])
            elif d.kind == "sql" and 0 <= d.index < len(project.sql_tabs):
                buf = project.sql_tabs[d.index]
                w = self.open_sql_tab(path=buf.filename) if buf.filename else self.open_sql_tab(buf.text)
                if not buf.filename and buf.name:
                    w.name = buf.name
                    self._retitle(w)
            elif d.kind == "info":
                self.open_info_tab()
                w = self.tabs.currentWidget()
            opened.append(w)
        current = opened[project.current_document] if 0 <= project.current_document < len(opened) else None
        if current is None and not project.documents:  # upstream file: show its current SQL editor or first tab
            sql_tabs = self._tabs_of(SqlTab)
            current = sql_tabs[project.current_sql_tab] if 0 <= project.current_sql_tab < len(sql_tabs) else None
        self.tabs.setCurrentWidget(current or self.tabs.widget(0))

    def _collect_project(self) -> sqbpro.Project:
        p = sqbpro.Project(db_path=self.db.cur_file if self.db.cur_file == ":memory:" else os.path.abspath(self.db.cur_file),
                           read_only=self.db.read_only)
        for name in sqbpro.PROJECT_PRAGMAS:
            value = self.db.get_pragma(name)
            if value is not None:
                p.pragmas[name] = str(value)
        p.attached = self.db.attached_databases()
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, TableTab):
                p.documents.append(sqbpro.DocumentRef("table", len(p.tables)))
                p.tables.append(w.ident)
                p.table_settings[w.ident] = w.browse_settings()
            elif isinstance(w, SqlTab):
                if w is self.tabs.currentWidget():
                    p.current_sql_tab = len(p.sql_tabs)
                p.documents.append(sqbpro.DocumentRef("sql", len(p.sql_tabs)))
                name = os.path.basename(w.file_path) if w.file_path else w.name
                p.sql_tabs.append(sqbpro.SqlBuffer(name, w.editor.toPlainText(), w.file_path or ""))
            elif isinstance(w, DbInfoTab):
                p.documents.append(sqbpro.DocumentRef("info"))
        p.current_document = self.tabs.currentIndex()
        p.geometry = self.saveGeometry().data()
        p.window_state = self.saveState().data()
        return p

    def save_project_as(self) -> None:
        if not self.db.is_open():
            return
        start = self.project_path or ("" if self.db.cur_file == ":memory:" else os.path.splitext(self.db.cur_file)[0])
        path, _ = QFileDialog.getSaveFileName(self, "Choose a filename to save under", start,
                                              f"{sqbpro.PROJECT_FILTER};;{DB_FILTER_ALL}")
        if path:
            self.save_project(sqbpro.project_file_name(path))

    def save_project(self, path: str) -> bool:
        """Write the current database, tabs and layout to ``path`` (port of ``saveProject``)."""
        try:
            sqbpro.save_project(path, self._collect_project())
        except OSError as e:
            QMessageBox.warning(self, APP_DISPLAY_NAME, f"Could not write the project file.\nReason: {e}")
            return False
        self.project_path = os.path.abspath(path)
        self._add_recent(path)
        self.notify(f"Project saved to {os.path.basename(path)}")
        return True

    # ================================================================== tabs
    def _tabs_of(self, cls) -> list:
        return [self.tabs.widget(i) for i in range(self.tabs.count()) if isinstance(self.tabs.widget(i), cls)]

    def _add_tab(self, widget: QWidget, title: str, icon_name: str) -> int:
        i = self.tabs.addTab(widget, icon(icon_name, "accent" if icon_name in ("table", "view") else None), title)
        self.tabs.setTabToolTip(i, title)
        self.tabs.setCurrentIndex(i)
        return i

    def _update_tab_icon(self, i: int) -> None:
        w = self.tabs.widget(i)
        name = "terminal" if isinstance(w, SqlTab) else ("info" if isinstance(w, DbInfoTab) else "file-code"
                                                        if isinstance(w, SchemaTab) else
                                                        ("view" if getattr(w, "table", None) and w.table and w.table.is_view() else "table"))
        self.tabs.setTabIcon(i, icon(name, "accent" if name in ("table", "view") else None))

    def open_table(self, ident: sqlb.ObjectIdentifier, view: int = 0) -> Optional[TableTab]:
        if not self.db.is_open() or ident is None or self.db.get_table_by_name(ident) is None:
            return None
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, TableTab) and w.ident == ident:
                self.tabs.setCurrentIndex(i)
                w.show_view(view)
                return w
        tab = TableTab(self.db, ident)
        tab.cellSelected.connect(self._on_cell_selected)
        tab.foreignKeyJump.connect(self._follow_foreign_key)
        tab.modifyRequested.connect(self.edit_table)
        tab.exportRequested.connect(lambda ident_: self.export_data("csv", ident_))
        tab.exportViewRequested.connect(lambda model: self.export_data("csv", model=model))
        tab.insertDialogRequested.connect(self.add_record_dialog)
        tab.statusMessage.connect(self.notify)
        tab.fullScreenRequested.connect(self._set_full_screen)
        t = self.db.get_table_by_name(ident)
        self._add_tab(tab, tab.title_text(), "view" if t.is_view() else "table")
        tab.show_view(view)
        return tab

    def open_sql_tab(self, sql: str = "", path: str = "") -> SqlTab:
        tab = SqlTab(self.db)
        tab.cellSelected.connect(self._on_cell_selected)
        tab.structureChanged.connect(self.db.update_schema)
        tab.statusMessage.connect(self.notify)
        tab.titleChanged.connect(lambda t=tab: self._retitle(t))
        tab.exportResultsRequested.connect(lambda model: self.export_data("csv", model=model))
        tab.runningChanged.connect(lambda running: self.act_stop.setEnabled(running))
        tab.fullScreenRequested.connect(self._set_full_screen)
        tab.set_schema(self._schema_for_completion())
        if path:
            tab.open_file(path)
        elif sql:
            tab.set_sql(sql)
        self._add_tab(tab, tab.title_text(), "terminal")
        tab.editor.setFocus()
        return tab

    def open_sql_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open SQL file", "", "SQL files (*.sql);;Text files (*.txt);;All files (*)")
        if path:
            self.open_sql_tab(path=path)

    def open_info_tab(self) -> None:
        if not self.db.is_open():
            return
        for i in range(self.tabs.count()):
            if isinstance(self.tabs.widget(i), DbInfoTab):
                self.tabs.widget(i).reload()
                self.tabs.setCurrentIndex(i)
                return
        tab = DbInfoTab(self.db)
        tab.statusMessage.connect(self.notify)
        tab.reload()
        self._add_tab(tab, "Database Info", "info")

    def open_schema_tab(self, ident: Optional[sqlb.ObjectIdentifier] = None) -> Optional[SchemaTab]:
        """Show CREATE statements and columns for the whole database, or for one table/view."""
        if not self.db.is_open():
            return None
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SchemaTab):
                w.set_scope(ident)
                self.tabs.setCurrentIndex(i)
                self._retitle(w)
                return w
        tab = SchemaTab(self.db)
        tab.statusMessage.connect(self.notify)
        tab.openSqlRequested.connect(lambda sql: self.open_sql_tab(sql))
        tab.openTableRequested.connect(self.open_table)
        tab.titleChanged.connect(lambda t=tab: self._retitle(t))
        tab.set_scope(ident)
        self._add_tab(tab, tab.title_text(), "file-code")
        return tab

    def _retitle(self, w: QWidget) -> None:
        i = self.tabs.indexOf(w)
        if i >= 0:
            self.tabs.setTabText(i, w.title_text())

    def _remove_tab(self, i: int) -> None:
        w = self.tabs.widget(i)
        if self.inspector.model is not None and getattr(w, "model", None) is self.inspector.model:
            self.inspector.clear()
        self.tabs.removeTab(i)
        if hasattr(w, "shutdown"):
            w.shutdown()
        w.deleteLater()

    def close_tab(self, i: int) -> None:
        w = self.tabs.widget(i)
        if isinstance(w, SqlTab):
            if w.is_running():
                if QMessageBox.question(self, APP_DISPLAY_NAME, "This tab is still executing. Stop and close it?") != QMessageBox.Yes:
                    return
                w.stop()
            if w.is_modified():
                r = QMessageBox.question(self, APP_DISPLAY_NAME, f"Save changes to '{w.title_text()}'?",
                                         QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
                if r == QMessageBox.Cancel or (r == QMessageBox.Save and not w.save_file()):
                    return
        self._remove_tab(i)

    def close_current_tab(self) -> None:
        if self.tabs.count():
            self.close_tab(self.tabs.currentIndex())

    def _tab_context_menu(self, pos) -> None:
        i = self.tabs.tabBar().tabAt(pos)
        if i < 0:
            return
        menu = QMenu(self)
        menu.addAction("Close", lambda: self.close_tab(i))
        menu.addAction("Close Others", lambda: self._close_others(i))
        menu.addAction("Close All", lambda: [self.close_tab(j) for j in reversed(range(self.tabs.count()))])
        w = self.tabs.widget(i)
        if isinstance(w, SqlTab):
            menu.addSeparator()
            menu.addAction("Rename…", lambda: self._rename_sql_tab(w))
        menu.exec(self.tabs.tabBar().mapToGlobal(pos))

    def _close_others(self, keep: int) -> None:
        keep_w = self.tabs.widget(keep)
        for j in reversed(range(self.tabs.count())):
            if self.tabs.widget(j) is not keep_w:
                self.close_tab(j)

    def _rename_sql_tab(self, w: SqlTab) -> None:
        name, ok = QInputDialog.getText(self, "Rename tab", "Name:", text=w.name)
        if ok and name.strip():
            w.name = name.strip()
            self._retitle(w)

    # ------------------------------------------------------------------ full screen grid
    def _toggle_current_full_screen(self) -> None:
        w = self.tabs.currentWidget()
        if hasattr(w, "set_expanded"):
            w.set_expanded(not w.is_expanded())

    def _set_full_screen(self, on: bool) -> None:
        """Hide everything except the current tab's grid and fill the screen; restore on exit."""
        if on == getattr(self, "_full_screen", False):
            return
        self._full_screen = on
        self.act_exit_full_screen.setEnabled(on)
        if on:
            self._fs_state = {
                "docks": [d for d in (self.dock_explorer, self.dock_inspector, self.dock_log) if d.isVisible()],
                "maximized": self.isMaximized(),
            }
            for d in self._fs_state["docks"]:
                d.hide()
        else:
            for i in range(self.tabs.count()):
                w = self.tabs.widget(i)
                if hasattr(w, "is_expanded") and w.is_expanded():
                    w.set_expanded(False)
        for widget in (self.toolbar, self.menuBar(), self.statusBar(), self.tabs.tabBar(),
                       self.tabs.cornerWidget(Qt.TopRightCorner)):
            widget.setVisible(not on)
        if on:
            self.showFullScreen()
            self.notify("Full screen — press Esc to exit")
            return
        for d in self._fs_state["docks"]:
            d.show()
        if self._fs_state["maximized"]:
            self.showMaximized()
        else:
            self.showNormal()

    def _on_tab_changed(self, i: int) -> None:
        w = self.tabs.widget(i)
        model = getattr(w, "model", None) if isinstance(w, (TableTab, SqlTab)) else None
        self.plot.set_model(model, self.tabs.tabText(i) if model is not None else "")
        if isinstance(w, DbInfoTab):
            w.reload()

    def _sql_tab_call(self, method: str) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, SqlTab):
            getattr(w, method)()

    def _execute_or_refresh(self) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, SqlTab):
            w.execute_all()
        elif isinstance(w, TableTab):
            w.reload()
        elif isinstance(w, (DbInfoTab, SchemaTab)):
            w.reload()

    # ================================================================== schema / cells
    def _schema_for_completion(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for schema, objmap in self.db.schemata.items():
            for name, t in objmap.tables.items():
                out[name] = t.field_names()
        return out

    def _refresh_sql_schema(self) -> None:
        schema = self._schema_for_completion()
        for tab in self._tabs_of(SqlTab):
            tab.set_schema(schema)

    def _reload_table_tabs(self) -> None:
        for i in reversed(range(self.tabs.count())):
            w = self.tabs.widget(i)
            if isinstance(w, TableTab):
                if self.db.get_table_by_name(w.ident) is None:
                    self._remove_tab(i)
                else:
                    w.reload()
                    self.tabs.setTabText(i, w.title_text())

    def _on_structure_updated(self) -> None:
        if not self.db.is_open():
            return
        # Coalesce bursts of schema updates (e.g. ALTER TABLE emulation)
        if not hasattr(self, "_schema_timer"):
            self._schema_timer = QTimer(self)
            self._schema_timer.setSingleShot(True)
            self._schema_timer.timeout.connect(self._apply_structure_update)
        self._schema_timer.start(50)

    def _apply_structure_update(self) -> None:
        self._refresh_sql_schema()
        for i in reversed(range(self.tabs.count())):
            w = self.tabs.widget(i)
            if isinstance(w, TableTab) and self.db.get_table_by_name(w.ident) is None:
                self._remove_tab(i)
            elif isinstance(w, TableTab):
                w.structure.populate(self.db, w.ident)
            elif isinstance(w, SchemaTab):
                w.reload()
                self._retitle(w)

    def _on_cell_selected(self, model, index: QModelIndex) -> None:
        if index.isValid():
            self.inspector.set_cell(model, index)

    def _follow_foreign_key(self, table: str, column: str, value) -> None:
        src = self.tabs.currentWidget()
        schema = src.ident.schema if isinstance(src, TableTab) else "main"
        ident = sqlb.ObjectIdentifier(schema, table)
        t = self.db.get_table_by_name(ident)
        if t is None:
            return
        if not column:
            pk = t.primary_key_names()
            column = pk[0] if pk else "_rowid_"
        tab = self.open_table(ident)
        if tab is not None and column in t.field_names():
            tab.jump_to_value(column, value)

    # ================================================================== structure editing
    def edit_table(self, ident: Optional[sqlb.ObjectIdentifier]) -> None:
        from .dialogs.edit_table import EditTableDialog
        if not self.db.is_open() or self.db.read_only:
            return
        if ident is None:
            dlg = EditTableDialog(self.db, None, self)
            if dlg.exec():
                self.notify(f"Table '{dlg.result_name}' created")
                self.open_table(sqlb.ObjectIdentifier(dlg.result_schema, dlg.result_name))
            return
        # Modifying a table with foreign keys on needs committing first (same as upstream)
        fk = str(self.db.get_pragma("foreign_keys"))
        if fk == "1":
            if self.db.dirty and QMessageBox.question(
                    self, APP_DISPLAY_NAME,
                    "Editing the table requires to save all pending changes now.\nAre you sure you want to save the database?",
                    QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Save) != QMessageBox.Save:
                return
            self.db.release_all_savepoints()
            self.db.set_pragma("foreign_keys", "0")
        dlg = EditTableDialog(self.db, ident, self)
        ok = bool(dlg.exec())
        if fk == "1":
            bad = self.db.query_single_value(f"PRAGMA {sqlb.escape_identifier(ident.schema)}.foreign_key_check")
            if bad is not None:
                QMessageBox.warning(self, APP_DISPLAY_NAME,
                                    "Error checking foreign keys after table modification. The changes will be reverted." if ok else
                                    "This table did not pass a foreign-key check.\nYou should run 'Tools › Foreign-Key Check' "
                                    "and fix the reported issues.")
                self.db.revert_all()
                self.db.update_schema()
            else:
                self.db.release_all_savepoints()
            self.db.set_pragma("foreign_keys", fk)
        if ok:
            new_ident = sqlb.ObjectIdentifier(dlg.result_schema, dlg.result_name)
            for i in range(self.tabs.count()):
                w = self.tabs.widget(i)
                if isinstance(w, TableTab) and w.ident == ident:
                    w.ident = new_ident
                    w.reload()
                    self.tabs.setTabText(i, w.title_text())
            self._update_state()
            self.notify(f"Table '{dlg.result_name}' modified")

    def modify_current_table(self) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, TableTab):
            self.edit_table(w.ident)
            return
        kind, ident = self.explorer.current_object()
        if kind == "table" and ident is not None:
            self.edit_table(ident)
        else:
            self.notify("Select a table in the explorer first")

    def create_index(self, table_ident: Optional[sqlb.ObjectIdentifier]) -> None:
        from .dialogs.edit_index import EditIndexDialog
        if not self.db.is_open() or self.db.read_only:
            return
        dlg = EditIndexDialog(self.db, table_ident, self)
        if dlg.exec():
            self.notify("Index created")

    def drop_object(self, kind: str, ident: sqlb.ObjectIdentifier) -> None:
        if ident is None:
            return
        word = {"table": "TABLE", "view": "VIEW", "index": "INDEX", "trigger": "TRIGGER"}[kind]
        if QMessageBox.question(self, APP_DISPLAY_NAME,
                                f"Are you sure you want to delete the {kind} '{ident.to_display_string()}'?\n"
                                "All data associated with it will be lost (until you revert changes).",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.db.set_undo_savepoint()
        if not self.db.execute_sql(f"DROP {word} {ident.to_string()};"):
            QMessageBox.warning(self, APP_DISPLAY_NAME, f"Error: could not delete the {kind}.\n{self.db.last_error}")
            self.db.revert_to_undo_savepoint()
            return
        self._update_state()
        self.notify(f"Deleted {kind} '{ident.name}'")

    def delete_current_object(self) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, TableTab):
            t = w.table
            self.drop_object("view" if t and t.is_view() else "table", w.ident)
            return
        kind, ident = self.explorer.current_object()
        if kind in ("table", "view", "index", "trigger"):
            self.drop_object(kind, ident)

    def add_record_dialog(self, ident: sqlb.ObjectIdentifier) -> None:
        from .dialogs.add_record import AddRecordDialog
        dlg = AddRecordDialog(self.db, ident, self)
        if dlg.exec():
            for w in self._tabs_of(TableTab):
                if w.ident == ident:
                    w.reload()
            self._update_state()
            self.notify("Record inserted")

    # ================================================================== import / export
    def import_sql(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import SQL file", "", "SQL files (*.sql);;Text files (*.txt);;All files (*)")
        if not path:
            return
        if not self.db.is_open():
            r = QMessageBox.question(self, "Import SQL", "Create a new database file to import into?\n"
                                     "Choose No to import into an in-memory database.",
                                     QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Yes:
                target, _ = QFileDialog.getSaveFileName(self, "New database for import", os.path.splitext(path)[0] + ".db",
                                                        f"{Settings.get('General', 'DBFileExtensions')};;{DB_FILTER_ALL}")
                if not target:
                    return
                if os.path.exists(target):
                    os.remove(target)
                if not self.db.create(target):
                    QMessageBox.warning(self, "Import SQL", self.db.last_error)
                    return
                self._add_recent(target)
            elif not self.db.open(":memory:"):
                return
            self._after_open()
        elif self.db.schemata.get("main") and any(not n.startswith("sqlite_") for n in self.db.schemata["main"].tables):
            if QMessageBox.question(self, "Import SQL", "Do you want to import the SQL into the current database?\n"
                                    "Existing objects with the same names may cause errors.") != QMessageBox.Yes:
                return
        try:
            with open(path, encoding="utf-8-sig") as fh:
                sql = fh.read()
        except (OSError, UnicodeDecodeError) as e:
            QMessageBox.warning(self, "Import SQL", f"Couldn't read file:\n{e}")
            return
        progress = QProgressDialog("Importing SQL…", "Cancel", 0, 100, self)
        progress.setWindowModality(Qt.ApplicationModal)
        progress.setMinimumDuration(300)

        def cb(pct: int) -> bool:
            progress.setValue(pct)
            QApplication.processEvents()
            return not progress.wasCanceled()

        ok = self.db.execute_multi_sql(sql, dirty=True, log=False, progress=cb)
        progress.close()
        if ok:
            self.db.update_schema()
            self.notify("Import completed — remember to Write Changes")
        else:
            QMessageBox.warning(self, "Import SQL", f"Error importing data:\n{self.db.last_error}")
        self._update_state()

    def import_csv(self, files: Optional[list[str]] = None) -> None:
        from .dialogs.import_csv import ImportCsvDialog
        if not self.db.is_open():
            if QMessageBox.question(self, "Import CSV", "No database is open. Create an in-memory database for the import?") != QMessageBox.Yes:
                return
            self.new_memory_database()
        if self.db.read_only:
            return
        if not files:
            files, _ = QFileDialog.getOpenFileNames(self, "Import CSV file(s)", "",
                                                    "Text files (*.csv *.tsv *.txt);;All files (*)")
        if not files:
            return
        dlg = ImportCsvDialog(self.db, files, self)
        if dlg.exec():
            self._update_state()
            self.notify("CSV import finished — remember to Write Changes")
            if dlg.imported_tables:
                self.open_table(sqlb.ObjectIdentifier("main", dlg.imported_tables[-1]))

    def export_data(self, fmt: str, ident: Optional[sqlb.ObjectIdentifier] = None, model=None) -> None:
        from .dialogs.export_data import ExportDataDialog
        if not self.db.is_open():
            return
        if ident is None and model is None:
            w = self.tabs.currentWidget()
            if isinstance(w, TableTab):
                ident = w.ident
        ExportDataDialog(self.db, fmt, ident, model, self).exec()

    def export_sql(self) -> None:
        from .dialogs.export_sql import ExportSqlDialog
        if self.db.is_open():
            ExportSqlDialog(self.db, self).exec()

    # ================================================================== tools
    def compact_database(self) -> None:
        if not self.db.is_open():
            return
        if self.db.dirty and QMessageBox.question(self, APP_DISPLAY_NAME,
                                                  "Compacting commits all pending changes first. Continue?") != QMessageBox.Yes:
            return
        self.db.release_all_savepoints()
        before = os.path.getsize(self.db.cur_file) if os.path.exists(self.db.cur_file) else 0
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok = self.db.execute_sql("VACUUM;", False)
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            QMessageBox.warning(self, APP_DISPLAY_NAME, self.db.last_error)
            return
        after = os.path.getsize(self.db.cur_file) if os.path.exists(self.db.cur_file) else 0
        from ..core.data import human_readable_size
        self.notify(f"Database compacted ({human_readable_size(before)} → {human_readable_size(after)})")

    def run_check(self, pragma: str) -> None:
        """Run a check pragma in a new SQL tab (same as upstream, which uses the Execute SQL tab)."""
        if not self.db.is_open():
            return
        tab = self.open_sql_tab(pragma)
        tab.execute_all()

    def load_extension(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load Extension", "", "Extensions (*.dll *.so *.dylib);;All files (*)")
        if not path:
            return
        if self.db.load_extension(path):
            self.notify("Extension successfully loaded")
        else:
            QMessageBox.warning(self, APP_DISPLAY_NAME, f"Error loading extension: {self.db.last_error}")

    # ================================================================== appearance / misc
    def set_theme(self, mode: str) -> None:
        Settings.set("General", "theme", mode)
        theme.apply()

    def toggle_theme(self) -> None:
        self.set_theme("light" if theme.tokens.name == "dark" else "dark")
        for a in self.theme_group.actions():
            a.setChecked(a.text().lower() == theme.tokens.name)

    def show_preferences(self) -> None:
        from .dialogs.preferences import PreferencesDialog
        dlg = PreferencesDialog(self)
        if dlg.exec():
            theme.apply()
            for w in self._tabs_of(TableTab) + self._tabs_of(SqlTab):
                w.model.reload_settings()
                if isinstance(w, SqlTab):
                    w.editor.apply_settings()
                else:
                    w.reload()
            sqlb.set_identifier_quoting(sqlb.EscapeQuoting(int(Settings.get("editor", "identifier_quotes"))))
            self.notify("Preferences saved")

    def show_about(self) -> None:
        QMessageBox.about(
            self, f"About {APP_DISPLAY_NAME}",
            f"<h3>{APP_DISPLAY_NAME}</h3><p>Version {__version__}</p>"
            "<p>A Python/Qt for Python port of <a href='https://sqlitebrowser.org'>DB Browser for SQLite</a> "
            "with a redesigned interface. The database engine logic follows the original C++ implementation.</p>"
            f"<p>SQLite {DBBrowserDB.sqlite_version()} · Qt {__import__('PySide6').QtCore.qVersion()}</p>")

    def show_shortcuts(self) -> None:
        from .dialogs.misc import ShortcutsDialog
        ShortcutsDialog(self).exec()

    def _palette_entries(self) -> list[PaletteEntry]:
        entries: list[PaletteEntry] = []
        for a in self.findChildren(QAction):
            text = a.text().replace("&&", "\0").replace("&", "").replace("\0", "&").rstrip("…").strip()
            if not text or not a.isEnabled() or a.menu() is not None or a.isSeparator():
                continue
            if a.parent() is not self:
                continue
            entries.append(PaletteEntry(text, "Command", a.icon(), a.trigger, a.shortcut().toString(QKeySequence.NativeText)))
        seen = set()
        uniq = []
        for e in entries:
            if e.title not in seen:
                seen.add(e.title)
                uniq.append(e)
        entries = uniq
        if self.db.is_open():
            for schema, objmap in self.db.schemata.items():
                for name, t in objmap.tables.items():
                    ident = sqlb.ObjectIdentifier(schema, name)
                    kind = "View" if t.is_view() else "Table"
                    entries.append(PaletteEntry(ident.to_display_string(), f"Open {kind.lower()}",
                                                icon("view" if t.is_view() else "table", "accent"),
                                                lambda i=ident: self.open_table(i)))
                    for f in t.fields:
                        entries.append(PaletteEntry(f"{name}.{f.name}", f"Column · {f.type or 'no type'}", icon("column"),
                                                    lambda i=ident: self.open_table(i)))
        for path in self._recent_files():
            entries.append(PaletteEntry(os.path.basename(path), f"Recent · {path}", icon("history"),
                                        lambda p=path: self.open_database(p)))
        return entries

    # ================================================================== events
    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        csvs = [p for p in paths if p.lower().endswith((".csv", ".tsv", ".txt"))]
        sqls = [p for p in paths if p.lower().endswith(".sql")]
        if csvs:
            self.import_csv(csvs)
        elif sqls:
            if self.db.is_open():
                for p in sqls:
                    self.open_sql_tab(path=p)
            else:
                self.import_sql()
        else:
            self.open_database(paths[0])

    def closeEvent(self, e) -> None:
        for tab in self._tabs_of(SqlTab):
            if tab.is_modified():
                self.tabs.setCurrentWidget(tab)
                r = QMessageBox.question(self, APP_DISPLAY_NAME, f"Save changes to '{tab.title_text()}'?",
                                         QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
                if r == QMessageBox.Cancel or (r == QMessageBox.Save and not tab.save_file()):
                    e.ignore()
                    return
        if not self._confirm_close_db():
            e.ignore()
            return
        Settings.set("MainWindow", "geometry", self.saveGeometry())
        Settings.set("MainWindow", "windowState", self.saveState())
        for i in reversed(range(self.tabs.count())):
            self._remove_tab(i)
        self.db.close(force=True)
        e.accept()
