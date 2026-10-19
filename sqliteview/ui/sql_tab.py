"""Document tab with a SQL editor and a result grid (replaces upstream's "Execute SQL" tab)."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QSplitter,
                               QStackedWidget, QVBoxLayout, QWidget)

from ..core.db import DBBrowserDB
from ..core.runsql import RunSql, StaticResult
from ..core.sqlsplit import statement_at
from ..core.table_model import RowCount, SqliteTableModel
from .data_grid import DataGrid
from .icons import icon
from .sql_editor import EditorWithFind, SqlEditor
from .theme import mono_font
from .widgets import EmptyState, label, tool_button, vsep


class SqlTab(QWidget):
    cellSelected = Signal(object, QModelIndex)
    structureChanged = Signal()
    titleChanged = Signal()
    statusMessage = Signal(str)
    runningChanged = Signal(bool)
    exportResultsRequested = Signal(object)  # SqliteTableModel
    fullScreenRequested = Signal(bool)

    kind = "sql"
    _counter = 0

    def __init__(self, db: DBBrowserDB, parent=None):
        super().__init__(parent)
        SqlTab._counter += 1
        self.db = db
        self.file_path: Optional[str] = None
        self.name = f"SQL {SqlTab._counter}"
        self.runner: Optional[RunSql] = None
        self._last_rows: Optional[tuple[str, int, int, int]] = None
        self._last_static: Optional[tuple[StaticResult, str]] = None
        self._messages: list[tuple[str, bool]] = []
        self._exec_offset = 0
        self.model = SqliteTableModel(db, self, force_wait=True)
        self._build()
        self.model.finishedRowCount.connect(self._update_result_info)
        self.model.finishedFetch.connect(lambda *_: self._update_result_info())
        self.model.errorOccurred.connect(lambda m: self._append_message(m, True))

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QFrame()
        bar.setProperty("role", "toolbar")
        self.bar = bar
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 6, 10, 6)
        bl.setSpacing(4)
        self.btn_run = tool_button("play", "Execute all / selected SQL", text="Run", kind="primary",
                                   on_click=self.execute_all, shortcut="Ctrl+Return")
        self.btn_run_current = tool_button("play-cursor", "Execute current statement", on_click=self.execute_current,
                                           shortcut="Shift+F5")
        self.btn_run_line = tool_button("play-line", "Execute current line", on_click=self.execute_line, shortcut="Shift+F6")
        self.btn_stop = tool_button("stop", "Stop execution", on_click=self.stop)
        self.btn_stop.setEnabled(False)
        for b in (self.btn_run, self.btn_run_current, self.btn_run_line, self.btn_stop):
            bl.addWidget(b)
        bl.addWidget(vsep())
        bl.addWidget(tool_button("folder-open", "Open SQL file…", on_click=self.open_file))
        bl.addWidget(tool_button("save", "Save SQL file", on_click=self.save_file, shortcut="Ctrl+Shift+S"))
        bl.addWidget(tool_button("comment", "Toggle comment", on_click=lambda: self.editor.toggle_comment(), shortcut="Ctrl+/"))
        bl.addWidget(tool_button("find-replace", "Find / replace", on_click=lambda: self.editor_box.find_bar.open_for(self.editor),
                                 shortcut="Ctrl+F"))
        bl.addStretch(1)
        self.hint = label("Ctrl+Enter run · Shift+F5 statement · Ctrl+Space complete", "faint")
        bl.addWidget(self.hint)
        lay.addWidget(bar)

        self.split = QSplitter(Qt.Vertical)
        self.split.setChildrenCollapsible(False)
        self.editor = SqlEditor()
        self.editor.setPlaceholderText("-- Write SQL here. Ctrl+Enter executes everything (or the selection).")
        self.editor_box = EditorWithFind(self.editor)
        self.split.addWidget(self.editor_box)

        results = QWidget()
        rl = QVBoxLayout(results)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rhead = QFrame()
        rhead.setProperty("role", "toolbar")
        rh = QHBoxLayout(rhead)
        rh.setContentsMargins(12, 4, 10, 4)
        self.result_title = label("Results", "section")
        self.result_info = label("", "muted")
        rh.addWidget(self.result_title)
        rh.addWidget(self.result_info)
        rh.addStretch(1)
        self.btn_export = tool_button("download", "Export results…", on_click=lambda: self.exportResultsRequested.emit(self.model))
        self.btn_export.setEnabled(False)
        rh.addWidget(self.btn_export)
        self.btn_expand = tool_button("maximize", "Show results full screen (F11, Esc to exit)", checkable=True,
                                      on_click=lambda: self.set_expanded(self.btn_expand.isChecked()))
        rh.addWidget(self.btn_expand)
        rl.addWidget(rhead)
        self.result_stack = QStackedWidget()
        self.empty = EmptyState("terminal", "No results yet", "Run a query to see its results here.")
        self.empty.finish()
        self.result_stack.addWidget(self.empty)
        self.grid = DataGrid()
        self.grid.set_table_model(self.model)
        self.grid.filter_header.set_filters_enabled(False)
        self.grid.cellActivated.connect(lambda ix: self.cellSelected.emit(self.model, ix))
        self.result_stack.addWidget(self.grid)
        rl.addWidget(self.result_stack, 1)
        self.messages = QPlainTextEdit()
        self.messages.setReadOnly(True)
        self.messages.setProperty("role", "editor")
        self.messages.setFont(mono_font())
        self.messages.setMaximumBlockCount(2000)
        msplit = QSplitter(Qt.Vertical)
        msplit.addWidget(results)
        msplit.addWidget(self.messages)
        msplit.setStretchFactor(0, 4)
        msplit.setStretchFactor(1, 1)
        msplit.setSizes([300, 90])
        self.split.addWidget(msplit)
        self.split.setStretchFactor(0, 2)
        self.split.setStretchFactor(1, 3)
        self.split.setSizes([260, 400])
        lay.addWidget(self.split, 1)
        self.editor.document().modificationChanged.connect(lambda *_: self.titleChanged.emit())

    # ------------------------------------------------------------------ titles / files
    def title_text(self) -> str:
        base = os.path.basename(self.file_path) if self.file_path else self.name
        return base + (" •" if self.editor.document().isModified() and self.file_path else "")

    def set_sql(self, sql: str) -> None:
        self.editor.setPlainText(sql)

    def open_file(self, path: str = "") -> bool:
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Open SQL file", "", "SQL files (*.sql);;Text files (*.txt);;All files (*)")
        if not path:
            return False
        try:
            with open(path, encoding="utf-8-sig") as fh:
                self.editor.setPlainText(fh.read())
        except (OSError, UnicodeDecodeError) as e:
            QMessageBox.warning(self, "Open SQL file", f"Couldn't read file:\n{e}")
            return False
        self.file_path = path
        self.editor.document().setModified(False)
        self.titleChanged.emit()
        return True

    def save_file(self, save_as: bool = False) -> bool:
        path = self.file_path
        if save_as or not path:
            path, _ = QFileDialog.getSaveFileName(self, "Save SQL file", self.file_path or f"{self.name}.sql",
                                                  "SQL files (*.sql);;All files (*)")
            if not path:
                return False
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(self.editor.toPlainText())
        except OSError as e:
            QMessageBox.warning(self, "Save SQL file", f"Couldn't write file:\n{e}")
            return False
        self.file_path = path
        self.editor.document().setModified(False)
        self.titleChanged.emit()
        self.statusMessage.emit(f"Saved {os.path.basename(path)}")
        return True

    def is_modified(self) -> bool:
        return self.editor.document().isModified() and bool(self.editor.toPlainText().strip())

    # ------------------------------------------------------------------ execution
    def is_running(self) -> bool:
        return self.runner is not None

    def execute_all(self) -> None:
        sel = self.editor.selection_range()
        if sel:
            self._execute(sel[0], sel[1])
        else:
            self._execute(0, len(self.editor.toPlainText()))

    def execute_current(self) -> None:
        text = self.editor.toPlainText()
        st = statement_at(text, self.editor.textCursor().position())
        if st is not None:
            self._execute(st.start, st.end)

    def execute_line(self) -> None:
        start, end = self.editor.current_line_range()
        self._execute(start, end)

    def _execute(self, start: int, end: int) -> None:
        if self.runner is not None or not self.db.is_open():
            return
        text = self.editor.toPlainText()
        if not text[start:end].strip():
            return
        self.editor.clear_marks()
        self.messages.clear()
        self._messages.clear()
        self._last_rows = None
        self._last_static = None
        self.model.reset()
        self.result_stack.setCurrentIndex(0)
        self.result_info.setText("")
        self.btn_export.setEnabled(False)
        self.runner = RunSql(self.db, text, start, end)
        self.runner.statementExecuted.connect(self._on_executed)
        self.runner.statementErrored.connect(self._on_error)
        self.runner.statementReturnsRows.connect(self._on_rows)
        self.runner.statementReturnsStaticRows.connect(self._on_static_rows)
        self.runner.confirmSaveBeforePragmaOrVacuum.connect(self._confirm_commit)
        self.runner.structureUpdated.connect(self.structureChanged)
        self.runner.finished.connect(self._on_finished)
        self._set_running(True)
        self._started_at = __import__("time").perf_counter()
        self.runner.start()

    def stop(self) -> None:
        if self.runner is not None:
            self.runner.stop()
            self._append_message("Execution stopped by user.", True)

    def _set_running(self, running: bool) -> None:
        for b in (self.btn_run, self.btn_run_current, self.btn_run_line):
            b.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.editor.setReadOnly(running)
        self.runningChanged.emit(running)

    def _append_message(self, msg: str, error: bool = False) -> None:
        self._messages.append((msg, error))
        self.messages.appendPlainText(("✖ " if error else "✔ ") + msg)

    def _on_executed(self, msg: str, start: int, end: int) -> None:
        self.editor.set_executing_range(start, end)
        snippet = " ".join(self.editor.toPlainText()[start:end].split())[:80]
        self._append_message(f"{msg}\n   {snippet}")

    def _on_error(self, msg: str, start: int, end: int) -> None:
        self.editor.set_error_range(start, end)
        line = self.editor.document().findBlock(start).blockNumber() + 1
        self._append_message(f"Error on line {line}: {msg}", True)

    def _on_rows(self, query: str, start: int, end: int, ms: int) -> None:
        self.editor.set_executing_range(start, end)
        self._last_rows = (query, start, end, ms)
        self._last_static = None

    def _on_static_rows(self, result: StaticResult, start: int, end: int, msg: str) -> None:
        self.editor.set_executing_range(start, end)
        self._last_static = (result, msg)
        self._last_rows = None
        self._append_message(msg)

    def _confirm_commit(self) -> None:
        ok = QMessageBox.question(
            self, "Pending changes",
            "Setting PRAGMA values or vacuuming will commit your current transaction.\nAre you sure?") == QMessageBox.Yes
        if self.runner is not None:
            self.runner.answer_confirmation(ok)

    def _on_finished(self) -> None:
        runner = self.runner
        self.runner = None
        if runner is not None:
            runner.wait()
        self._set_running(False)
        total_ms = int((__import__("time").perf_counter() - self._started_at) * 1000)
        if self._last_rows is not None:
            query, start, end, ms = self._last_rows
            self.result_stack.setCurrentIndex(1)
            self.model.set_sql_query(query)
            if self.model.last_error:
                self._append_message(self.model.last_error, True)
            self.btn_export.setEnabled(True)
            if not any(err for _, err in self._messages):
                self._append_message(f"Query returned rows (prepared in {ms} ms)")
        elif self._last_static is not None:
            result, _ = self._last_static
            self.result_stack.setCurrentIndex(1)
            self.model.set_static_rows(result.columns, result.rows)
            self.btn_export.setEnabled(True)
        self.statusMessage.emit(f"Execution finished in {total_ms} ms")
        QTimer.singleShot(0, self._update_result_info)

    def _update_result_info(self) -> None:
        m = self.model
        if not m.headers():
            return
        n = m.real_row_count() if m.row_count_available() == RowCount.Complete else m.rowCount()
        more = "" if m.row_count_available() == RowCount.Complete else "+"
        self.result_info.setText(f"{n:,}{more} row{'s' if n != 1 else ''} · {m.columnCount()} column{'s' if m.columnCount() != 1 else ''}")
        if m.rowCount() and self.grid.columnWidth(1) == self.grid.horizontalHeader().defaultSectionSize():
            self.grid.auto_size_columns()

    def is_expanded(self) -> bool:
        return self.btn_expand.isChecked()

    def set_expanded(self, on: bool) -> None:
        """Give the result grid the whole tab (and ask the window to go full screen)."""
        self.btn_expand.setChecked(on)
        self.btn_expand.setIcon(icon("minimize" if on else "maximize"))
        self.btn_expand.setToolTip("Exit full screen (Esc)" if on else "Show results full screen (F11, Esc to exit)")
        self.editor_box.setVisible(not on)
        self.messages.setVisible(not on)
        self.bar.setVisible(not on)
        self.fullScreenRequested.emit(on)

    def set_schema(self, tables: dict[str, list[str]]) -> None:
        self.editor.set_schema(tables)

    def shutdown(self) -> None:
        if self.runner is not None:
            self.runner.stop()
            self.runner.wait(3000)
        self.model.shutdown()
