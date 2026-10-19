"""The MCP server: registers the tools, resources and prompts, and runs on stdio.

    sqliteview-mcp --db shop.db                # read and edit shop.db
    sqliteview-mcp --db shop.db --read-only    # no edit or import tools at all
    sqliteview-mcp --root D:/data              # no database yet; the assistant opens one inside D:/data

MCP clients (Claude Desktop, Claude Code, Cursor, VS Code …) start this program themselves and
exchange JSON-RPC messages over stdin/stdout, so nothing else may be written to stdout.
"""
import argparse
import functools
import json
import logging
import os
import sqlite3
import sys
from typing import Annotated, Any, Callable, Literal, Optional, TypeVar, Union

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .. import APP_DISPLAY_NAME, __version__
from . import editing, reading, schema, transfer
from .models import ColumnSpec, RowFilter, SortKey
from .session import (DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT, QUERY_TIMEOUT_SECONDS, DatabaseSession, Sandbox,
                      ToolInputError, describe_sqlite_error)

log = logging.getLogger("sqliteview.mcp")

INSTRUCTIONS = f"""\
{APP_DISPLAY_NAME} gives you a SQLite database.
1. Start with get_schema (all CREATE statements) or list_tables / describe_table.
2. Read with filter_rows (one table: conditions, search, sort, paging, no SQL needed) or run_query (one read-only
   SQL statement: joins, GROUP BY, aggregates). Results are paged: at most {MAX_ROW_LIMIT} rows per call, use
   next_offset for more. explain_query checks a statement without running it.
3. Changes (insert_rows, update_rows, delete_rows, create_table, execute_sql, import_*) are staged, not saved:
   call commit_changes once the user is happy, or revert_changes to undo everything since the last commit.
   Use dry_run=true to preview updates, deletes and imports, and confirm destructive changes with the user.
4. Rows are addressed by the _rowid_ column that filter_rows returns.
5. Files (imports, exports, other databases) must be inside the folders list_database_files reports.
"""

READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
ADDS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
CHANGES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

F = TypeVar("F", bound=Callable[..., Any])

Table = Annotated[str, Field(description="Table or view name (see list_tables)")]
Limit = Annotated[int, Field(description=f"Rows per page (1–{MAX_ROW_LIMIT})")]
Offset = Annotated[int, Field(description="Rows to skip (next_offset of the previous page)")]
DryRun = Annotated[bool, Field(description="Only report what would happen; change nothing")]
Filters = Annotated[Optional[list[RowFilter]], Field(description="Conditions, combined with AND")]
RowIds = Annotated[Optional[list[Union[int, str]]], Field(description="_rowid_ values from filter_rows")]
AllRows = Annotated[bool, Field(description="Apply to every row; only when the user explicitly means all rows")]
Conflict = Annotated[Literal["abort", "ignore", "replace"],
                     Field(description="On a UNIQUE/PRIMARY KEY clash: abort the call, ignore the row or replace "
                                       "the existing row")]
IfExists = Annotated[Literal["fail", "append", "replace"],
                     Field(description="If the table exists: fail, append the rows, or drop and recreate it")]
Path = Annotated[str, Field(description="File path, absolute or relative to the first allowed folder")]
Overwrite = Annotated[bool, Field(description="Replace the file if it exists")]


def build_server(session: DatabaseSession, log_level: str = "WARNING") -> FastMCP:
    mcp = FastMCP(APP_DISPLAY_NAME, instructions=INSTRUCTIONS, log_level=log_level)

    def tool(annotations: ToolAnnotations, *, writes: bool = False) -> Callable[[F], F]:
        """Register a tool (write tools only when the server isn't read-only) and turn engine errors
        into messages the assistant can act on."""
        def register(fn: F) -> F:
            @functools.wraps(fn)
            def run(*args: Any, **kwargs: Any) -> Any:
                try:
                    return fn(*args, **kwargs)
                except ToolInputError as e:
                    raise ToolError(str(e)) from e
                except sqlite3.Error as e:
                    raise ToolError(describe_sqlite_error(e, session.query_timeout)) from e
            if not (writes and session.read_only):
                mcp.tool(annotations=annotations)(run)
            return fn
        return register

    # ------------------------------------------------------------------ database & schema
    @tool(ADDS)
    def open_database(path: Path, create: Annotated[bool, Field(description="Create the file if missing")] = False
                      ) -> dict[str, Any]:
        """Open (or create) a SQLite database file. The previous one must have no uncommitted changes."""
        session.open(path, create=create)
        return schema.database_info(session)

    @tool(READ)
    def list_database_files() -> dict[str, Any]:
        """List the database files (.db, .sqlite, .sqlite3, .db3) in the folders this server may use."""
        return schema.list_database_files(session)

    @tool(READ)
    def database_info() -> dict[str, Any]:
        """Which database is open, whether it is read-only, and how many changes are uncommitted."""
        return schema.database_info(session)

    @tool(READ)
    def list_tables(include_row_counts: bool = True) -> dict[str, Any]:
        """List tables and views with their columns (and row counts)."""
        return schema.list_tables(session, include_row_counts)

    @tool(READ)
    def describe_table(table: Table, sample_rows: Annotated[int, Field(description="Example rows (0–20)")] = 3
                       ) -> dict[str, Any]:
        """Columns (type, NOT NULL, default, primary key), foreign keys, indices, triggers, row count, the CREATE
        statement and a few example rows of one table or view."""
        return schema.describe_table(session, table, sample_rows)

    @tool(READ)
    def get_schema() -> dict[str, Any]:
        """The CREATE statements of every table, view, index and trigger: the quickest way to learn the schema
        before writing SQL."""
        return {"database": session.path, "sql": schema.schema_sql(session)}

    # ------------------------------------------------------------------ reading
    @tool(READ)
    def run_query(sql: Annotated[str, Field(description="One read-only statement (SELECT / WITH / VALUES / "
                                                        "EXPLAIN / PRAGMA without a value)")],
                  params: Annotated[Optional[Union[list[Any], dict[str, Any]]],
                                    Field(description="Values for ? (a list) or :name (an object) placeholders")] = None,
                  limit: Limit = DEFAULT_ROW_LIMIT, offset: Offset = 0) -> dict[str, Any]:
        """Run one read-only SQL query and return a page of its rows. Use placeholders for values."""
        return reading.run_query(session, sql, params, limit, offset)

    @tool(READ)
    def explain_query(sql: Annotated[str, Field(description="One SQL statement of any kind")]) -> dict[str, Any]:
        """Check one statement without running it: reports errors, whether it only reads, and SQLite's query
        plan (e.g. whether an index is used)."""
        return reading.explain_query(session, sql)

    @tool(READ)
    def filter_rows(table: Table, filters: Filters = None,
                    search: Annotated[str, Field(description="Words to find in any column")] = "",
                    sort: Annotated[Optional[list[SortKey]], Field(description="Sort order, first key first")] = None,
                    columns: Annotated[Optional[list[str]], Field(description="Columns to return (default all)")] = None,
                    limit: Limit = DEFAULT_ROW_LIMIT, offset: Offset = 0,
                    include_rowid: Annotated[bool, Field(description="Add the _rowid_ column used by "
                                                                     "update_rows / delete_rows")] = True
                    ) -> dict[str, Any]:
        """Find, filter, sort and page the rows of one table without writing SQL. Also returns the total number
        of matching rows and the equivalent SQL."""
        return reading.filter_rows(session, table, filters, search, sort, columns, limit, offset, include_rowid)

    # ------------------------------------------------------------------ editing
    @tool(ADDS, writes=True)
    def insert_rows(table: Table,
                    rows: Annotated[list[dict[str, Any]], Field(description='Rows as objects: [{"name": "Asha", '
                                                                            '"city": "Delhi"}]; '
                                                                            'binary values as {"hex": "…"}')],
                    on_conflict: Conflict = "abort") -> dict[str, Any]:
        """Add rows to a table (staged until commit_changes). Columns left out get their default."""
        return editing.insert_rows(session, table, rows, on_conflict)

    @tool(CHANGES, writes=True)
    def update_rows(table: Table,
                    values: Annotated[dict[str, Any], Field(description="Column -> new value")],
                    filters: Filters = None, rowids: RowIds = None, all_rows: AllRows = False,
                    dry_run: DryRun = False) -> dict[str, Any]:
        """Change values in the rows chosen by filters, rowids or all_rows (staged until commit_changes)."""
        return editing.update_rows(session, table, values, filters, rowids, all_rows, dry_run)

    @tool(CHANGES, writes=True)
    def delete_rows(table: Table, filters: Filters = None, rowids: RowIds = None, all_rows: AllRows = False,
                    dry_run: DryRun = False) -> dict[str, Any]:
        """Delete the rows chosen by filters, rowids or all_rows (staged until commit_changes)."""
        return editing.delete_rows(session, table, filters, rowids, all_rows, dry_run)

    @tool(ADDS, writes=True)
    def create_table(name: Annotated[str, Field(description="New table name (lower_case_with_underscores)")],
                     columns: list[ColumnSpec], if_not_exists: bool = False,
                     without_rowid: Annotated[bool, Field(description="WITHOUT ROWID table (needs a primary key)")]
                     = False) -> dict[str, Any]:
        """Create a table from a list of columns (staged until commit_changes)."""
        return editing.create_table(session, name, columns, if_not_exists, without_rowid)

    @tool(CHANGES, writes=True)
    def execute_sql(sql: Annotated[str, Field(description="One or more statements separated by ;")],
                    dry_run: DryRun = False) -> dict[str, Any]:
        """Run SQL that changes data or schema (INSERT, UPDATE, DELETE, CREATE, ALTER, DROP …) as one change: if
        a statement fails, the whole script is rolled back. Staged until commit_changes. Not allowed: ATTACH,
        DETACH, VACUUM, PRAGMA changes, BEGIN/COMMIT/ROLLBACK/SAVEPOINT."""
        return editing.execute_sql(session, sql, dry_run)

    @tool(READ)
    def pending_changes() -> dict[str, Any]:
        """List the changes made since the last commit."""
        return editing.pending_changes(session)

    @tool(CHANGES, writes=True)
    def commit_changes() -> dict[str, Any]:
        """Save all staged changes to the database file (like Write Changes in SQliteView). Ask the user first."""
        return editing.commit_changes(session)

    @tool(CHANGES, writes=True)
    def revert_changes() -> dict[str, Any]:
        """Undo every change made since the last commit (like Revert Changes in SQliteView)."""
        return editing.revert_changes(session)

    # ------------------------------------------------------------------ import / export
    @tool(ADDS, writes=True)
    def import_csv(path: Path,
                   table: Annotated[Optional[str], Field(description="Target table (default: the file name)")] = None,
                   separator: str = ",", quote: Annotated[str, Field(description='Quote character, "" for none')] = '"',
                   encoding: str = "UTF-8", header: Annotated[bool, Field(description="First line has the column "
                                                                                    "names")] = True,
                   if_exists: IfExists = "fail", on_conflict: Conflict = "abort",
                   dry_run: DryRun = False) -> dict[str, Any]:
        """Import a CSV file into a new or existing table; column types are detected from the data. Use
        dry_run=true first to see the detected columns and the first rows."""
        return transfer.import_csv(session, path, table, separator, quote, encoding, header, if_exists,
                                   on_conflict, dry_run)

    @tool(ADDS, writes=True)
    def import_json(path: Path,
                    table: Annotated[Optional[str], Field(description="Target table (default: the file name)")] = None,
                    if_exists: IfExists = "fail", on_conflict: Conflict = "abort",
                    dry_run: DryRun = False) -> dict[str, Any]:
        """Import a JSON file holding an array of objects (one per row) into a new or existing table."""
        return transfer.import_json(session, path, table, if_exists, on_conflict, dry_run)

    @tool(CHANGES, writes=True)
    def import_sql(path: Path, dry_run: DryRun = False) -> dict[str, Any]:
        """Run an .sql file (e.g. a dump) as one change, rolled back completely if a statement fails."""
        return transfer.import_sql(session, path, dry_run)

    @tool(ADDS)
    def export_data(path: Path, table: Annotated[Optional[str], Field(description="Table or view to export")] = None,
                    sql: Annotated[Optional[str], Field(description="Or: one read-only query to export")] = None,
                    filters: Filters = None,
                    format: Annotated[Optional[Literal["csv", "tsv", "json", "sql", "md"]],
                                      Field(description="Default: from the file extension")] = None,
                    header: bool = True, overwrite: Overwrite = False) -> dict[str, Any]:
        """Write a table (optionally filtered) or a query result to a CSV, TSV, JSON, SQL INSERT or Markdown file."""
        return transfer.export_data(session, path, table, sql, filters, format, header, overwrite)

    @tool(ADDS)
    def export_sql_dump(path: Path,
                        tables: Annotated[Optional[list[str]], Field(description="Tables (default: all)")] = None,
                        schema_sql: Annotated[bool, Field(description="Include CREATE statements")] = True,
                        data: Annotated[bool, Field(description="Include INSERT statements")] = True,
                        overwrite: Overwrite = False) -> dict[str, Any]:
        """Write the database (or some tables) as an SQL script that recreates it."""
        return transfer.export_sql_dump(session, path, tables, schema_sql, data, overwrite)

    # ------------------------------------------------------------------ resources & prompts
    @mcp.resource("sqlite://schema", name="schema", mime_type="text/plain",
                  description="CREATE statements of the open database")
    def schema_resource() -> str:
        return schema.schema_sql(session)

    @mcp.resource("sqlite://tables/{name}", name="table", mime_type="application/json",
                  description="Columns, keys, indices and example rows of one table")
    def table_resource(name: str) -> str:
        return json.dumps(schema.describe_table(session, name), ensure_ascii=False, default=str)

    @mcp.prompt(description="Get an overview of the open database")
    def explore_database() -> str:
        return ("Use get_schema and list_tables to look at this database. Then describe in plain words what each "
                "table holds, how the tables relate to each other, and suggest three useful questions to ask.")

    @mcp.prompt(description="Answer a question from the data, showing the SQL used")
    def answer_question(question: str) -> str:
        return (f"Answer this question from the open database: {question}\n"
                "Read the schema first (get_schema), then use filter_rows or run_query. Don't change any data. "
                "Give the answer in one or two sentences, then the SQL you used.")

    return mcp


# --------------------------------------------------------------------------- command line

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="sqliteview-mcp",
                                description=f"{APP_DISPLAY_NAME} MCP server for Claude and other AI assistants "
                                            "(speaks MCP over stdin/stdout)")
    p.add_argument("--db", metavar="FILE", help="database to open at start")
    p.add_argument("--create", action="store_true", help="create the --db file if it doesn't exist")
    p.add_argument("-R", "--read-only", action="store_true", help="open read-only and offer no edit/import tools")
    p.add_argument("--root", action="append", default=[], metavar="FOLDER",
                   help="folder the server may read and write files in (repeatable; default: the folder of --db, "
                        "or the current folder)")
    p.add_argument("--query-timeout", type=float, default=QUERY_TIMEOUT_SECONDS, metavar="SECONDS",
                   help=f"stop statements that run longer (default {QUERY_TIMEOUT_SECONDS:g})")
    p.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="messages written to stderr (default WARNING)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def allowed_roots(db: Optional[str], roots: list[str]) -> list[str]:
    """The --root folders plus the folder of --db (the current folder when neither is given)."""
    result = list(roots)
    if db:
        result.append(os.path.dirname(os.path.abspath(db)))
    return result or [os.getcwd()]


def _reserve_stdout_for_protocol() -> None:
    """Only MCP messages may reach stdout. The protocol gets a private copy of the real stdout, and file
    descriptor 1 is pointed at stderr so output from native code (Qt, SQLite extensions) can't corrupt it."""
    try:
        out_fd, err_fd = sys.stdout.fileno(), sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        return  # no real stdio handles to protect
    sys.stdout.flush()
    protocol_fd = os.dup(out_fd)
    os.dup2(err_fd, out_fd)
    # FastMCP's stdio transport writes to sys.stdout.buffer
    sys.stdout = os.fdopen(protocol_fd, "w", encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(stream=sys.stderr, level=args.log_level, format="sqliteview-mcp %(levelname)s: %(message)s")
    _reserve_stdout_for_protocol()

    from PySide6.QtCore import QCoreApplication
    from ..core.settings import APP_NAME, ORG_NAME
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    app.setOrganizationName(ORG_NAME)
    app.setApplicationName(APP_NAME)

    session = DatabaseSession(Sandbox(allowed_roots(args.db, args.root)), read_only=args.read_only,
                              query_timeout=args.query_timeout)
    if args.db:
        try:
            session.open(args.db, create=args.create)
        except ToolInputError as e:
            log.error("%s", e)
            return 2
    server = build_server(session, args.log_level)
    try:
        server.run("stdio")
    finally:
        session.close()
    return 0
