"""MCP server (sqliteview/mcp): every tool through a real MCP client session, the SQL guards,
the folder sandbox, staging/commit semantics, and the stdio program itself."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import asynccontextmanager

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from PySide6.QtCore import QCoreApplication

from sqliteview.mcp.server import allowed_roots, build_server
from sqliteview.mcp.session import DatabaseSession, Sandbox, ToolInputError, cell_value, param_value

APP_DIR = os.path.join(os.path.dirname(__file__), "..")
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory):
    QCoreApplication.instance() or QCoreApplication([])
    path = tmp_path_factory.mktemp("template") / "demo.db"
    subprocess.run([sys.executable, os.path.join(APP_DIR, "scripts", "make_demo_db.py"), str(path)], check=True,
                   capture_output=True)
    return str(path)


@pytest.fixture
def workdir(tmp_path, demo_template):
    shutil.copy(demo_template, tmp_path / "demo.db")
    return tmp_path


@pytest.fixture
def session(workdir):
    s = DatabaseSession(Sandbox([str(workdir)]))
    s.open(str(workdir / "demo.db"))
    yield s
    s.close()


@asynccontextmanager
async def connect(session: DatabaseSession):
    async with create_connected_server_and_client_session(build_server(session)) as client:
        yield client


async def call(client, tool: str, /, **args) -> dict:
    r = await client.call_tool(tool, args)
    assert not r.isError, r.content[0].text
    return r.structuredContent


async def fails(client, tool: str, /, **args) -> str:
    r = await client.call_tool(tool, args)
    assert r.isError, r.structuredContent
    return r.content[0].text


def file_rows(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------------------- tool list

async def test_tools_and_read_only_mode(workdir):
    s = DatabaseSession(Sandbox([str(workdir)]), read_only=True)
    s.open(str(workdir / "demo.db"))
    async with connect(s) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert "run_query" in tools and "insert_rows" not in tools and "commit_changes" not in tools
        assert tools["run_query"].annotations.readOnlyHint is True
        assert "filters" in json.dumps(tools["filter_rows"].inputSchema)
        assert "read-only" in await fails(c, "open_database", path=str(workdir / "new.db"), create=True)
    s.close()
    w = DatabaseSession(Sandbox([str(workdir)]))
    async with connect(w) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert tools["delete_rows"].annotations.destructiveHint is True
        assert "open_database" in await fails(c, "list_tables")  # nothing open yet


# ---------------------------------------------------------------------------- schema

async def test_schema_tools_and_resources(session):
    async with connect(session) as c:
        tables = {t["name"]: t for t in (await call(c, "list_tables"))["tables"]}
        assert tables["customers"]["rows"] == 500 and tables["order_summary"]["type"] == "view"
        assert not any(n.startswith("sqlite_") for n in tables)
        d = await call(c, "describe_table", table="orders", sample_rows=2)
        cols = {col["name"]: col for col in d["columns"]}
        assert d["primary_key"] == ["id"] and cols["customer_id"]["not_null"] and "generated" in cols["total"]
        assert d["foreign_keys"][0]["references_table"] in ("customers", "products")
        assert {i["name"] for i in d["indices"]} == {"idx_orders_customer", "idx_orders_date"}
        assert d["triggers"] == ["trg_stock"] and len(d["sample_rows"]) == 2 and d["rows"] == 20000
        assert (await call(c, "describe_table", table="products"))["row_address"].startswith("primary key")
        assert "Did you mean customers" in await fails(c, "describe_table", table="customer")
        sql = (await call(c, "get_schema"))["sql"]
        assert 'CREATE TABLE customers' in sql and "CREATE VIEW order_summary" in sql and "CREATE TRIGGER" in sql
        res = await c.read_resource("sqlite://schema")
        assert "CREATE INDEX idx_orders_date" in res.contents[0].text
        res = await c.read_resource("sqlite://tables/customers")
        assert json.loads(res.contents[0].text)["name"] == "customers"
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        assert prompts == {"explore_database", "answer_question"}


# ---------------------------------------------------------------------------- reading

async def test_run_query_paging_params_and_values(session):
    async with connect(session) as c:
        r = await call(c, "run_query", sql="SELECT id, name FROM customers WHERE country = ? ORDER BY id",
                       params=["IN"], limit=5)
        assert r["columns"] == ["id", "name"] and r["returned"] == 5 and r["truncated"] and r["next_offset"] == 5
        r2 = await call(c, "run_query", sql="SELECT id FROM customers WHERE country = :c ORDER BY id",
                        params={"c": "IN"}, limit=5, offset=5)
        assert r2["rows"][0][0] > r["rows"][-1][0]
        r = await call(c, "run_query", sql="SELECT sku, image FROM products WHERE image IS NOT NULL LIMIT 1")
        assert r["rows"][0][1]["blob_bytes"] > 0 and r["rows"][0][1]["hex_preview"].startswith("89504e47")
        r = await call(c, "run_query", sql="WITH t(x) AS (VALUES (1), (2)) SELECT SUM(x) AS s FROM t")
        assert r["rows"] == [[3]] and not r["truncated"]
        assert (await call(c, "run_query", sql="PRAGMA table_info(customers)"))["returned"] == 6
        assert (await call(c, "run_query", sql="SELECT * FROM pragma_index_list('orders')"))["returned"] == 2
        assert (await call(c, "run_query", sql="PRAGMA foreign_keys"))["rows"] == [[1]]
        assert "exactly one" in await fails(c, "run_query", sql="SELECT 1; SELECT 2")
        assert "limit" in await fails(c, "run_query", sql="SELECT 1", limit=0)


@pytest.mark.parametrize("sql", [
    "DELETE FROM customers",
    "UPDATE customers SET name = 'x'",
    "WITH x AS (SELECT 1) DELETE FROM customers",
    "CREATE TABLE t(a)",
    "ATTACH 'other.db' AS o",
    "PRAGMA foreign_keys = OFF",
])
async def test_run_query_rejects_writes(session, sql):
    async with connect(session) as c:
        assert "reading statements" in await fails(c, "run_query", sql=sql)


@pytest.mark.parametrize("sql", [
    "PRAGMA writable_schema(1)",
    "PRAGMA user_version(5)",
    "SELECT load_extension('evil')",
])
async def test_read_authorizer_blocks_disguised_writes(session, sql):
    async with connect(session) as c:
        assert "not authorized" in await fails(c, "run_query", sql=sql)
    assert file_rows(session.path, "PRAGMA user_version") == [(0,)]


async def test_query_timeout(workdir):
    s = DatabaseSession(Sandbox([str(workdir)]), query_timeout=0.2)
    s.open(str(workdir / "demo.db"))
    async with connect(s) as c:
        msg = await fails(c, "run_query", sql="WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r) "
                                              "SELECT COUNT(*) FROM r")
        assert "stopped after 0.2 seconds" in msg
        assert (await call(c, "run_query", sql="SELECT 1"))["rows"] == [[1]]  # the connection still works
    s.close()


async def test_explain_query(session):
    async with connect(session) as c:
        r = await call(c, "explain_query", sql="SELECT * FROM orders WHERE customer_id = 5")
        assert r["valid"] and r["reads_only"] and "idx_orders_customer" in " ".join(p["detail"] for p in r["plan"])
        r = await call(c, "explain_query", sql="DELETE FROM orders WHERE id = 1")
        assert r["valid"] and not r["reads_only"]
        r = await call(c, "explain_query", sql="SELECT nope FROM orders")
        assert not r["valid"] and "nope" in r["error"]
    assert file_rows(session.path, "SELECT COUNT(*) FROM orders") == [(20000,)]


async def test_filter_rows(session):
    async with connect(session) as c:
        r = await call(c, "filter_rows", table="customers",
                       filters=[{"column": "country", "op": "equals", "value": "IN"},
                                {"column": "created_at", "op": "ge", "value": "2024-06-01"},
                                {"column": "created_at", "op": "lt", "value": "2024-07-01"}],
                       sort=[{"column": "created_at", "descending": True}], columns=["name", "created_at"], limit=500)
        expected = file_rows(session.path, "SELECT COUNT(*) FROM customers WHERE country = 'IN' AND created_at >= "
                                           "'2024-06-01' AND created_at < '2024-07-01'")[0][0]
        assert r["total_matching"] == expected == r["returned"] and r["columns"] == ["_rowid_", "name", "created_at"]
        dates = [row[2] for row in r["rows"]]
        assert dates == sorted(dates, reverse=True)
        assert '"created_at" >= \'2024-06-01\' AND "created_at" < \'2024-07-01\'' in r["sql"]
        r = await call(c, "filter_rows", table="customers", filters=[{"column": "meta", "op": "not_null"}],
                       search="vip", limit=1)
        assert r["total_matching"] == len(range(7, 501, 7))
        r = await call(c, "filter_rows", table="products", filters=[{"column": "price", "op": "between",
                                                                     "value": 10, "value2": 50}])
        assert all(10 <= row[3] <= 50 for row in r["rows"])
        assert isinstance(r["rows"][0][0], str)  # WITHOUT ROWID: the primary key addresses the row
        r = await call(c, "filter_rows", table="order_summary", limit=1)
        assert "_rowid_" not in r["columns"] and r["total_matching"] == 500
        assert "needs two numbers" in await fails(c, "filter_rows", table="products", filters=[
            {"column": "sku", "op": "between", "value": "a", "value2": "b"}])
        assert "can't start with" in await fails(c, "filter_rows", table="customers", filters=[
            {"column": "name", "op": "contains", "value": "=x"}])
        assert "needs a value" in await fails(c, "filter_rows", table="customers", filters=[
            {"column": "name", "op": "starts"}])
        assert "Columns: id, name" in await fails(c, "filter_rows", table="customers", sort=[{"column": "nme"}])


# ---------------------------------------------------------------------------- editing

async def test_insert_stage_revert_and_commit(session):
    async with connect(session) as c:
        r = await call(c, "insert_rows", table="customers", rows=[{"name": "Asha", "email": "asha@x.in"},
                                                                  {"name": "Ben", "country": "GB"},
                                                                  {"name": "Cy", "country": "US"}])
        assert r["inserted"] == 3 and r["last_rowid"] == 503 and "commit_changes" in r["note"]
        assert len((await call(c, "pending_changes"))["changes"]) == 1
        assert file_rows(session.path, "SELECT COUNT(*) FROM customers") == [(500,)]  # not on disk yet
        found = await call(c, "filter_rows", table="customers", filters=[{"column": "name", "op": "equals",
                                                                          "value": "Asha"}])
        assert found["rows"][0][4] == "IN"  # column default applied
        assert (await call(c, "revert_changes"))["reverted"]
        assert (await call(c, "filter_rows", table="customers", search="Asha"))["total_matching"] == 0
        await call(c, "insert_rows", table="customers", rows=[{"name": "Dev", "email": "user1@example.com"}],
                   on_conflict="ignore")
        assert "UNIQUE" in await fails(c, "insert_rows", table="customers",
                                       rows=[{"name": "Dev", "email": "user1@example.com"}])
        await call(c, "insert_rows", table="customers", rows=[{"name": "Esha", "meta": {"hex": "00ff"}}])
        assert (await call(c, "commit_changes"))["committed"]
        assert not (await call(c, "pending_changes"))["uncommitted"]
    assert file_rows(session.path, "SELECT hex(meta) FROM customers WHERE name = 'Esha'") == [("00FF",)]
    async with connect(session) as c:
        assert "view" in await fails(c, "insert_rows", table="order_summary", rows=[{"customer": "x"}])
        assert "no column" in await fails(c, "insert_rows", table="customers", rows=[{"nmae": "x"}])
        assert "Unsupported value" in await fails(c, "insert_rows", table="customers", rows=[{"name": [1]}])


async def test_update_and_delete(session):
    async with connect(session) as c:
        de = [{"column": "country", "op": "equals", "value": "DE"}]
        count = file_rows(session.path, "SELECT COUNT(*) FROM customers WHERE country = 'DE'")[0][0]
        r = await call(c, "update_rows", table="customers", values={"country": "AT"}, filters=de, dry_run=True)
        assert r["would_update"] == count and len(r["sample_before"]["rows"]) == 5
        assert not (await call(c, "pending_changes"))["uncommitted"]
        r = await call(c, "update_rows", table="customers", values={"country": "AT"}, filters=de)
        assert r["updated"] == count
        assert (await call(c, "filter_rows", table="customers", filters=de))["total_matching"] == 0
        r = await call(c, "update_rows", table="products", values={"stock": 0}, rowids=["SKU-0001", "SKU-0002"])
        assert r["updated"] == 2
        assert "exactly one of" in await fails(c, "delete_rows", table="orders")
        assert "exactly one of" in await fails(c, "delete_rows", table="orders", rowids=[1], all_rows=True)
        r = await call(c, "delete_rows", table="orders", rowids=[1, 2, 3], dry_run=True)
        assert r["would_delete"] == 3
        assert (await call(c, "delete_rows", table="orders", rowids=[1, 2, 3]))["deleted"] == 3
        assert (await call(c, "delete_rows", table="orders", all_rows=True))["deleted"] == 19997
        await call(c, "commit_changes")
    assert file_rows(session.path, "SELECT COUNT(*) FROM orders") == [(0,)]
    assert file_rows(session.path, "SELECT stock FROM products WHERE sku = 'SKU-0001'") == [(0,)]


async def test_create_table(session):
    async with connect(session) as c:
        r = await call(c, "create_table", name="visits", columns=[
            {"name": "id", "type": "INTEGER", "primary_key": True, "autoincrement": True},
            {"name": "customer_id", "type": "INTEGER", "not_null": True, "references": "customers(id)"},
            {"name": "note", "default": "'none'", "check": "length(note) < 50"},
        ])
        assert "AUTOINCREMENT" in r["sql"] and 'REFERENCES "customers"("id")' in r["sql"]
        await call(c, "insert_rows", table="visits", rows=[{"customer_id": 1}])
        assert (await call(c, "filter_rows", table="visits"))["rows"][0][3] == "none"
        assert "FOREIGN KEY" in await fails(c, "insert_rows", table="visits", rows=[{"customer_id": 9999}])
        assert "already exists" in await fails(c, "create_table", name="visits", columns=[{"name": "a"}])
        assert "autoincrement" in await fails(c, "create_table", name="bad", columns=[
            {"name": "a", "type": "TEXT", "primary_key": True, "autoincrement": True}])
        assert "visits" in [t["name"] for t in (await call(c, "list_tables", include_row_counts=False))["tables"]]
        await call(c, "revert_changes")
        assert "visits" not in [t["name"] for t in (await call(c, "list_tables"))["tables"]]


async def test_execute_sql(session):
    async with connect(session) as c:
        r = await call(c, "execute_sql", sql="CREATE TABLE tags(name TEXT UNIQUE); INSERT INTO tags VALUES ('a'), "
                                             "('b'); UPDATE customers SET meta = 'x' WHERE id <= 10;")
        assert r["statements"] == 3 and r["rows_changed"] == 12 and r["schema_changed"]
        r = await call(c, "execute_sql", sql="INSERT INTO tags VALUES ('c') RETURNING name, rowid")
        assert r["returned"] == {"columns": ["name", "rowid"], "rows": [["c", 3]]}
        msg = await fails(c, "execute_sql", sql="INSERT INTO tags VALUES ('d'); INSERT INTO tags VALUES ('a');")
        assert "Statement 2 of 2 failed" in msg
        assert (await call(c, "run_query", sql="SELECT COUNT(*) FROM tags"))["rows"] == [[3]]  # 'd' rolled back
        r = await call(c, "execute_sql", sql="DELETE FROM orders", dry_run=True)
        assert r["rows_changed"] == 20000 and r["dry_run"]
        assert (await call(c, "run_query", sql="SELECT COUNT(*) FROM orders"))["rows"] == [[20000]]
        assert len((await call(c, "pending_changes"))["changes"]) == 2
        for sql in ("BEGIN; DELETE FROM tags; COMMIT;", "PRAGMA foreign_keys = OFF", "SAVEPOINT x",
                    "RELEASE RESTOREPOINT", "SELECT load_extension('evil')"):
            assert "not authorized" in await fails(c, "execute_sql", sql=sql), sql
        assert "ATTACH isn't available" in await fails(c, "execute_sql", sql="ATTACH 'x.db' AS x")
        assert "VACUUM isn't available" in await fails(c, "execute_sql", sql="VACUUM INTO 'copy.db'")
        assert (await call(c, "run_query", sql="SELECT COUNT(*) FROM tags"))["rows"] == [[3]]
        await call(c, "commit_changes")
    assert file_rows(session.path, "SELECT name FROM tags ORDER BY name") == [("a",), ("b",), ("c",)]


async def test_close_discards_uncommitted(workdir):
    s = DatabaseSession(Sandbox([str(workdir)]))
    s.open(str(workdir / "demo.db"))
    async with connect(s) as c:
        await call(c, "delete_rows", table="orders", all_rows=True)
    s.close()
    assert file_rows(str(workdir / "demo.db"), "SELECT COUNT(*) FROM orders") == [(20000,)]


# ---------------------------------------------------------------------------- import / export

async def test_csv_import_and_export(session, workdir):
    csv_path = workdir / "visits.csv"
    csv_path.write_bytes(b"city;count;when\nPune;3;2025-01-01\nDelhi;12;2025-01-02\n")
    async with connect(session) as c:
        r = await call(c, "import_csv", path="visits.csv", separator=";", dry_run=True)
        assert r["action"] == "create" and r["columns"][1] == {"name": "count", "type": "INTEGER"}
        assert r["preview"][0] == ["Pune", "3", "2025-01-01"]
        assert (await call(c, "import_csv", path=str(csv_path), separator=";"))["rows_read"] == 2
        assert "already exists" in await fails(c, "import_csv", path="visits.csv", separator=";")
        await call(c, "import_csv", path="visits.csv", separator=";", if_exists="append")
        assert (await call(c, "run_query", sql="SELECT SUM(count) FROM visits"))["rows"] == [[30]]
        r = await call(c, "import_csv", path="visits.csv", separator=";", table="visits", if_exists="replace")
        assert r["action"] == "replace"
        assert (await call(c, "run_query", sql="SELECT COUNT(*) FROM visits"))["rows"] == [[2]]
        assert "same number of columns" in await fails(c, "import_csv", path="visits.csv", table="customers",
                                                       separator=";", if_exists="append")

        r = await call(c, "export_data", path="in.csv", table="customers",
                       filters=[{"column": "country", "op": "equals", "value": "IN"}])
        lines = (workdir / "in.csv").read_text(encoding="utf-8").splitlines()
        assert r["rows"] == len(lines) - 1 and lines[0].startswith('"id","name"')
        assert "already exists" in await fails(c, "export_data", path="in.csv", table="customers")
        r = await call(c, "export_data", path="top.json",
                       sql="SELECT customer, items FROM order_summary ORDER BY items DESC LIMIT 3")
        assert r["format"] == "json" and len(json.loads((workdir / "top.json").read_text(encoding="utf-8"))) == 3
        r = await call(c, "export_data", path="in.csv", table="visits", format="md", overwrite=True)
        assert (workdir / "in.csv").read_text(encoding="utf-8").startswith("| city |")
        assert "reading statements" in await fails(c, "export_data", path="x.csv", sql="DELETE FROM customers")


async def test_json_import(session, workdir):
    (workdir / "people.json").write_text(json.dumps([{"name": "a", "age": 3, "tags": ["x"]},
                                                     {"name": "b", "score": 1.5}]), encoding="utf-8")
    async with connect(session) as c:
        r = await call(c, "import_json", path="people.json", dry_run=True)
        assert [col["type"] for col in r["columns"]] == ["TEXT", "INTEGER", "TEXT", "REAL"]
        r = await call(c, "import_json", path="people.json")
        assert r["inserted"] == 2
        rows = (await call(c, "run_query", sql="SELECT name, age, tags, score FROM people ORDER BY name"))["rows"]
        assert rows == [["a", 3, '["x"]', None], ["b", None, None, 1.5]]
        (workdir / "bad.json").write_text('{"not": "a list"}', encoding="utf-8")
        assert "array of objects" in await fails(c, "import_json", path="bad.json")


async def test_sql_dump_round_trip(session, workdir):
    async with connect(session) as c:
        r = await call(c, "export_sql_dump", path="dump.sql", tables=["customers", "orders", "products"])
        assert r["tables"] == ["customers", "orders", "products"]
        await call(c, "open_database", path="copy.db", create=True)
        r = await call(c, "import_sql", path="dump.sql", dry_run=True)
        assert r["dry_run"] and not (await call(c, "list_tables"))["tables"]
        r = await call(c, "import_sql", path="dump.sql")
        assert r["rows_changed"] >= 20540
        await call(c, "commit_changes")
    assert file_rows(str(workdir / "copy.db"), "SELECT COUNT(*) FROM orders") == [(20000,)]


# ---------------------------------------------------------------------------- files and databases

async def test_sandbox_and_open_database(session, workdir, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "x.csv").write_text("a\n1\n", encoding="utf-8")
    async with connect(session) as c:
        for path in (str(outside / "x.csv"), "../" + outside.name + "/x.csv"):
            assert "outside the folders" in await fails(c, "import_csv", path=path)
        assert "outside the folders" in await fails(c, "export_data", path=str(outside / "y.csv"), table="customers")
        assert "outside the folders" in await fails(c, "open_database", path=str(outside / "z.db"), create=True)
        assert "File not found" in await fails(c, "open_database", path="missing.db")
        files = await call(c, "list_database_files")
        assert files["files"] == [str(workdir / "demo.db")] and files["open"] == str(workdir / "demo.db")
        await call(c, "insert_rows", table="customers", rows=[{"name": "x"}])
        assert "uncommitted changes" in await fails(c, "open_database", path="other.db", create=True)
        await call(c, "revert_changes")
        assert "extensions" in await fails(c, "open_database", path="notes.txt", create=True)
        info = await call(c, "open_database", path="fresh.db", create=True)
        assert info["database"] == str(workdir / "fresh.db") and not info["read_only"]
        assert (await call(c, "list_tables"))["tables"] == []
    assert not os.path.exists(outside / "y.csv")


def test_value_helpers_and_roots(tmp_path):
    assert cell_value(float("nan")) == "nan" and cell_value("x" * 5000).endswith("[5000 characters in total]")
    assert param_value(True) == 1 and param_value({"hex": "0a"}) == b"\n"
    with pytest.raises(ToolInputError):
        param_value({"hex": "zz"})
    with pytest.raises(ToolInputError):
        Sandbox([str(tmp_path)]).resolve("   ")
    db = str(tmp_path / "a" / "x.db")
    assert allowed_roots(db, []) == [str(tmp_path / "a")]
    assert allowed_roots(None, []) == [os.getcwd()]
    assert allowed_roots(db, ["D:/data"]) == ["D:/data", str(tmp_path / "a")]


# ---------------------------------------------------------------------------- the stdio program

def test_stdio_program_speaks_only_mcp_on_stdout(workdir):
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "run_query", "arguments": {"sql": "SELECT COUNT(*) AS n FROM customers"}}},
    ]
    env = dict(os.environ, PYTHONPATH=os.path.abspath(APP_DIR))
    proc = subprocess.run([sys.executable, "-m", "sqliteview.mcp", "--db", str(workdir / "demo.db"),
                           "--log-level", "INFO"],
                          input="".join(json.dumps(m) + "\n" for m in messages).encode(), capture_output=True,
                          timeout=60, env=env, cwd=str(workdir))
    lines = [ln for ln in proc.stdout.decode("utf-8").splitlines() if ln.strip()]
    replies = [json.loads(ln) for ln in lines]  # every stdout line must be a JSON-RPC message
    assert [r["id"] for r in replies] == [1, 2, 3], proc.stderr.decode(errors="replace")
    assert replies[0]["result"]["serverInfo"]["name"] == "SQliteView"
    assert len(replies[1]["result"]["tools"]) == 22
    assert replies[2]["result"]["structuredContent"]["rows"] == [[500]]
    assert b"opened" in proc.stderr  # logging goes to stderr


def test_stdio_program_bad_database(workdir):
    env = dict(os.environ, PYTHONPATH=os.path.abspath(APP_DIR))
    proc = subprocess.run([sys.executable, "-m", "sqliteview.mcp", "--db", str(workdir / "missing.db")],
                          capture_output=True, timeout=60, env=env, input=b"")
    assert proc.returncode == 2 and b"File not found" in proc.stderr and proc.stdout == b""


# ---------------------------------------------------------------------------- client configuration

def test_client_config_snippets(tmp_path):
    from sqliteview.mcp.clientconfig import CLIENTS, server_args, server_available, server_command
    cmd = server_command()
    assert cmd and server_available()
    args = server_args(str(tmp_path / "my shop.db"), read_only=True)
    assert args == ["--db", str(tmp_path / "my shop.db"), "--read-only"] and server_args() == []
    by_name = {c.name: c for c in CLIENTS}
    desktop = json.loads(by_name["Claude Desktop"].snippet(cmd, args))["mcpServers"]["sqliteview"]
    assert desktop == {"command": cmd[0], "args": cmd[1:] + args}
    vscode = json.loads(by_name["VS Code"].snippet(cmd, args))["servers"]["sqliteview"]
    assert vscode["type"] == "stdio" and vscode["args"][-1] == "--read-only"
    claude_code = by_name["Claude Code"].snippet(["C:/Program Files/SQliteView/sqliteview-mcp.exe"], args)
    assert claude_code.startswith("claude mcp add sqliteview -- ") and "my shop.db" in claude_code
    assert "claude_desktop_config.json" in by_name["Claude Desktop"].where


# ---------------------------------------------------------------------------- review regressions

async def test_write_that_loses_the_transaction_is_reported(workdir):
    """An interrupted write makes SQLite roll back the whole transaction, not just its savepoint."""
    s = DatabaseSession(Sandbox([str(workdir)]), query_timeout=0.2)
    s.open(str(workdir / "demo.db"))
    slow = ("UPDATE customers SET meta = (WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r "
            "LIMIT 2000000) SELECT COUNT(*) FROM r)")
    async with connect(s) as c:
        await call(c, "insert_rows", table="customers", rows=[{"name": "Kept?"}])
        msg = await fails(c, "execute_sql", sql=slow)
        assert "lost" in msg and "insert 1 row(s) into customers" in msg
        assert (await call(c, "pending_changes")) == {"database": s.path, "uncommitted": False, "changes": []}
        await call(c, "insert_rows", table="customers", rows=[{"name": "After"}])
        assert (await call(c, "commit_changes"))["committed"]
    s.close()
    assert file_rows(str(workdir / "demo.db"), "SELECT name FROM customers WHERE id > 500") == [("After",)]


async def test_counts_ignore_trigger_and_cascade_rows(session):
    async with connect(session) as c:
        r = await call(c, "insert_rows", table="orders", rows=[{"customer_id": 1, "sku": "SKU-0001"},
                                                               {"customer_id": 2, "sku": "SKU-0002"}])
        assert (r["inserted"], r["skipped"]) == (2, 0)  # trg_stock also updates products
        orders_of_1 = file_rows(session.path, "SELECT COUNT(*) FROM orders WHERE customer_id = 1")[0][0]
        assert orders_of_1 > 0
        assert (await call(c, "delete_rows", table="customers", rowids=[1]))["deleted"] == 1  # cascades to orders
        r = await call(c, "update_rows", table="orders", values={"quantity": 2},
                       filters=[{"column": "customer_id", "op": "equals", "value": 3}])
        assert r["updated"] == file_rows(session.path, "SELECT COUNT(*) FROM orders WHERE customer_id = 3")[0][0]


async def test_import_sqlite_cli_dump(session, workdir):
    (workdir / "cli.sql").write_text("PRAGMA foreign_keys=OFF;\nBEGIN IMMEDIATE TRANSACTION;\nCREATE TABLE z(a);\n"
                                     "INSERT INTO z VALUES(1);\ncommit;\n", encoding="utf-8")
    async with connect(session) as c:
        assert (await call(c, "import_sql", path="cli.sql"))["statements"] == 2
        assert (await call(c, "run_query", sql="SELECT a FROM z"))["rows"] == [[1]]


async def test_export_ignores_query_timeout(workdir):
    s = DatabaseSession(Sandbox([str(workdir)]), query_timeout=0.001)
    s.open(str(workdir / "demo.db"))
    async with connect(s) as c:
        assert (await call(c, "export_data", path="orders.json", table="orders"))["rows"] == 20000
    s.close()


async def test_failed_dump_keeps_existing_file(session, workdir, monkeypatch):
    from sqliteview.core.db import DBBrowserDB
    target = workdir / "keep.sql"
    target.write_text("original", encoding="utf-8")

    def broken_dump(self, filename, *args, **kwargs):
        with open(filename, "w", encoding="utf-8") as fh:
            fh.write("BEGIN TRANSACTION;\npartial")
        self.last_error = "disk full"
        return False
    monkeypatch.setattr(DBBrowserDB, "dump", broken_dump)
    async with connect(session) as c:
        assert "disk full" in await fails(c, "export_sql_dump", path="keep.sql", overwrite=True)
    assert target.read_text(encoding="utf-8") == "original"
    assert not [n for n in os.listdir(workdir) if n.endswith(".part")]


def test_relative_db_is_relative_to_current_folder(workdir):
    (workdir / "other").mkdir()
    env = dict(os.environ, PYTHONPATH=os.path.abspath(APP_DIR))
    proc = subprocess.run([sys.executable, "-m", "sqliteview.mcp", "--root", "other", "--db", "demo.db"],
                          capture_output=True, timeout=60, env=env, input=b"", cwd=str(workdir))
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")


# ---------------------------------------------------------------------------- security review regressions

async def test_exports_never_replace_databases(session, workdir):
    shutil.copy(workdir / "demo.db", workdir / "other.db")
    async with connect(session) as c:
        assert "open database" in await fails(c, "export_sql_dump", path="demo.db", overwrite=True)
        assert "open database" in await fails(c, "export_data", path="demo.db-journal", table="customers",
                                              overwrite=True)
        assert "never replace databases" in await fails(c, "export_data", path="other.db", sql="SELECT 1",
                                                        overwrite=True)
    for name in ("demo.db", "other.db"):
        assert file_rows(str(workdir / name), "SELECT COUNT(*) FROM customers") == [(500,)]


async def test_read_only_exports_only_create_files(workdir):
    (workdir / "notes.csv").write_text("keep", encoding="utf-8")
    s = DatabaseSession(Sandbox([str(workdir)]), read_only=True)
    s.open(str(workdir / "demo.db"))
    async with connect(s) as c:
        assert "read-only" in await fails(c, "export_data", path="notes.csv", table="customers", overwrite=True)
        assert (await call(c, "export_data", path="new.csv", table="customers"))["rows"] == 500
    s.close()
    assert (workdir / "notes.csv").read_text(encoding="utf-8") == "keep"


async def test_whole_transaction_rollback_never_commits_reverted_changes(session):
    async with connect(session) as c:
        await call(c, "execute_sql", sql="CREATE TABLE u(k UNIQUE ON CONFLICT ROLLBACK); CREATE TABLE log(x)")
        await call(c, "commit_changes")
        await call(c, "insert_rows", table="log", rows=[{"x": "A"}])
        msg = await fails(c, "execute_sql", sql="INSERT INTO u VALUES (1), (1)")
        assert "lost" in msg and "insert 1 row(s) into log" in msg
        assert not (await call(c, "pending_changes"))["uncommitted"]
        await call(c, "insert_rows", table="log", rows=[{"x": "C"}])
        assert (await call(c, "revert_changes"))["reverted"] == ["insert 1 row(s) into log"]
        await call(c, "insert_rows", table="log", rows=[{"x": "D"}])
        await call(c, "commit_changes")
    assert file_rows(session.path, "SELECT x FROM log") == [("D",)]


def test_open_ignores_extensions_and_startup_sql_from_settings(workdir, monkeypatch):
    from sqliteview.core.db import DBBrowserDB
    from sqliteview.core.settings import Settings
    loaded = []
    monkeypatch.setattr(DBBrowserDB, "load_extensions_from_settings", lambda self: loaded.append(self))
    Settings.set("db", "defaultsqltext", "CREATE TABLE injected(a);")
    try:
        s = DatabaseSession(Sandbox([str(workdir)]))
        s.open(str(workdir / "demo.db"))
        assert loaded == [] and s.db.get_table_by_name(sqlb_ident("injected")) is None
        s.close()
        app_db = DBBrowserDB()  # the window app still applies both
        assert app_db.open(str(workdir / "demo.db")) and loaded == [app_db]
        assert app_db.get_table_by_name(sqlb_ident("injected")) is not None
        app_db.close(force=True)
    finally:
        Settings.set("db", "defaultsqltext", "")


def sqlb_ident(name):
    from sqliteview.core import sqlb
    return sqlb.ObjectIdentifier("main", name)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows device names")
def test_windows_device_names_are_refused(tmp_path):
    for name in ("NUL", "con.csv", "CONIN$", "com1.txt", "sub/aux"):
        with pytest.raises(ToolInputError, match="device name"):
            Sandbox([str(tmp_path)]).resolve(name)
    assert Sandbox([str(tmp_path)]).resolve("console.csv").endswith("console.csv")
