"""Background row fetching. Port of upstream ``src/RowLoader.{h,cpp}``.

Rows are fetched in chunks with ``LIMIT/OFFSET`` on a worker thread and stored in a shared
cache; the total row count is determined by a separate ``COUNT(*)`` query that is only run
when the first chunk came back full.
"""
from __future__ import annotations

import atexit
import re
import sqlite3
import threading
import weakref
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QThread, Signal

from .db import DBBrowserDB

_LIMIT_RE = re.compile(r"LIMIT\s+.+\s*((,|\b(OFFSET)\b)\s*.+\s*)?$", re.I)
_COMPOUND_RE = re.compile(r"\s(UNION)|(INTERSECT)|(EXCEPT)\s", re.I)
_NO_LIMIT_PREFIXES = ("PRAGMA", "EXPLAIN", "DELETE", "INSERT", "UPDATE")


@dataclass
class _Task:
    token: int
    row_begin: int
    row_end: int
    cancel: bool = False


class RowCache:
    """Sparse row storage (port of ``RowCache``)."""

    def __init__(self) -> None:
        self.rows: dict[int, list] = {}

    def clear(self) -> None:
        self.rows.clear()

    def __contains__(self, row: int) -> bool:
        return row in self.rows

    def get(self, row: int) -> Optional[list]:
        return self.rows.get(row)

    def set(self, row: int, data: list) -> None:
        self.rows[row] = data

    def insert(self, row: int, data: list) -> None:
        """Insert shifting subsequent rows down by one."""
        if any(r >= row for r in self.rows):
            self.rows = {(r + 1 if r >= row else r): v for r, v in self.rows.items()}
        self.rows[row] = data

    def erase(self, row: int) -> None:
        self.rows.pop(row, None)
        if any(r > row for r in self.rows):
            self.rows = {(r - 1 if r > row else r): v for r, v in self.rows.items()}

    def smallest_non_available_range(self, begin: int, end: int) -> tuple[int, int]:
        while begin < end and begin in self.rows:
            begin += 1
        while end > begin and (end - 1) in self.rows:
            end -= 1
        return begin, end


_live_loaders: "weakref.WeakSet[RowLoader]" = weakref.WeakSet()


@atexit.register
def _stop_all_loaders() -> None:
    # A QThread destroyed while running aborts the process; make sure none survive shutdown
    for loader in list(_live_loaders):
        try:
            loader.stop()
            loader.wait(2000)
        except RuntimeError:
            pass


class RowLoader(QThread):
    fetched = Signal(int, int, int)  # token, row_begin, row_end
    rowCountComplete = Signal(int, int)  # token, count
    error = Signal(int, str)

    def __init__(self, db: DBBrowserDB, cache: RowCache, cache_lock: threading.Lock,
                 force_wait: bool = False, parent=None):
        super().__init__(parent)
        self.db = db
        self.cache = cache
        self.cache_lock = cache_lock
        self.force_wait = force_wait
        self.query = ""
        self.count_query = ""
        self._cv = threading.Condition()
        self._next: Optional[_Task] = None
        self._current: Optional[_Task] = None
        self._stop = False
        self._first_chunk_loaded = False
        self._count_thread: Optional[threading.Thread] = None
        self._in_db = False  # True while this worker holds the connection
        _live_loaders.add(self)

    # ------------------------------------------------------------------ API (main thread)
    def set_query(self, query: str, count_query: str = "") -> None:
        with self._cv:
            self.cancel()
            self.query = query
            # For a custom query, just COUNT over it (same as upstream)
            self.count_query = count_query or build_count_query_for(query)
            self._first_chunk_loaded = False

    def _cancel_current(self) -> None:
        if self._current is not None:
            self._current.cancel = True
            if self._in_db:
                # Abort a long running step right away (only our own statement can be running)
                self.db.interrupt_query()

    def trigger_fetch(self, token: int, row_begin: int, row_end: int) -> None:
        with self._cv:
            self._cancel_current()
            self._next = _Task(token, row_begin, row_end)
            self._cv.notify_all()

    def cancel(self) -> None:
        with self._cv:
            self._cancel_current()
            self._next = None

    def reading_data(self) -> bool:
        with self._cv:
            return self._current is not None or self._next is not None

    def wait_until_idle(self) -> None:
        with self._cv:
            while self._current is not None or self._next is not None:
                self._cv.wait(0.05)

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            if self._current is not None:
                self._current.cancel = True
            self._cv.notify_all()

    # ------------------------------------------------------------------ worker
    def run(self) -> None:
        while True:
            with self._cv:
                self._current = None
                self._cv.notify_all()
                while not self._stop and self._next is None:
                    self._cv.wait()
                if self._stop:
                    return
                self._current, self._next = self._next, None
                task = self._current
                query = self.query
            self._process(task, query)

    def _limit_query(self, task: _Task, query: str) -> str:
        if query.lstrip()[:7].upper().startswith(_NO_LIMIT_PREFIXES):
            return query
        q = query.rstrip().rstrip(";").rstrip()
        if _LIMIT_RE.search(q) or _COMPOUND_RE.search(q):
            return q
        return f"{q} LIMIT {task.row_end - task.row_begin} OFFSET {task.row_begin};"

    def _process(self, task: _Task, query: str) -> None:
        sql = self._limit_query(task, query)
        limited = sql != query or not query.lstrip()[:7].upper().startswith(_NO_LIMIT_PREFIXES)
        row = task.row_begin
        self.db.log_sql(sql)
        with self.db.get(self.tr("reading rows"), self.force_wait) as conn:
            if conn is None:
                return
            self._in_db = True
            for attempt in (0, 1):
                try:
                    cur = self.db.run(conn, sql)
                    while not task.cancel:
                        batch = cur.fetchmany(256)
                        if not batch:
                            break
                        with self.cache_lock:
                            for r in batch:
                                self.cache.set(row, list(r))
                                row += 1
                    cur.close()
                    break
                except sqlite3.OperationalError as e:
                    if attempt == 0 and "decode" in str(e).lower():
                        from .db import _text_factory_fallback
                        conn.text_factory = _text_factory_fallback
                        row = task.row_begin
                        continue
                    if not task.cancel:
                        self.error.emit(task.token, str(e))
                    break
                except sqlite3.Error as e:
                    if not task.cancel:
                        self.error.emit(task.token, str(e))
                    break
            conn.text_factory = str
            self._in_db = False

        if not self._first_chunk_loaded and not task.cancel:
            self._first_chunk_loaded = True
            if limited and row == task.row_end and self.count_query:
                self._trigger_row_count(task.token)
            else:
                self.rowCountComplete.emit(task.token, row - task.row_begin)
        self.fetched.emit(task.token, task.row_begin, row)

    def _trigger_row_count(self, token: int) -> None:
        count_query = self.count_query

        def worker() -> None:
            self.db.log_sql(count_query)
            with self.db.get(self.tr("determining row count..."), True) as conn:
                if conn is None:
                    return
                try:
                    n = conn.execute(count_query).fetchone()[0]
                except sqlite3.Error as e:
                    self.error.emit(token, str(e))
                    return
            self.rowCountComplete.emit(token, int(n))

        self._count_thread = threading.Thread(target=worker, daemon=True)
        self._count_thread.start()


def build_count_query_for(query: str) -> str:
    """Count query for an arbitrary SELECT (used for SQL editor results)."""
    q = query.strip().rstrip(";")
    return f"SELECT COUNT(*) FROM ({q});"

