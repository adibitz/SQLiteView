"""Background execution of SQL editor contents. Port of upstream ``src/RunSql.{h,cpp}``.

Statements are executed one at a time in a worker thread. Savepoint handling is the same as
upstream: a restore point is set before the first modifying statement (so "Revert Changes"
undoes the execution), and it's released again if nothing was changed. PRAGMA assignments and
VACUUM need all pending changes to be committed first; the UI is asked for confirmation.
Row-returning statements are reported back so the result grid can load them lazily.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal

from .db import DBBrowserDB
from .sqlsplit import iter_statements


class StatementType(Enum):
    Select = auto()
    Alter = auto()
    Drop = auto()
    Rollback = auto()
    Pragma = auto()
    Vacuum = auto()
    Insert = auto()
    Update = auto()
    Delete = auto()
    Create = auto()
    Attach = auto()
    Detach = auto()
    Analyze = auto()
    Explain = auto()
    With = auto()
    Values = auto()
    Other = auto()


_TYPES = {t.name.upper(): t for t in StatementType}
_LEADING_COMMENTS_RE = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?(?:\*/|\Z))*", re.S)
_CTE_WRITE_RE = re.compile(r"\)\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)


def strip_leading_comments(sql: str) -> str:
    return sql[_LEADING_COMMENTS_RE.match(sql).end():]


def statement_type(sql: str) -> StatementType:
    head = strip_leading_comments(sql)
    m = re.match(r"[A-Za-z]+", head)
    if not m:
        return StatementType.Other
    word = m.group(0).upper()
    if word == "REPLACE":
        return StatementType.Insert
    return _TYPES.get(word, StatementType.Other)


def returns_rows(sql: str, st: StatementType) -> bool:
    """Reader statements are deferred to the lazily loading result model (like upstream, which
    does not step statements that have result columns)."""
    if st in (StatementType.Select, StatementType.Explain, StatementType.Values):
        return True
    if st == StatementType.Pragma:
        return "=" not in sql
    if st == StatementType.With:
        return not _CTE_WRITE_RE.search(sql)
    return False


@dataclass
class StaticResult:
    """Rows returned by a modifying statement (INSERT ... RETURNING); can't be re-run lazily."""
    columns: list[str]
    rows: list[tuple]


class RunSql(QObject):
    statementErrored = Signal(str, int, int)
    statementExecuted = Signal(str, int, int)
    statementReturnsRows = Signal(str, int, int, int)  # query, from, to, ms
    statementReturnsStaticRows = Signal(object, int, int, str)  # StaticResult, from, to, message
    confirmSaveBeforePragmaOrVacuum = Signal()
    structureUpdated = Signal()
    finished = Signal()

    def __init__(self, db: DBBrowserDB, query: str, execute_from: int, execute_to: int):
        super().__init__()
        self.db = db
        self.query = query
        self.execute_from = execute_from
        self.execute_to = execute_to
        self._stop = False
        self._structure_updated = False
        self._savepoint_created = False
        self._was_dirty = db.dirty
        self._modified = False
        self._confirm_event = threading.Event()
        self._confirm_result = False
        self._conn: Optional[sqlite3.Connection] = None
        self._thread: Optional[QThread] = None

    # ------------------------------------------------------------------ thread control
    def start(self) -> None:
        self._thread = QThread()
        self.moveToThread(self._thread)
        self._thread.started.connect(self.run)
        # Direct: quit() is thread-safe and must not wait for the (possibly blocked) main loop
        self.finished.connect(self._thread.quit, Qt.DirectConnection)
        self._thread.start()

    def wait(self, ms: int = -1) -> bool:
        if self._thread is None:
            return True
        return self._thread.wait() if ms < 0 else self._thread.wait(ms)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def stop(self) -> None:
        self._stop = True
        self.db.interrupt_query()
        self._confirm_result = False
        self._confirm_event.set()

    def answer_confirmation(self, ok: bool) -> None:
        """Called by the UI (main thread) in response to ``confirmSaveBeforePragmaOrVacuum``."""
        self._confirm_result = ok
        self._confirm_event.set()

    # ------------------------------------------------------------------ execution
    def run(self) -> None:
        try:
            for st in iter_statements(self.query, self.execute_from, self.execute_to):
                if self._stop:
                    break
                if not self._execute(st.text, st.start, st.end):
                    break
        finally:
            if self._structure_updated:
                self.structureUpdated.emit()
            self.finished.emit()

    def _execute(self, sql: str, start: int, end: int) -> bool:
        stype = statement_type(sql)
        stripped = strip_leading_comments(sql).strip()
        # Upstream drops a leading "BEGIN TRANSACTION;" and trailing "COMMIT;" (we manage the transaction)
        if re.fullmatch(r"BEGIN(\s+(DEFERRED|IMMEDIATE|EXCLUSIVE))?(\s+TRANSACTION)?\s*;?", stripped, re.I) or \
                re.fullmatch(r"(COMMIT|END)(\s+TRANSACTION)?\s*;?", stripped, re.I):
            return True

        if stype in (StatementType.Alter, StatementType.Create, StatementType.Drop, StatementType.Rollback,
                     StatementType.Attach, StatementType.Detach, StatementType.Analyze):
            self._structure_updated = True

        if (stype == StatementType.Pragma and "=" in sql and "defer_foreign_keys" not in sql.lower()) \
                or stype == StatementType.Vacuum:
            if self.db.dirty:
                self._confirm_event.clear()
                self.confirmSaveBeforePragmaOrVacuum.emit()
                self._confirm_event.wait()
                if not self._confirm_result:
                    self.statementErrored.emit(self.tr("Execution aborted by user"), start, end)
                    return False
                self.db.release_all_savepoints()
                self._savepoint_created = False
        elif not self._savepoint_created and not self.db.read_only and stype not in (StatementType.Attach, StatementType.Detach):
            self.db.set_undo_savepoint()
            self.db.set_savepoint()
            self._savepoint_created = True

        error = ""
        t0 = time.perf_counter()
        with self.db.get(self.tr("executing query"), force_wait=True) as conn:
            if conn is None:
                return False
            try:
                if returns_rows(sql, stype):
                    # Validate without running the query; the result model will run it lazily
                    if stype != StatementType.Explain:
                        self.db.run(conn, "EXPLAIN " + sql).fetchall()
                    ms = int((time.perf_counter() - t0) * 1000)
                    result_kind = "rows"
                else:
                    before = conn.total_changes
                    cur = self.db.run(conn, sql)
                    rows = cur.fetchall() if cur.description else None
                    cols = [d[0] for d in cur.description] if cur.description else []
                    changes = conn.total_changes - before
                    ms = int((time.perf_counter() - t0) * 1000)
                    result_kind = "static" if cur.description else "done"
            except sqlite3.Error as e:
                error = str(e)

        if error:
            self._release_unneeded_savepoints()
            self.statementErrored.emit(error, start, end)
            return False

        if result_kind == "rows":
            self.statementReturnsRows.emit(sql.strip(), start, end, ms)
        else:
            affected = ""
            if stype in (StatementType.Insert, StatementType.Update, StatementType.Delete):
                affected = self.tr(", %1 rows affected").replace("%1", str(changes))
            if stype not in (StatementType.Attach, StatementType.Detach):
                self._modified = True
            msg = self.tr("query executed successfully. Took %1ms%2").replace("%1", str(ms)).replace("%2", affected)
            if result_kind == "static":
                self.statementReturnsStaticRows.emit(StaticResult(cols, rows or []), start, end, msg)
            else:
                self.statementExecuted.emit(msg, start, end)
        self._release_unneeded_savepoints()
        return True

    def _release_unneeded_savepoints(self) -> None:
        if not self._modified and not self._was_dirty and self._savepoint_created:
            self.db.release_savepoint()
            self.db.release_undo_savepoint()
            self._savepoint_created = False
