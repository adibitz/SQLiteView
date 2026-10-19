"""The database engine. Port of upstream ``src/sqlitedb.{h,cpp}`` (class ``DBBrowserDB``).

All behaviour that defines how DB4S treats a database is kept: changes are wrapped in
SAVEPOINTs so nothing is committed until "Write Changes", schema objects are re-read after
DDL, ALTER TABLE is emulated by rebuilding tables, records are addressed by their rowid (or
the PRIMARY KEY of WITHOUT ROWID tables), etc. SQLite itself runs in C through the
standard ``sqlite3`` module.

UI interactions that the C++ code performed directly (message boxes) are delegated to
optional callbacks so that the engine stays free of widgets.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import IntEnum
from functools import lru_cache
from typing import Any, Callable, Iterator, Optional

from PySide6.QtCore import QObject, QThread, QCoreApplication, Signal

from . import parser, sqlb
from .data import value_to_text
from .settings import Settings
from .sqlsplit import is_transaction_control, iter_statements

TrackColumns = list[tuple[Optional[str], Optional[str]]]


class LogMessageType(IntEnum):
    User = 0
    App = 1
    ErrorLog = 2


class ChoiceOnUse(IntEnum):
    Ask = 0
    Wait = 1
    CancelOther = 2


@dataclass
class ObjectMap:
    tables: dict[str, sqlb.Table] = field(default_factory=dict)  # tables AND views
    indices: dict[str, sqlb.Index] = field(default_factory=dict)
    triggers: dict[str, sqlb.Trigger] = field(default_factory=dict)

    def empty(self) -> bool:
        return not (self.tables or self.indices or self.triggers)


JOURNAL_MODE_VALUES = ["DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"]
LOCKING_MODE_VALUES = ["NORMAL", "EXCLUSIVE"]
DATATYPES = ["INTEGER", "TEXT", "BLOB", "REAL", "NUMERIC"]
DATATYPES_STRICT = ["INT", "INTEGER", "TEXT", "BLOB", "REAL", "ANY"]

_STRUCTURE_PREFIXES = ("ALTER", "CREATE", "DROP", "ROLLBACK")
_MISSING_COLLATION_RE = re.compile(r"no such collation sequence: (\S+)", re.I)


# --------------------------------------------------------------------------- SQL functions


@lru_cache(maxsize=50)
def _compile_regex(pattern: str) -> re.Pattern:
    return re.compile(pattern)


def _sql_regexp(pattern, value) -> Optional[int]:
    if pattern is None or value is None:
        return None
    try:
        rx = _compile_regex(str(pattern))
    except re.error as e:
        raise ValueError("invalid operand") from e
    return 1 if rx.search(value_to_text(value)) else 0


def _sql_make_single_value(*args) -> str:
    out = []
    for a in args:
        t = value_to_text(a) if a is not None else ""
        out.append(f"{len(t.encode('utf-8'))}:{t}")
    return "".join(out)


def _collate_utf16(a: str, b: str) -> int:
    return (a > b) - (a < b)


def _collate_utf16ci(a: str, b: str) -> int:
    a, b = a.casefold(), b.casefold()
    return (a > b) - (a < b)


def _collate_binary(a: str, b: str) -> int:
    ab, bb = a.encode("utf-8"), b.encode("utf-8")
    if len(ab) == len(bb):
        return (ab > bb) - (ab < bb)
    return len(ab) - len(bb)


def _text_factory_fallback(b: bytes):
    """Decode TEXT as UTF-8; keep undecodable data as bytes so it can still be shown as binary."""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b


def is_main_thread() -> bool:
    app = QCoreApplication.instance()
    return app is None or QThread.currentThread() is app.thread()


# --------------------------------------------------------------------------- engine


class DBBrowserDB(QObject):
    sqlExecuted = Signal(str, int)
    dbChanged = Signal(bool)
    structureUpdated = Signal()
    databaseInUseChanged = Signal(bool, str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._conn: Optional[sqlite3.Connection] = None
        self._cond = threading.Condition()
        self._owner: Optional[int] = None
        self._depth = 0
        self._db_user = ""
        self._structure_checks_disabled = 0
        self.cur_file = ""
        self.last_error = ""
        self.savepoints: list[str] = []
        self.read_only = False
        self.encrypted = False
        self.schemata: dict[str, ObjectMap] = {}
        # UI hooks (set by the main window). busy_handler(user) -> True to interrupt the other
        # operation; collation_handler(name) -> True to register a default collation.
        self.busy_handler: Optional[Callable[[str], bool]] = None
        self.collation_handler: Optional[Callable[[str], bool]] = None
        self.warning_handler: Optional[Callable[[str], None]] = None
        self._temp_counter = 0

    # ------------------------------------------------------------------ connection access

    def is_open(self) -> bool:
        return self._conn is not None

    @property
    def dirty(self) -> bool:
        return bool(self.savepoints)

    def _wait_and_own(self, user: str, choice: ChoiceOnUse) -> bool:
        """Acquire exclusive access. Returns True if this is the outermost acquisition."""
        me = threading.get_ident()
        if choice == ChoiceOnUse.Ask and not is_main_thread():
            choice = ChoiceOnUse.Wait
        with self._cond:
            if self._owner == me:
                self._depth += 1
                return False
            asked = False
            while self._owner is not None:
                if choice == ChoiceOnUse.CancelOther and self._conn is not None:
                    self._conn.interrupt()
                elif choice == ChoiceOnUse.Ask and not asked and self.busy_handler is not None:
                    asked = True
                    other = self._db_user
                    self._cond.release()
                    try:
                        cancel = self.busy_handler(other)
                    finally:
                        self._cond.acquire()
                    if cancel and self._conn is not None and self._owner is not None:
                        self._conn.interrupt()
                self._cond.wait(0.05)
                if is_main_thread():
                    QCoreApplication.processEvents()
            self._owner = me
            self._depth = 1
            self._db_user = user
            return True

    def _release(self) -> None:
        with self._cond:
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
                self._db_user = ""
                self._cond.notify_all()

    @contextmanager
    def exclusive(self, user: str = "", choice: ChoiceOnUse = ChoiceOnUse.Ask) -> Iterator[Optional[sqlite3.Connection]]:
        """Internal exclusive access (re-entrant for the owning thread)."""
        if self._conn is None:
            yield None
            return
        self._wait_and_own(user, choice)
        try:
            yield self._conn
        finally:
            self._release()

    @contextmanager
    def get(self, user: str, force_wait: bool = False) -> Iterator[Optional[sqlite3.Connection]]:
        """Borrow the connection for a (possibly background) operation; port of ``DBBrowserDB::get``."""
        if self._conn is None:
            yield None
            return
        outer = self._wait_and_own(user, ChoiceOnUse.Wait if force_wait else ChoiceOnUse.Ask)
        if outer:
            self.databaseInUseChanged.emit(True, user)
        try:
            yield self._conn
        finally:
            self._release()
            if outer:
                self.databaseInUseChanged.emit(False, "")

    def is_busy(self) -> bool:
        with self._cond:
            return self._owner is not None and self._owner != threading.get_ident()

    def interrupt_query(self) -> None:
        if self._conn is not None:
            self._conn.interrupt()

    @contextmanager
    def no_structure_update_checks(self):
        self._structure_checks_disabled += 1
        try:
            yield
        finally:
            self._structure_checks_disabled -= 1

    # ------------------------------------------------------------------ open / close

    def _connect(self, path: str, read_only: bool) -> sqlite3.Connection:
        if path == ":memory:" or path.startswith("file:"):
            uri, target = path.startswith("file:"), path
        else:
            target = "file:" + _quote_uri_path(os.path.abspath(path)) + ("?mode=ro" if read_only else "?mode=rw")
            uri = True
        conn = sqlite3.connect(target, uri=uri, check_same_thread=False, isolation_level=None,
                               detect_types=0, cached_statements=256)
        return conn

    def _setup_connection(self, conn: sqlite3.Connection) -> None:
        conn.create_collation("UTF16", _collate_utf16)
        conn.create_collation("UTF16CI", _collate_utf16ci)
        if not Settings.get("extensions", "disableregex"):
            conn.create_function("REGEXP", 2, _sql_regexp, deterministic=True)
        conn.create_function("sqlb_make_single_value", -1, _sql_make_single_value, deterministic=True)

    def open(self, path: str, read_only: bool = False, load_extensions: bool = True,
             run_default_sql: bool = True) -> bool:
        """Open ``path``. ``load_extensions`` / ``run_default_sql`` apply the user's extension list and
        "SQL to execute after opening" from the settings (off for the MCP server, which must not hand
        extension functions such as file access to an AI assistant)."""
        if self.is_open() and not self.close(force=True):
            return False
        self.encrypted = False
        try:
            conn = self._connect(path, read_only)
            # Same probe as tryEncryptionSettings(): make sure this is a readable SQLite file
            conn.execute("SELECT COUNT(*) FROM sqlite_master;").fetchone()
        except sqlite3.DatabaseError as e:
            self.last_error = str(e)
            try:
                conn.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            return False
        self._conn = conn
        self._setup_connection(conn)
        self.set_pragma("foreign_keys", "1" if Settings.get("db", "foreignkeys") else "0")

        if path == ":memory:":
            self.read_only = False
        else:
            ap = os.path.abspath(path)
            self.read_only = read_only or not os.access(ap, os.W_OK) or not os.access(os.path.dirname(ap), os.W_OK)

        if load_extensions:
            self.load_extensions_from_settings()

        if not self.read_only and run_default_sql:
            default_sql = Settings.get("db", "defaultsqltext")
            if default_sql:
                self.execute_multi_sql(default_sql, dirty=False, log=True)

        self.cur_file = path
        self.update_schema()
        return True

    def create(self, path: str, load_extensions: bool = True, run_default_sql: bool = True) -> bool:
        if self.is_open() and not self.close(force=True):
            return False
        try:
            conn = sqlite3.connect(path, isolation_level=None)
            enc = Settings.get("db", "defaultencoding")
            if enc and enc.upper() not in ("UTF-8", "UTF8", "LATIN1"):
                conn.execute(f"PRAGMA encoding = {sqlb.escape_string(enc)};")
            # Force SQLite to write a proper file header (same trick as upstream)
            conn.execute("CREATE TABLE notempty (id integer primary key);")
            conn.execute("DROP TABLE notempty;")
            conn.close()
        except sqlite3.Error as e:
            self.last_error = str(e)
            return False
        return self.open(path, load_extensions=load_extensions, run_default_sql=run_default_sql)

    def close(self, force: bool = False) -> bool:
        """Close the database. With pending changes, the caller must commit/revert first
        unless ``force`` is set (in which case they are rolled back)."""
        if self._conn is None:
            return True
        if self.dirty and not force:
            return False
        with self.exclusive("closing database"):
            if self.dirty:
                self.revert_all()
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
        self.cur_file = ""
        self.schemata.clear()
        self.savepoints.clear()
        self.dbChanged.emit(False)
        self.structureUpdated.emit()
        return True

    def attach(self, file_path: str, attach_as: str) -> bool:
        if not self._conn or not attach_as:
            return False
        if not self.execute_sql(f"ATTACH {sqlb.escape_string(file_path)} AS {sqlb.escape_identifier(attach_as)}", False):
            return False
        self.update_schema()
        return True

    def detach(self, attached_as: str) -> bool:
        if not self._conn:
            return False
        if not self.execute_sql(f"DETACH {sqlb.escape_identifier(attached_as)};", False):
            return False
        self.update_schema()
        return True

    def attached_databases(self) -> list[tuple[str, str]]:
        """(schema, file) of every attached database, without main and temp (as stored in project files)."""
        if self._conn is None:
            return []
        self.log_sql("PRAGMA database_list;")
        with self.exclusive("listing attached databases") as conn:
            if conn is None:
                return []
            try:
                _, rows = self.fetch_all(conn, "PRAGMA database_list;")
            except sqlite3.Error as e:
                self.last_error = str(e)
                return []
        return [(schema, path or "") for _, schema, path in rows if schema not in ("main", "temp")]

    def save_as(self, filename: str) -> bool:
        if not self._conn:
            return False
        with self.exclusive("saving database"):
            try:
                dest = sqlite3.connect(filename, check_same_thread=False, isolation_level=None)
                self._conn.backup(dest)
            except sqlite3.Error as e:
                self.last_error = str(e)
                return False
            self._conn.close()
            self._conn = dest
            self._setup_connection(dest)
            self.cur_file = filename
            self.savepoints.clear()
        self.dbChanged.emit(False)
        return True

    @staticmethod
    def sqlite_version() -> str:
        return sqlite3.sqlite_version

    # ------------------------------------------------------------------ savepoints

    def set_savepoint(self, name: str = "RESTOREPOINT", unique: bool = True) -> bool:
        if not self.is_open():
            return False
        if self.read_only:
            return False
        if unique and name in self.savepoints:
            return True
        self.execute_sql(f"SAVEPOINT {sqlb.escape_identifier(name)};", False, True)
        self.savepoints.append(name)
        self.dbChanged.emit(self.dirty)
        return True

    def release_savepoint(self, name: str = "RESTOREPOINT") -> bool:
        if not self.is_open():
            return False
        if name not in self.savepoints:
            return True
        if not self.execute_sql(f"RELEASE {sqlb.escape_identifier(name)};", False, True):
            return False
        idx = len(self.savepoints) - 1 - self.savepoints[::-1].index(name)
        del self.savepoints[idx:]
        self.dbChanged.emit(self.dirty)
        return True

    def revert_to_savepoint(self, name: str = "RESTOREPOINT") -> bool:
        if not self.is_open() or name not in self.savepoints:
            return False
        self.execute_sql(f"ROLLBACK TO SAVEPOINT {sqlb.escape_identifier(name)};", False, True)
        self.execute_sql(f"RELEASE {sqlb.escape_identifier(name)};", False, True)
        idx = len(self.savepoints) - 1 - self.savepoints[::-1].index(name)
        del self.savepoints[idx:]
        self.dbChanged.emit(self.dirty)
        return True

    def release_all_savepoints(self) -> bool:
        if not self._conn:
            return False
        with self.exclusive("committing changes"):
            while self.savepoints:
                if not self.release_savepoint(self.savepoints[0]):
                    return False
            if self._conn.in_transaction:
                self.execute_sql("COMMIT;", False, True)
        return True

    def revert_all(self) -> bool:
        while self.savepoints:
            if not self.revert_to_savepoint(self.savepoints[0]):
                return False
        return True

    def set_undo_savepoint(self) -> bool:
        return self.set_savepoint("UNDOPOINT", unique=False)

    def revert_to_undo_savepoint(self) -> bool:
        return self.revert_to_savepoint("UNDOPOINT")

    def release_undo_savepoint(self) -> bool:
        return self.release_savepoint("UNDOPOINT")

    def generate_savepoint_name(self, identifier: str = "") -> str:
        return f"db4s_{identifier}_{time.time_ns()}"

    def generate_temporary_table_name(self, schema: str) -> str:
        while True:
            self._temp_counter += 1
            name = f"sqlb_temp_table_{self._temp_counter}"
            if self.get_table_by_name(sqlb.ObjectIdentifier(schema, name)) is None:
                return name

    # ------------------------------------------------------------------ execution

    def log_sql(self, statement: str, msgtype: LogMessageType = LogMessageType.App) -> None:
        self.sqlExecuted.emit(statement.strip(), int(msgtype))

    def _handle_missing_collation(self, err: Exception) -> bool:
        m = _MISSING_COLLATION_RE.search(str(err))
        if not m or self._conn is None:
            return False
        name = m.group(1)
        if self.collation_handler is not None and not self.collation_handler(name):
            return False
        self._conn.create_collation(name, _collate_binary)
        return True

    def run(self, conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """Execute one statement, registering missing collations on demand (replaces
        upstream's ``sqlite3_collation_needed`` callback)."""
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            if self._handle_missing_collation(e):
                return conn.execute(sql, params)
            raise

    def fetch_all(self, conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> tuple[list[str], list[tuple]]:
        """Run a query and return (column names, rows); invalid UTF-8 text is returned as bytes."""
        try:
            cur = self.run(conn, sql, params)
            return [d[0] for d in (cur.description or [])], cur.fetchall()
        except sqlite3.OperationalError as e:
            if "decode" not in str(e).lower():
                raise
        conn.text_factory = _text_factory_fallback
        try:
            cur = self.run(conn, sql, params)
            return [d[0] for d in (cur.description or [])], cur.fetchall()
        finally:
            conn.text_factory = str

    def execute_sql(self, statement: str, dirty: bool = True, log: bool = True,
                    callback: Optional[Callable[[tuple, list[str]], bool]] = None) -> bool:
        """Execute one or more statements (``sqlite3_exec`` semantics). ``callback(row, names)``
        is invoked for result rows; returning True aborts."""
        if self._conn is None:
            self.last_error = self.tr("No database file opened")
            return False
        with self.exclusive("executing SQL") as conn:
            if dirty:
                self.set_savepoint()
            if log:
                self.log_sql(statement, LogMessageType.App)
            try:
                for st in iter_statements(statement):
                    if callback is None:
                        self.run(conn, st.text)
                    else:
                        names, rows = self.fetch_all(conn, st.text)
                        for row in rows:
                            if callback(row, names):
                                self.last_error = "query aborted by callback"
                                return False
            except sqlite3.Error as e:
                self.last_error = f"{e} ({statement})"
                return False
        if not self._structure_checks_disabled and statement.lstrip()[:8].upper().startswith(_STRUCTURE_PREFIXES):
            self.update_schema()
        return True

    def execute_multi_sql(self, query: str, dirty: bool = True, log: bool = False,
                          progress: Optional[Callable[[int], bool]] = None) -> bool:
        """Execute a script statement by statement; on error roll back (``executeMultiSQL``)."""
        if self._conn is None:
            self.last_error = self.tr("No database file opened")
            return False
        if log:
            self.log_sql(query, LogMessageType.App)
        total = max(1, len(query))
        structure_updated = False
        savepoint_name = ""
        last_progress = -1
        line = 0
        with self.exclusive("executing SQL script") as conn:
            if dirty:
                savepoint_name = self.generate_savepoint_name("execmultisql")
                self.set_savepoint(savepoint_name)
            for st in iter_statements(query):
                line += 1
                pct = int(st.start * 100 / total)
                if progress is not None and pct > last_progress:
                    last_progress = pct
                    if not progress(pct):
                        self.last_error = self.tr("Action cancelled.")
                        if savepoint_name:
                            self.revert_to_savepoint(savepoint_name)
                        return False
                head = st.text.lstrip()[:8].upper()
                if is_transaction_control(st.text):
                    if not savepoint_name:
                        savepoint_name = self.generate_savepoint_name("execmultisql")
                        self.set_savepoint(savepoint_name)
                        dirty = True
                    continue
                if not self._structure_checks_disabled and head.startswith(_STRUCTURE_PREFIXES):
                    structure_updated = True
                try:
                    cur = self.run(conn, st.text)
                    cur.fetchall() if cur.description else None
                except sqlite3.Error as e:
                    self.last_error = self.tr("Error in statement #%1: %2.\nAborting execution%3.") \
                        .replace("%1", str(line)).replace("%2", str(e)) \
                        .replace("%3", self.tr(" and rolling back") if dirty else "")
                    if dirty and savepoint_name:
                        self.revert_to_savepoint(savepoint_name)
                    if structure_updated:
                        self.update_schema()
                    return False
        if progress is not None:
            progress(100)
        if structure_updated:
            self.update_schema()
        return True

    def query_single_value(self, sql: str, log: bool = True, choice: ChoiceOnUse = ChoiceOnUse.Ask,
                           params: tuple = ()) -> Any:
        if self._conn is None:
            return None
        if log:
            self.log_sql(sql, LogMessageType.App)
        with self.exclusive("querying value", choice) as conn:
            try:
                _, rows = self.fetch_all(conn, sql, params)
            except sqlite3.Error as e:
                self.last_error = self.tr("could not execute command: %1").replace("%1", str(e))
                return None
        if rows and rows[0]:
            return rows[0][0]
        return None

    # ------------------------------------------------------------------ records

    def _primary_key_for_editing(self, table: sqlb.ObjectIdentifier, pseudo_pk: list[str]) -> list[str]:
        if pseudo_pk:
            return list(pseudo_pk)
        t = self.get_table_by_name(table)
        return t.rowid_columns() if t else []

    @staticmethod
    def rowid_selector(pks: list[str]) -> str:
        """SQL expression that addresses a record by ``pks`` (``_rowid_`` or the PRIMARY KEY columns)."""
        if len(pks) == 1:
            return sqlb.escape_identifier(pks[0])
        return "sqlb_make_single_value(" + ",".join(sqlb.escape_identifiers(pks)) + ")"

    def _rowid_where(self, pks: list[str]) -> str:
        return self.rowid_selector(pks) + "=?"

    def get_row(self, table: sqlb.ObjectIdentifier, rowid: Any) -> Optional[list]:
        t = self.get_table_by_name(table)
        if self._conn is None or t is None:
            return None
        sql = f"SELECT * FROM {table.to_string()} WHERE {self._rowid_where(t.rowid_columns())}"
        with self.exclusive("reading row") as conn:
            try:
                _, rows = self.fetch_all(conn, sql, (rowid,))
            except sqlite3.Error as e:
                self.last_error = str(e)
                return None
        return list(rows[0]) if rows else None

    def _max(self, table: sqlb.ObjectIdentifier, column: str) -> int:
        query = f"SELECT MAX(CAST({sqlb.escape_identifier(column)} AS INTEGER)) FROM {table.to_string()}"
        objmap = self.schemata.get(table.schema)
        if objmap and "sqlite_sequence" in objmap.tables:
            t = self.get_table_by_name(table)
            pk = t.primary_key_names() if t else []
            if len(pk) == 1 and pk[0] == column:
                query = (f"SELECT COALESCE(SUM(seq), ({query})) FROM {sqlb.escape_identifier(table.schema)}.sqlite_sequence "
                         f"WHERE name={sqlb.escape_string(table.name)}")
        v = self.query_single_value(query)
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    def _empty_insert_stmt(self, schema: str, t: sqlb.Table, pk_value: Optional[str] = None) -> str:
        stmt = f"INSERT INTO {sqlb.escape_identifier(schema)}.{sqlb.escape_identifier(t.name)}"
        pk = t.primary_key_names()
        fields, vals = [], []
        for f in t.fields:
            if f.generated:
                continue
            if len(pk) == 1 and pk[0] == f.name:
                fields.append(f.name)
                if pk_value is not None:
                    vals.append(sqlb.escape_string(pk_value) if f.is_text() else pk_value)
                elif f.notnull:
                    newval = str(self._max(sqlb.ObjectIdentifier(schema, t.name), f.name) + 1)
                    vals.append(sqlb.escape_string(newval) if f.is_text() else newval)
                else:
                    vals.append("NULL")
            elif f.notnull and not f.default_value:
                fields.append(f.name)
                vals.append("0" if f.is_integer() else "''")
            elif not f.default_value:
                fields.append(f.name)
                vals.append("NULL")
        if not fields:
            return stmt + " DEFAULT VALUES;"
        return stmt + "(" + ",".join(sqlb.escape_identifiers(fields)) + ") VALUES (" + ",".join(vals) + ");"

    def add_record(self, table: sqlb.ObjectIdentifier) -> Optional[Any]:
        """Insert an empty record; returns its rowid (or PK value for WITHOUT ROWID tables)."""
        t = self.get_table_by_name(table)
        if self._conn is None or t is None:
            return None
        pk_value = None
        if t.without_rowid:
            rc = t.rowid_columns()
            pk_value = str(self._max(table, rc[-1]) + 1)
            stmt = self._empty_insert_stmt(table.schema, t, pk_value)
        else:
            stmt = self._empty_insert_stmt(table.schema, t)
        with self.exclusive("adding record") as conn:
            if not self.execute_sql(stmt):
                return None
            if t.without_rowid:
                return int(pk_value) if pk_value.isdigit() else pk_value
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def delete_records(self, table: sqlb.ObjectIdentifier, rowids: list, pseudo_pk: list[str] = ()) -> bool:
        if not self.is_open():
            return False
        pks = self._primary_key_for_editing(table, list(pseudo_pk))
        if not pks:
            self.last_error = self.tr("Cannot delete this object")
            return False
        target = self.rowid_selector(pks)
        self.set_savepoint()
        with self.exclusive("deleting records") as conn:
            try:
                chunk = 500
                for i in range(0, len(rowids), chunk):
                    part = rowids[i:i + chunk]
                    sql = f"DELETE FROM {table.to_string()} WHERE {target} IN ({','.join('?' * len(part))});"
                    self.log_sql(_expand_sql(sql, part))
                    self.run(conn, sql, part)
            except sqlite3.Error as e:
                self.last_error = str(e)
                return False
        return True

    def update_record(self, table: sqlb.ObjectIdentifier, column: str, rowid: Any, value: Any,
                      pseudo_pk: list[str] = ()) -> bool:
        """Set one cell. ``value`` is already typed (None/int/float/str/bytes), mirroring the
        ``force_type`` handling of the upstream function."""
        if not self.is_open():
            return False
        pks = self._primary_key_for_editing(table, list(pseudo_pk))
        if not pks:
            self.last_error = self.tr("Cannot set data on this object")
            return False
        sql = f"UPDATE {table.to_string()} SET {sqlb.escape_identifier(column)}=? WHERE {self._rowid_where(pks)}"
        self.set_savepoint()
        with self.exclusive("updating record") as conn:
            params = (value, rowid)
            self.log_sql(_expand_sql(sql, params))
            try:
                self.run(conn, sql, params)
            except sqlite3.Error as e:
                self.last_error = str(e)
                return False
        return True

    # ------------------------------------------------------------------ DDL

    def create_table(self, name: sqlb.ObjectIdentifier, fields: list[sqlb.Field]) -> bool:
        t = sqlb.Table(name.name)
        t.fields = list(fields)
        return self.execute_sql(t.sql(name.schema))

    def add_column(self, table: sqlb.ObjectIdentifier, f: sqlb.Field) -> bool:
        return self.execute_sql(f"ALTER TABLE {table.to_string()} ADD COLUMN {f.to_string(' ', ' ')}")

    def rename_table(self, schema: str, from_table: str, to_table: str) -> bool:
        if from_table == to_table:
            return True
        if from_table.lower() == to_table.lower():
            tmp = self.generate_temporary_table_name(schema)
            return self.rename_table(schema, from_table, tmp) and self.rename_table(schema, tmp, to_table)
        sql = (f"ALTER TABLE {sqlb.escape_identifier(schema)}.{sqlb.escape_identifier(from_table)} "
               f"RENAME TO {sqlb.escape_identifier(to_table)}")
        if not self.execute_sql(sql):
            self.last_error = (self.tr("Error renaming table '%1' to '%2'.\nMessage from database engine:\n%3")
                               .replace("%1", from_table).replace("%2", to_table).replace("%3", self.last_error))
            return False
        return True

    def alter_table(self, tablename: sqlb.ObjectIdentifier, new_table: sqlb.Table, track_columns: TrackColumns,
                    new_schema: str = "") -> bool:
        """Change a table to look like ``new_table``. ``track_columns`` holds (old, new) column name
        pairs: (a, a) keep, (a, b) rename, (a, None) drop, (None, b) add. Port of ``alterTable``."""
        # ---- part 1: checks
        if not new_schema:
            new_schema = tablename.schema
            if tablename.name != new_table.name and self.get_table_by_name(sqlb.ObjectIdentifier(new_schema, new_table.name)):
                self.last_error = self.tr("A table with the name '%1' already exists in schema '%2'.") \
                    .replace("%1", new_table.name).replace("%2", new_schema)
                return False
        elif new_schema != tablename.schema and self.get_table_by_name(sqlb.ObjectIdentifier(new_schema, new_table.name)):
            self.last_error = self.tr("A table with the name '%1' already exists in schema '%2'.") \
                .replace("%1", new_table.name).replace("%2", new_schema)
            return False

        old_ptr = self.get_table_by_name(tablename)
        if old_ptr is None:
            self.last_error = self.tr("No table with name '%1' exists in schema '%2'.") \
                .replace("%1", tablename.name).replace("%2", tablename.schema)
            return False
        old_table = old_ptr.clone()
        track = list(track_columns)

        for old, _ in track:
            if old is not None and old_table.find_field(old) is None:
                self.last_error = self.tr("Cannot find column %1.").replace("%1", old)
                return False
        tracked_old = {o for o, _ in track if o is not None}
        for f in old_table.fields:
            if f.name not in tracked_old:
                track.append((f.name, f.name))
        for _, new in track:
            if new is not None and new_table.find_field(new) is None:
                self.last_error = self.tr("Cannot find column %1.").replace("%1", new)
                return False

        if old_table == new_table and new_schema == tablename.schema:
            return True

        savepoint = self.generate_savepoint_name("renamecolumn")
        if not self.set_savepoint(savepoint):
            self.last_error = self.tr("Creating savepoint failed. DB says: %1").replace("%1", self.last_error)
            return False

        with self.no_structure_update_checks():
            # ---- part 2: use SQLite's own ALTER TABLE as far as possible
            changed = False
            if new_schema == tablename.schema and tablename.name != new_table.name:
                if not self.rename_table(tablename.schema, old_table.name, new_table.name):
                    err = self.last_error
                    self.revert_to_savepoint(savepoint)
                    self.last_error = err
                    return False
                changed = True

            new_names = {n for o, n in track if o is None}
            for f in new_table.fields:
                if f.name in new_names:
                    if not self.add_column(sqlb.ObjectIdentifier(tablename.schema, new_table.name), f):
                        err = self.last_error
                        self.revert_to_savepoint(savepoint)
                        self.last_error = err
                        return False
                    changed = True
            # Added columns now exist in the table and keep their name
            track = [((n, n) if o is None else (o, n)) for o, n in track]

            new_track: TrackColumns = []
            for old, new in track:
                if old is not None and new is not None and old != new:
                    sql = (f"ALTER TABLE {sqlb.ObjectIdentifier(tablename.schema, new_table.name).to_string()} RENAME COLUMN "
                           f"{sqlb.escape_identifier(old)} TO {sqlb.escape_identifier(new)}")
                    if not self.execute_sql(sql):
                        err = self.tr("Renaming the column failed. DB says:\n%1").replace("%1", self.last_error)
                        self.revert_to_savepoint(savepoint)
                        self.last_error = err
                        return False
                    changed = True
                    new_track.append((new, new))
                else:
                    new_track.append((old, new))
            track = new_track

            if changed:
                self.update_schema()
                cur = self.get_table_by_name(sqlb.ObjectIdentifier(tablename.schema, new_table.name))
                if cur is not None:
                    old_table = cur.clone()

            if tablename.schema == new_schema and old_table == new_table:
                if not self.release_savepoint(savepoint):
                    self.last_error = self.tr("Releasing savepoint failed. DB says: %1").replace("%1", self.last_error)
                    return False
                self.update_schema()
                return True

            # ---- part 3: rebuild the table
            tmp_table = new_table.clone()
            tmp_table.name = self.generate_temporary_table_name(new_schema)
            if not self.execute_sql(tmp_table.sql(new_schema), True, True):
                err = self.tr("Creating new table failed. DB says: %1").replace("%1", self.last_error)
                self.revert_to_savepoint(savepoint)
                self.last_error = err
                return False

            copy_from, copy_to = [], []
            for old, new in track:
                if old is None or new is None:
                    continue
                nf = new_table.find_field(new)
                if nf is None or nf.generated:
                    continue
                if old_table.find_field(old) is None:
                    continue
                copy_from.append(old)
                copy_to.append(new)

            if copy_to:
                sql = (f"INSERT INTO {sqlb.escape_identifier(new_schema)}.{sqlb.escape_identifier(tmp_table.name)} "
                       f"({','.join(sqlb.escape_identifiers(copy_to))}) SELECT {','.join(sqlb.escape_identifiers(copy_from))} "
                       f"FROM {sqlb.escape_identifier(tablename.schema)}.{sqlb.escape_identifier(old_table.name)}")
                if not self.execute_sql(sql):
                    err = self.tr("Copying data to new table failed. DB says:\n%1").replace("%1", self.last_error)
                    self.revert_to_savepoint(savepoint)
                    self.last_error = err
                    return False

            # Save indices, triggers and views referencing the table; DROP TABLE removes them
            other_sql: list[str] = []
            objmap = self.schemata.get(tablename.schema, ObjectMap())
            for idx in objmap.indices.values():
                if idx.base_table() != old_table.name or not idx.original_sql:
                    continue
                if idx.fully_parsed:
                    i2 = idx.clone()
                    for old, new in track:
                        if old is None:
                            continue
                        if new is not None:
                            for c in i2.fields:
                                if c.name == old:
                                    c.name = new
                        else:
                            while i2.remove_field(old):
                                pass
                    if i2.fields:
                        other_sql.append(i2.sql(new_schema))
                else:
                    other_sql.append(idx.original_sql + ";")
            for trg in objmap.triggers.values():
                if trg.base_table() == old_table.name:
                    other_sql.append(trg.original_sql + ";")

            fk_old = self.get_pragma("defer_foreign_keys")
            self.set_pragma("defer_foreign_keys", "1")
            # Since SQLite 3.26 RENAME validates views/triggers, which fails while the original
            # table is gone. The legacy mode restores the documented 12-step behaviour and can be
            # toggled inside the transaction.
            legacy_old = self.query_single_value("PRAGMA legacy_alter_table;", log=False)
            self.execute_sql("PRAGMA legacy_alter_table = 1;", False, True)

            def restore_pragmas() -> None:
                self.execute_sql(f"PRAGMA legacy_alter_table = {int(legacy_old or 0)};", False, True)
                self.set_pragma("defer_foreign_keys", str(fk_old if fk_old is not None else 0))

            if not self.execute_sql(f"DROP TABLE {sqlb.escape_identifier(tablename.schema)}.{sqlb.escape_identifier(old_table.name)}", True, True):
                err = self.tr("Deleting old table failed. DB says: %1").replace("%1", self.last_error)
                restore_pragmas()
                self.revert_to_savepoint(savepoint)
                self.last_error = err
                return False
            if not self.rename_table(new_schema, tmp_table.name, new_table.name):
                err = self.last_error
                restore_pragmas()
                self.revert_to_savepoint(savepoint)
                self.last_error = err
                return False
            restore_pragmas()

            errored = [s for s in other_sql if not self.execute_sql(s, True, True)]
            if errored and self.warning_handler:
                self.warning_handler(self.tr(
                    "Restoring some of the objects associated with this table failed. This is most likely because "
                    "some column names changed. Here's the SQL statement which you might want to fix and execute "
                    "manually:\n\n") + "\n".join(errored))

            if not self.release_savepoint(savepoint):
                self.last_error = self.tr("Releasing savepoint failed. DB says: %1").replace("%1", self.last_error)
                return False
        self.update_schema()
        return True

    # ------------------------------------------------------------------ schema

    def _query_column_information(self, conn: sqlite3.Connection, schema: str, name: str,
                                  pragma: str = "TABLE_INFO") -> list[tuple[str, str]]:
        sql = f"PRAGMA {sqlb.escape_identifier(schema)}.{pragma}({sqlb.escape_identifier(name)});"
        self.log_sql(sql)
        try:
            return [(r[1], r[2] or "") for r in self.run(conn, sql).fetchall()]
        except sqlite3.Error:
            self.last_error = self.tr("could not get column information")
            return []

    def update_schema(self) -> None:
        self.schemata = {}
        if self._conn is None:
            self.structureUpdated.emit()
            return
        with self.exclusive("reading schema") as conn:
            try:
                dbs = self.run(conn, "PRAGMA database_list;").fetchall()
            except sqlite3.Error as e:
                self.last_error = str(e)
                dbs = []
            self.log_sql("PRAGMA database_list;")
            for _, schema_name, _file in dbs:
                objmap = self.schemata.setdefault(schema_name, ObjectMap())
                if schema_name == "temp":
                    statement = "SELECT type,name,sql,tbl_name FROM sqlite_temp_master;"
                else:
                    statement = f"SELECT type,name,sql,tbl_name FROM {sqlb.escape_identifier(schema_name)}.sqlite_master;"
                self.log_sql(statement)
                try:
                    _, rows = self.fetch_all(conn, statement)
                except sqlite3.Error as e:
                    self.last_error = str(e)
                    continue
                for val_type, val_name, val_sql, val_tbl in rows:
                    if not val_sql:
                        continue
                    val_sql = val_sql.replace("\r", "")
                    if val_type in ("table", "view"):
                        t = parser.parse_table(val_sql) if val_type == "table" else parser.parse_view(val_sql)
                        if not t.fully_parsed:
                            t.name = val_name
                        cols = None
                        if (not t.fully_parsed and not t.fields) or t.is_virtual():
                            cols = self._query_column_information(conn, schema_name, val_name)
                            t.fields = [sqlb.Field(n, ty) for n, ty in cols]
                        elif t.fully_parsed:
                            # Safety net: the parser must agree with SQLite on the column list
                            # (table_xinfo also lists generated columns)
                            cols = self._query_column_information(conn, schema_name, val_name, "TABLE_XINFO")
                            if [c[0] for c in cols] != t.field_names():
                                t2 = sqlb.Table(val_name)
                                t2.original_sql = val_sql
                                t2.fields = [sqlb.Field(n, ty) for n, ty in cols]
                                t = t2
                        objmap.tables[val_name] = t
                    elif val_type == "index":
                        idx = parser.parse_index(val_sql)
                        if not idx.fully_parsed:
                            idx.name = val_name
                            idx.table = val_tbl
                        objmap.indices[val_name] = idx
                    elif val_type == "trigger":
                        trg = parser.parse_trigger(val_sql)
                        trg.name = val_name
                        trg.table = val_tbl
                        objmap.triggers[val_name] = trg
                # Built-in schema tables are browsable too (upstream shows them in the tree)
                master = "sqlite_temp_master" if schema_name == "temp" else "sqlite_master"
                if master not in objmap.tables:
                    mt = sqlb.Table(master)
                    mt.fields = [sqlb.Field(n, ty) for n, ty in
                                 (("type", "text"), ("name", "text"), ("tbl_name", "text"), ("rootpage", "integer"), ("sql", "text"))]
                    mt.original_sql = (f"CREATE TABLE {master} (\n  type text,\n  name text,\n  tbl_name text,\n"
                                       "  rootpage integer,\n  sql text\n)")
                    objmap.tables[master] = mt
        self.structureUpdated.emit()

    def _find_ci(self, mapping: dict, key: str):
        if key in mapping:
            return mapping[key]
        lk = key.lower()
        for k, v in mapping.items():
            if k.lower() == lk:
                return v
        return None

    def get_table_by_name(self, name: sqlb.ObjectIdentifier) -> Optional[sqlb.Table]:
        if not self.schemata or not name.schema or not name.name:
            return None
        objmap = self._find_ci(self.schemata, name.schema)
        return self._find_ci(objmap.tables, name.name) if objmap else None

    def get_index_by_name(self, name: sqlb.ObjectIdentifier) -> Optional[sqlb.Index]:
        objmap = self._find_ci(self.schemata, name.schema) if self.schemata else None
        return self._find_ci(objmap.indices, name.name) if objmap else None

    def get_trigger_by_name(self, name: sqlb.ObjectIdentifier) -> Optional[sqlb.Trigger]:
        objmap = self._find_ci(self.schemata, name.schema) if self.schemata else None
        return self._find_ci(objmap.triggers, name.name) if objmap else None

    # ------------------------------------------------------------------ pragmas & extensions

    def get_pragma(self, pragma: str) -> Any:
        if pragma == "case_sensitive_like":
            return self.query_single_value("SELECT 'x' NOT LIKE 'X';")
        return self.query_single_value(f"PRAGMA {pragma};")

    def set_pragma(self, pragma: str, value: Any) -> bool:
        sql = f"PRAGMA {pragma} = {sqlb.escape_string(str(value))};"
        # Most pragmas can't be rolled back or fail inside transactions: commit first
        if pragma != "defer_foreign_keys":
            self.release_savepoint()
        res = self.execute_sql(sql, False, True)
        if res and pragma in ("page_size", "auto_vacuum"):
            res = self.execute_sql("VACUUM;", False, True)
        return res

    def load_extension(self, file_path: str) -> bool:
        if self._conn is None:
            return False
        if not os.path.exists(file_path):
            self.last_error = self.tr("File not found.")
            return False
        with self.exclusive("loading extension") as conn:
            try:
                conn.enable_load_extension(True)
                conn.load_extension(file_path)
            except (sqlite3.Error, AttributeError) as e:
                self.last_error = str(e)
                return False
            finally:
                if not Settings.get("extensions", "enable_load_extension"):
                    try:
                        conn.enable_load_extension(False)
                    except (sqlite3.Error, AttributeError):
                        pass
        return True

    def load_extensions_from_settings(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.enable_load_extension(bool(Settings.get("extensions", "enable_load_extension")))
        except (sqlite3.Error, AttributeError):
            pass
        for ext in Settings.get("extensions", "list") or []:
            if not self.load_extension(ext) and self.warning_handler:
                self.warning_handler(self.tr("Error loading extension: %1").replace("%1", self.last_error))

    # ------------------------------------------------------------------ export

    def dump(self, filename: str, tables_to_dump: list[str], insert_col_names: bool, insert_new_syntax: bool,
             keep_original: bool, export_schema: bool, export_data: bool, keep_old_schema: bool,
             progress: Optional[Callable[[int, int], bool]] = None) -> bool:
        """Export the main schema as SQL text (port of ``DBBrowserDB::dump``)."""
        objmap = self.schemata.get("main", ObjectMap())
        tables = [t for name, t in objmap.tables.items()
                  if not t.is_view() and not name.lower().startswith("sqlite_") and name in tables_to_dump]
        total = 0
        for t in tables:
            total += int(self.query_single_value(f"SELECT COUNT(*) FROM {sqlb.ObjectIdentifier('main', t.name).to_string()}") or 0)
        done = 0
        try:
            with open(filename, "w", encoding="utf-8", newline="\n") as fh, self.exclusive("exporting SQL") as conn:
                w = fh.write
                w("BEGIN TRANSACTION;\n")
                if export_schema:
                    for t in tables:
                        if not keep_old_schema:
                            w(f"DROP TABLE IF EXISTS {sqlb.escape_identifier(t.name)};\n")
                        if t.fully_parsed and not keep_original:
                            w(t.sql("main", keep_old_schema) + "\n")
                        else:
                            stmt = t.original_sql
                            if keep_old_schema:
                                if stmt[:20].upper() == "CREATE VIRTUAL TABLE":
                                    stmt = "CREATE VIRTUAL TABLE IF NOT EXISTS" + stmt[20:]
                                else:
                                    stmt = "CREATE TABLE IF NOT EXISTS" + stmt[12:]
                            w(stmt + ";\n")
                if export_data:
                    for t in tables:
                        cols = [f.name for f in t.fields if not f.generated]
                        col_list = ",".join(sqlb.escape_identifiers(cols))
                        if any(f.generated for f in t.fields):
                            sql = f"SELECT {col_list} FROM {sqlb.escape_identifier(t.name)}"
                        else:
                            sql = f"SELECT * FROM {sqlb.escape_identifier(t.name)}"
                        line_sep = "),\n" if insert_new_syntax else ");\n"
                        head = f"INSERT INTO {sqlb.escape_identifier(t.name)}"
                        if insert_col_names:
                            head += f" ({col_list})"
                        head += " VALUES ("
                        conn.text_factory = _text_factory_fallback
                        try:
                            cur = self.run(conn, sql)
                            counter = 0
                            for row in cur:
                                if counter:
                                    w(line_sep)
                                w(head if (not insert_new_syntax or not counter) else " (")
                                w(",".join(sql_literal(v) for v in row))
                                counter += 1
                                done += 1
                                if progress is not None and counter % 1000 == 0 and not progress(done, total):
                                    raise _Cancelled()
                            if counter:
                                w(");\n")
                        finally:
                            conn.text_factory = str
                if export_schema:
                    def write_objects(kind: str, objects) -> None:
                        for obj in objects:
                            base = obj.base_table()
                            if base and base not in tables_to_dump:
                                continue
                            if not obj.original_sql:
                                continue
                            if not keep_old_schema:
                                w(f"DROP {kind.upper()} IF EXISTS {sqlb.escape_identifier(obj.name)};\n")
                            if obj.fully_parsed and not keep_original:
                                w(obj.sql("main", keep_old_schema) + "\n")
                            else:
                                w(obj.original_sql + ";\n")
                    write_objects("view", [t for t in objmap.tables.values() if t.is_view()])
                    write_objects("index", objmap.indices.values())
                    write_objects("trigger", objmap.triggers.values())
                w("COMMIT;\n")
        except _Cancelled:
            try:
                os.remove(filename)
            except OSError:
                pass
            return False
        except (OSError, sqlite3.Error) as e:
            self.last_error = str(e)
            return False
        if progress is not None:
            progress(total, total)
        return True


class _Cancelled(Exception):
    pass


def sql_literal(v: Any) -> str:
    """SQL literal used in dumps/logs. Keeps values byte-exact (upstream writes X'..' for blobs)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v:
            return "NULL"
        if v in (float("inf"), float("-inf")):
            return "1e999" if v > 0 else "-1e999"
        return repr(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "X'" + bytes(v).hex().upper() + "'"
    s = str(v)
    if "\x00" in s:
        return "CAST(X'" + s.encode("utf-8").hex().upper() + "' AS TEXT)"
    return sqlb.escape_string(s)


def _expand_sql(sql: str, params) -> str:
    """Rough equivalent of ``sqlite3_expanded_sql`` for the SQL log."""
    it = iter(params)
    out = []
    for part in re.split(r"(\?)", sql):
        if part == "?":
            v = next(it, None)
            out.append(sql_literal(v) if not (isinstance(v, str) and len(v) > 200) else sql_literal(v[:200] + "…"))
        else:
            out.append(part)
    return "".join(out)


def _quote_uri_path(p: str) -> str:
    from urllib.parse import quote
    p = p.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", p):
        p = "/" + p
    return quote(p, safe="/:")
