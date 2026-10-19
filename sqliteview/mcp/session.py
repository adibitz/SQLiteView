"""State of one MCP server process: the open database, the folders it may touch, and the guards
that keep SQL sent by an AI assistant inside what the user allowed.

Changes follow the application's model: each one is wrapped in a SAVEPOINT and nothing reaches
the file until :meth:`DatabaseSession.commit` (the "Write Changes" button). SQL from the
assistant runs under an SQLite authorizer: read tools may only read, write tools may change
data and schema but can't attach files, change pragmas, end the transaction or load extensions.
"""
from __future__ import annotations

import difflib
import logging
import math
import os
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from functools import lru_cache
from typing import Any, Callable, Iterator, Optional, Union

from ..core import sqlb
from ..core.db import DBBrowserDB, _text_factory_fallback

log = logging.getLogger(__name__)

DEFAULT_ROW_LIMIT = 100
MAX_ROW_LIMIT = 1000
MAX_CELL_CHARS = 2000
BLOB_PREVIEW_BYTES = 32
QUERY_TIMEOUT_SECONDS = 30.0
DATABASE_EXTENSIONS = (".db", ".sqlite", ".sqlite3", ".db3")
PROGRESS_HANDLER_STEPS = 10_000
# Who holds the engine lock, as the app's "database is busy" prompt shows it
AI_READING = "AI assistant (reading)"
AI_WRITING = "AI assistant (writing)"
CLOSING_MESSAGE = "SQliteView stopped serving AI assistants."
ROOT_HINT = "Ask the user to add the folder with --root in the MCP server configuration."

# Pragmas that only report something, also when given an argument (e.g. table_info(t))
QUERY_PRAGMAS = frozenset({
    "table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo", "foreign_key_list",
    "foreign_key_check", "integrity_check", "quick_check", "database_list", "collation_list", "function_list",
    "module_list", "pragma_list", "compile_options",
})
# Setting pragmas that may be read (without an argument) but never set by the assistant
READABLE_PRAGMAS = frozenset({
    "foreign_keys", "user_version", "application_id", "schema_version", "encoding", "journal_mode", "page_size",
    "page_count", "freelist_count", "auto_vacuum", "cache_size", "busy_timeout", "data_version", "recursive_triggers",
    "case_sensitive_like", "synchronous", "temp_store", "max_page_count", "secure_delete",
})
BLOCKED_FUNCTIONS = frozenset({"load_extension"})
ENGINE_FUNCTIONS = frozenset({"regexp", "sqlb_make_single_value"})  # registered by DBBrowserDB
_SQLITE_RECURSIVE = getattr(sqlite3, "SQLITE_RECURSIVE", 33)
_READ_ACTIONS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, _SQLITE_RECURSIVE})
_NEVER_IN_WRITES = frozenset({sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_TRANSACTION,
                              getattr(sqlite3, "SQLITE_SAVEPOINT", 32)})


class ToolInputError(ValueError):
    """A problem the assistant can fix by changing its request (shown to it as the tool error)."""


# --------------------------------------------------------------------------- authorizers

@lru_cache(maxsize=1)
def builtin_functions() -> frozenset[str]:
    """SQL functions of SQLite itself plus the engine's own. Functions added by extensions (readfile, writefile,
    … which the app may have loaded from its settings) aren't in it. Empty when SQLite can't list them."""
    with closing(sqlite3.connect(":memory:")) as conn:
        try:
            names = {row[0].lower() for row in conn.execute("PRAGMA function_list")}
        except sqlite3.Error:
            log.warning("this SQLite can't list its functions; only load_extension() is blocked")
            return frozenset()
    return frozenset(names | ENGINE_FUNCTIONS) - BLOCKED_FUNCTIONS


@lru_cache(maxsize=1)
def builtin_modules() -> frozenset[str]:
    """Virtual-table modules of SQLite itself (json_each, pragma_* …)."""
    with closing(sqlite3.connect(":memory:")) as conn:
        try:
            return frozenset(row[0].lower() for row in conn.execute("PRAGMA module_list"))
        except sqlite3.Error:
            return frozenset()


def extension_modules(conn: sqlite3.Connection) -> frozenset[str]:
    """Virtual-table modules an extension added to ``conn`` (e.g. fsdir), which the assistant may not use."""
    try:
        return frozenset(row[0].lower() for row in conn.execute("PRAGMA module_list")) - builtin_modules()
    except sqlite3.Error:
        return frozenset()


def make_authorizer(writes: bool, foreign_modules: frozenset[str] = frozenset()) -> Callable[..., int]:
    """An SQLite authorizer for statements from the assistant. Read mode allows SELECT-type statements only.
    Write mode allows data and schema changes, but no ATTACH/DETACH, transaction control or pragma changes
    (those would escape the savepoint or the folder sandbox). Both refuse functions and virtual tables that
    don't come with SQLite, so loaded extensions can't reach the file system."""
    functions = builtin_functions()

    def authorize(action: int, arg1: Optional[str], arg2: Optional[str], _db: Optional[str],
                  _trigger: Optional[str]) -> int:
        if action == sqlite3.SQLITE_FUNCTION:
            name = (arg2 or "").lower()
            if name in BLOCKED_FUNCTIONS or (functions and name not in functions):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA:
            name = (arg1 or "").lower()
            if name in QUERY_PRAGMAS or (name in READABLE_PRAGMAS and arg2 is None):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if foreign_modules and (
                (action == sqlite3.SQLITE_READ and (arg1 or "").lower() in foreign_modules)
                or (action == sqlite3.SQLITE_CREATE_VTABLE and (arg2 or "").lower() in foreign_modules)):
            return sqlite3.SQLITE_DENY
        if writes:
            return sqlite3.SQLITE_DENY if action in _NEVER_IN_WRITES else sqlite3.SQLITE_OK
        return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY

    return authorize


def describe_sqlite_error(e: sqlite3.Error, timeout: float = QUERY_TIMEOUT_SECONDS) -> str:
    """A message for the assistant that says what to do next."""
    msg = str(e)
    low = msg.lower()
    if "not authorized" in low:
        return ("SQLite refused this statement (not authorized). Through this connection you can't ATTACH/DETACH "
                "databases, change PRAGMA settings, use BEGIN/COMMIT/ROLLBACK/SAVEPOINT, load extensions, or use "
                "functions and virtual tables that aren't part of SQLite, also not through a view or trigger that "
                "calls them (e.g. functions of an extension loaded in SQliteView). Read tools only accept reading "
                f"statements. SQLite said: {msg}")
    if "interrupted" in low:
        return f"The statement was stopped after {timeout:g} seconds. Narrow it down (WHERE, LIMIT) and try again."
    if "database is locked" in low or "database is busy" in low:
        return ("The database is locked by another program (often SQliteView itself with unwritten changes). "
                "Write or revert the changes there, then try again.")
    return f"SQLite error: {msg}"


# --------------------------------------------------------------------------- values

def cell_value(v: Any) -> Any:
    """A JSON-friendly form of an SQLite value (long text is shortened, BLOBs are summarised)."""
    if v is None or isinstance(v, (bool, int)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, str):
        return v if len(v) <= MAX_CELL_CHARS else v[:MAX_CELL_CHARS] + f"… [{len(v)} characters in total]"
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return {"blob_bytes": len(b), "hex_preview": b[:BLOB_PREVIEW_BYTES].hex()}
    return str(v)


def param_value(v: Any) -> Any:
    """An SQLite value from JSON input: scalars as they are, booleans as 0/1, {"hex": "…"} as a BLOB."""
    if v is None or isinstance(v, (str, float)):
        return v
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, dict) and set(v) == {"hex"} and isinstance(v["hex"], str):
        try:
            return bytes.fromhex(v["hex"])
        except ValueError:
            raise ToolInputError(f"Not a valid hex string: {v['hex'][:40]!r}") from None
    raise ToolInputError(f"Unsupported value {v!r}: use text, a number, true/false, null or {{\"hex\": \"…\"}} "
                         "for binary data. Store lists/objects as JSON text.")


def clamp_limit(limit: int) -> int:
    if limit < 1:
        raise ToolInputError("limit must be at least 1")
    return min(limit, MAX_ROW_LIMIT)


# --------------------------------------------------------------------------- sandbox

_WINDOWS_DEVICES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
                             | {f"{d}{i}" for d in ("COM", "LPT") for i in range(1, 10)})
SQLITE_HEADER = b"SQLite format 3\x00"


def _is_device_name(path: str) -> bool:
    return sys.platform == "win32" and os.path.basename(path).split(".")[0].rstrip(" ").upper() in _WINDOWS_DEVICES


def is_sqlite_file(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def _inside(path: str, root: str) -> bool:
    p, r = os.path.normcase(path), os.path.normcase(root)
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives on Windows
        return False


class Sandbox:
    """The folders the server may read and write files in (databases, CSV/JSON/SQL files)."""

    def __init__(self, roots: Union[list[str], Callable[[], list[str]]], hint: str = ROOT_HINT):
        """``roots``: the folders, or a function returning them (live mode follows the database the app has
        open). ``hint`` tells the assistant how to get access to other folders."""
        if not callable(roots) and not roots:
            raise ValueError("at least one root folder is needed")
        self._roots = roots
        self.hint = hint

    @property
    def roots(self) -> list[str]:
        roots = self._roots() if callable(self._roots) else self._roots
        if not roots:
            raise ToolInputError("No folder is available for files yet; open a database first.")
        return [os.path.realpath(os.path.abspath(r)) for r in roots]

    def resolve(self, path: str, *, must_exist: bool = False) -> str:
        """Absolute real path of ``path`` (relative paths start at the first root); raises
        :class:`ToolInputError` when it's outside every root."""
        if not path or not path.strip():
            raise ToolInputError("The path is empty.")
        if _is_device_name(path.strip()):
            raise ToolInputError(f"{path} is a Windows device name, not a file.")
        roots = self.roots
        expanded = os.path.expanduser(path.strip())
        if not os.path.isabs(expanded):
            expanded = os.path.join(roots[0], expanded)
        full = os.path.realpath(expanded)
        if not any(_inside(full, r) for r in roots):
            raise ToolInputError(f"{path} is outside the folders this server may use ({', '.join(roots)}). "
                                 + self.hint)
        if must_exist and not os.path.exists(full):
            raise ToolInputError(f"File not found: {full}")
        return full

    def writable_target(self, path: str, overwrite: bool) -> str:
        full = self.resolve(path)
        if os.path.isdir(full):
            raise ToolInputError(f"{full} is a folder; give a file name.")
        if os.path.exists(full) and not overwrite:
            raise ToolInputError(f"{full} already exists. Pass overwrite=true to replace it.")
        if not os.path.isdir(os.path.dirname(full)):
            raise ToolInputError(f"The folder {os.path.dirname(full)} doesn't exist.")
        return full


# --------------------------------------------------------------------------- session

class DatabaseSession:
    def __init__(self, sandbox: Sandbox, read_only: bool = False, query_timeout: float = QUERY_TIMEOUT_SECONDS,
                 db: Optional[DBBrowserDB] = None, on_change: Optional[Callable[[str], None]] = None):
        """``db``: an engine owned by someone else (live mode: the app window's); the session then never opens,
        closes or reverts it. ``on_change(description)`` is called after each staged change."""
        self.sandbox = sandbox
        self.read_only = read_only
        self.query_timeout = query_timeout
        self.owns_db = db is None
        self.db = DBBrowserDB() if db is None else db
        self.on_change = on_change
        self.closing = False  # set when the server stops: reads end at once, no new changes start
        self.staged: list[str] = []
        self._schema_version: Any = None

    # ------------------------------------------------------------------ open / close
    @property
    def path(self) -> str:
        return self.db.cur_file if self.db.is_open() else ""

    def open(self, path: str, create: bool = False) -> None:
        if not self.owns_db:
            raise ToolInputError("This server works on the database open in SQliteView; open another one there.")
        full = self.sandbox.resolve(path, must_exist=not create)
        if self.db.is_open() and self.db.dirty:
            raise ToolInputError(f"{self.path} has uncommitted changes. Call commit_changes or revert_changes first.")
        if create and self.read_only:
            raise ToolInputError("The server runs read-only; it can't create databases.")
        if create and not os.path.exists(full):
            if not full.lower().endswith(DATABASE_EXTENSIONS):
                raise ToolInputError(f"Use one of these extensions for a new database: {', '.join(DATABASE_EXTENSIONS)}")
            ok = self.db.create(full, load_extensions=False, run_default_sql=False)
        else:
            # The app's extension list and startup SQL aren't applied: extensions (fileio, sqlean …) would give
            # the assistant file access outside the sandbox.
            ok = self.db.open(full, self.read_only, load_extensions=False, run_default_sql=False)
        if not ok:
            raise ToolInputError(f"Couldn't open {full}: {self.db.last_error}")
        with self.db.exclusive("MCP setup") as conn:
            try:
                conn.enable_load_extension(False)
            except (AttributeError, sqlite3.Error):
                pass
        self.staged.clear()
        self._schema_version = self._read_schema_version()
        log.info("opened %s%s", full, " (read-only)" if self.db.read_only else "")

    def close(self) -> None:
        if not self.owns_db:  # the app keeps the database and decides about its uncommitted changes
            self.staged.clear()
            return
        if not self.db.is_open():
            return
        if self.db.dirty:
            log.warning("discarding %d uncommitted change(s) to %s", len(self.staged), self.path)
            self.db.revert_all()
        self.db.close(force=True)
        self.staged.clear()

    def _read_schema_version(self) -> Any:
        return self.db.query_single_value("PRAGMA schema_version;", log=False)

    def require_open(self) -> DBBrowserDB:
        """The open database, with the schema re-read if another program changed it."""
        if not self.db.is_open():
            if not self.owns_db:
                raise ToolInputError("No database is open in SQliteView. Ask the user to open one.")
            raise ToolInputError("No database is open. Call open_database first (list_database_files shows the "
                                 "files you can open).")
        lost = self._forget_lost_transaction()
        if lost:
            log.warning("SQLite rolled back the transaction; lost: %s", "; ".join(lost))
        if self.staged and not self.db.dirty:
            self.staged.clear()  # written or reverted in SQliteView (live mode)
        version = self._read_schema_version()
        if version != self._schema_version:
            self.db.update_schema()
            self._schema_version = version
        return self.db

    def require_writable(self) -> DBBrowserDB:
        db = self.require_open()
        if self.read_only or db.read_only:
            raise ToolInputError("The database is open read-only; changes aren't possible.")
        return db

    def schema_changed(self) -> None:
        """Call after DDL so the next schema check doesn't re-read twice."""
        self.db.update_schema()
        self._schema_version = self._read_schema_version()

    def export_target(self, path: str, overwrite: bool) -> str:
        """Where an export may write: inside the sandbox, never over a database (the open one, its journal files,
        or any other SQLite file), and in read-only mode never over an existing file."""
        if overwrite and self.read_only:
            raise ToolInputError("The server runs read-only: exports can create new files but not replace existing "
                                 "ones. Choose a new file name.")
        full = self.sandbox.writable_target(path, overwrite)
        if self.db.is_open():
            current = os.path.normcase(os.path.realpath(self.db.cur_file))
            if os.path.normcase(full) in {current + suffix for suffix in ("", "-journal", "-wal", "-shm")}:
                raise ToolInputError(f"{full} is the open database (or one of its journal files); export elsewhere.")
        if os.path.exists(full) and is_sqlite_file(full):
            raise ToolInputError(f"{full} is an SQLite database; exports never replace databases.")
        return full

    # ------------------------------------------------------------------ objects
    def table(self, name: str, *, writable: bool = False) -> tuple[sqlb.ObjectIdentifier, sqlb.Table]:
        db = self.require_open()
        ident = sqlb.ObjectIdentifier("main", name)
        t = db.get_table_by_name(ident)
        if t is None:
            known = sorted(db.schemata.get("main").tables) if db.schemata.get("main") else []
            close = difflib.get_close_matches(name, known, n=3)
            hint = f" Did you mean {', '.join(close)}?" if close else " Call list_tables to see the tables."
            raise ToolInputError(f"There is no table or view named {name!r}.{hint}")
        if writable and t.is_view():
            raise ToolInputError(f"{t.name!r} is a view; change the tables it reads from instead.")
        return sqlb.ObjectIdentifier("main", t.name), t

    @staticmethod
    def column(t: sqlb.Table, name: str) -> str:
        f = t.find_field(name)
        if f is None:
            raise ToolInputError(f"{t.name!r} has no column {name!r}. Columns: {', '.join(t.field_names())}")
        return f.name

    # ------------------------------------------------------------------ guarded access
    @contextmanager
    def _guarded(self, writes: bool, timeout: bool = True) -> Iterator[sqlite3.Connection]:
        with self.db.exclusive(AI_WRITING if writes else AI_READING) as conn:
            if conn is None:  # closed in the app (live mode) since the check
                raise ToolInputError("The database was closed.")
            conn.set_authorizer(make_authorizer(writes, extension_modules(conn)))
            deadline = time.monotonic() + self.query_timeout if timeout else math.inf
            # Reads also stop when the session closes; writes run to the end, since interrupting one makes SQLite
            # roll back the whole transaction (with any unsaved edits of the user in live mode)
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline or (self.closing and not writes)),
                                      PROGRESS_HANDLER_STEPS)
            conn.text_factory = _text_factory_fallback
            try:
                yield conn
            finally:
                conn.set_authorizer(None)
                conn.set_progress_handler(None, 0)
                conn.text_factory = str

    @contextmanager
    def reading(self, timeout: bool = True) -> Iterator[sqlite3.Connection]:
        """The connection, allowed to run reading statements only (``timeout=False`` for whole-table exports,
        which may legitimately take long)."""
        self.require_open()
        with self._guarded(False, timeout) as conn:
            yield conn

    @contextmanager
    def explaining(self) -> Iterator[sqlite3.Connection]:
        """The connection for EXPLAIN of any statement (EXPLAIN never runs it)."""
        self.require_open()
        with self._guarded(True) as conn:
            yield conn

    @contextmanager
    def staging(self, description: str, dry_run: bool = False) -> Iterator[DBBrowserDB]:
        """A change of the engine's own making in its own savepoint: kept (uncommitted) when the
        block succeeds, rolled back when it raises or when ``dry_run`` is set."""
        db = self.require_writable()
        if self.closing:
            raise ToolInputError(CLOSING_MESSAGE)
        # One lock for the whole change: Write/Revert Changes in the app (live mode) can't land in the middle of it
        with db.exclusive(AI_WRITING):
            savepoint = db.generate_savepoint_name("mcp")
            if not db.set_savepoint(savepoint):
                raise ToolInputError(f"Couldn't start the change: {db.last_error}")
            try:
                yield db
            except BaseException as e:
                lost = self._forget_lost_transaction()
                if lost is not None:
                    earlier = "; ".join(lost) if lost else "none"
                    also = "" if self.owns_db else ", including unsaved edits made in SQliteView"
                    raise ToolInputError(f"{e}\nSQLite rolled back the whole transaction, not just this change, so "
                                         f"all uncommitted changes were lost{also} (earlier changes: {earlier}). The "
                                         "database is as it was when changes were last written.") from e
                db.revert_to_savepoint(savepoint)
                self.schema_changed()  # the rolled-back statements may have been DDL
                raise
            if dry_run:
                db.revert_to_savepoint(savepoint)
                self.schema_changed()
            else:
                self.staged.append(description)
                if self.on_change is not None:
                    self.on_change(description)

    @contextmanager
    def writing(self, description: str, dry_run: bool = False) -> Iterator[sqlite3.Connection]:
        """Like :meth:`staging`, for SQL written by the assistant (runs under the write authorizer)."""
        with self.staging(description, dry_run), self._guarded(True) as conn:
            yield conn

    # ------------------------------------------------------------------ transaction
    def _in_transaction(self) -> bool:
        with self.db.exclusive(AI_READING) as conn:
            return conn is not None and conn.in_transaction

    def _forget_lost_transaction(self) -> Optional[list[str]]:
        """SQLite ends the whole transaction (not just a savepoint) when a write is interrupted, on ON CONFLICT
        ROLLBACK and on RAISE(ROLLBACK). Then the savepoints the engine tracks no longer exist: forget them and
        return the staged changes that were lost. None when the transaction is intact."""
        with self.db.exclusive(AI_WRITING):  # the app may write or revert at the same time (live mode)
            if not self.db.savepoints or self._in_transaction():
                return None
            lost = list(self.staged)
            self.db.savepoints.clear()
            self.staged.clear()
        self.db.dbChanged.emit(False)
        self.schema_changed()
        return lost

    def commit(self) -> list[str]:
        db = self.require_writable()
        done = list(self.staged)
        if db.dirty and not db.release_all_savepoints():
            raise ToolInputError(describe_sqlite_error(sqlite3.OperationalError(db.last_error)))
        self.staged.clear()
        return done

    def revert(self) -> list[str]:
        db = self.require_open()
        undone = list(self.staged)
        db.revert_all()
        if self._in_transaction():  # e.g. a transaction a failed ROLLBACK TO left behind
            db.execute_sql("ROLLBACK;", False, True)
        self.staged.clear()
        self.schema_changed()
        return undone
