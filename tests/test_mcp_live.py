"""Live mode (sqliteview/mcp/live.py): the window's database served over HTTP on 127.0.0.1 with a token,
and the stdio bridge that MCP clients start with ``sqliteview-mcp --live``."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_client_server_memory_streams
from PySide6.QtCore import QCoreApplication

from sqliteview.core import sqlb
from sqliteview.core.db import DBBrowserDB
from sqliteview.core.settings import Settings
from sqliteview.mcp.live import NOT_RUNNING, LiveServer, bridge, live_token, live_url

APP_DIR = os.path.join(os.path.dirname(__file__), "..")
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("live-template") / "demo.db"
    subprocess.run([sys.executable, os.path.join(APP_DIR, "scripts", "make_demo_db.py"), str(path)], check=True,
                   capture_output=True)
    return str(path)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pump(cond, timeout=5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def app_db(tmp_path, demo_template):
    """The window's engine: a DBBrowserDB opened like the app does (extensions and startup SQL applied)."""
    QCoreApplication.instance() or QCoreApplication([])
    shutil.copy(demo_template, tmp_path / "demo.db")
    db = DBBrowserDB()
    assert db.open(str(tmp_path / "demo.db"))
    yield db
    db.close(force=True)


@pytest.fixture
def live(app_db):
    Settings.set("mcp", "live_port", free_port())
    server = LiveServer(app_db)
    changes: list[str] = []
    server.changed.connect(changes.append)
    server.start()
    yield server, changes
    server.stop()


async def connect_http(url: str, token: str):
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return http, streamable_http_client(url, http_client=http, terminate_on_close=False)


async def tool(client: ClientSession, name: str, /, **args) -> dict:
    r = await client.call_tool(name, args)
    assert not r.isError, r.content[0].text
    return r.structuredContent


async def test_live_tools_work_on_the_window_database(live, app_db):
    server, changes = live
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {live_token()}"}, timeout=30) as http, \
            streamable_http_client(live_url(), http_client=http, terminate_on_close=False) as (r, w, _), \
            ClientSession(r, w) as client:
        init = await client.initialize()
        assert "open in SQliteView right now" in init.instructions
        names = {t.name for t in (await client.list_tools()).tools}
        assert "filter_rows" in names and "insert_rows" in names
        assert not names & {"open_database", "list_database_files", "commit_changes", "revert_changes"}
        assert (await tool(client, "run_query", sql="SELECT COUNT(*) FROM customers"))["rows"] == [[500]]
        r = await tool(client, "insert_rows", table="customers", rows=[{"name": "Live"}])
        assert r["inserted"] == 1 and "Write Changes" in r["note"] and "commit_changes" not in r["note"]
        insert_doc = next(t for t in (await client.list_tools()).tools if t.name == "insert_rows").description
        assert "Write Changes" in insert_doc and "commit_changes" not in insert_doc
        assert app_db.dirty  # the window's Write Changes button turns amber
        assert pump(lambda: changes == ["insert 1 row(s) into customers"])
        # The user presses Write Changes in the app; the assistant sees nothing pending afterwards
        assert app_db.release_all_savepoints()
        assert (await tool(client, "pending_changes"))["changes"] == []
        # The app's DDL is picked up by the assistant
        assert app_db.execute_sql("CREATE TABLE made_in_app(a);")
        assert "made_in_app" in (await tool(client, "get_schema"))["sql"]
        # Files only next to the open database
        r = await client.call_tool("export_data", {"path": "../outside.csv", "table": "customers"})
        assert r.isError and "outside the folders" in r.content[0].text
        assert (await tool(client, "export_data", path="customers.csv", table="customers"))["rows"] == 501
    assert app_db.query_single_value("SELECT COUNT(*) FROM customers WHERE name = 'Live'") == 1


async def test_live_server_needs_the_token(live):
    async with httpx.AsyncClient(timeout=10) as http:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        accept = {"Accept": "application/json, text/event-stream"}
        assert (await http.post(live_url(), json=body, headers=accept)).status_code == 401
        wrong = {**accept, "Authorization": "Bearer nope"}
        assert (await http.post(live_url(), json=body, headers=wrong)).status_code == 401
        evil_host = {**accept, "Authorization": f"Bearer {live_token()}", "Host": "evil.example:80"}
        assert (await http.post(live_url(), json=body, headers=evil_host)).status_code in (400, 421)


async def test_bridge_relays_to_the_live_server(live, app_db):
    async with create_client_server_memory_streams() as (client_streams, bridge_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(bridge, *bridge_streams, lambda: (live_url(), live_token()))
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                r = await tool(client, "describe_table", table="orders", sample_rows=0)
                assert r["rows"] == 20000
                await tool(client, "delete_rows", table="orders", rowids=[1])
            tg.cancel_scope.cancel()
    assert app_db.dirty and app_db.query_single_value("SELECT COUNT(*) FROM orders") == 19999
    app_db.revert_all()  # the user presses Revert Changes
    assert app_db.query_single_value("SELECT COUNT(*) FROM orders") == 20000


@pytest.mark.parametrize("token", ["", "some-token"])
async def test_bridge_explains_when_the_app_is_not_running(token):
    url = f"http://127.0.0.1:{free_port()}/mcp"  # nothing listens here
    async with create_client_server_memory_streams() as (client_streams, bridge_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(bridge, *bridge_streams, lambda: (url, token))
            async with ClientSession(*client_streams) as client:
                with pytest.raises(McpError, match="Allow AI Assistants"):
                    with anyio.fail_after(30):
                        await client.initialize()
            tg.cancel_scope.cancel()
    assert "Allow AI Assistants" in NOT_RUNNING


def test_port_in_use_and_restart(app_db):
    port = free_port()
    Settings.set("mcp", "live_port", port)
    first = LiveServer(app_db)
    first.start()
    try:
        with pytest.raises(OSError):
            LiveServer(app_db).start()
    finally:
        first.stop()
    assert not first.running
    again = LiveServer(app_db)
    again.start()  # the port is free again after stop()
    again.stop()


async def test_live_mode_refuses_extension_functions(live, app_db):
    # An extension loaded in the app (simulated: a function SQLite itself doesn't have) stays out of reach
    with app_db.exclusive("test") as conn:
        conn.create_function("readfile", 1, lambda p: "secret")
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {live_token()}"}, timeout=30) as http, \
            streamable_http_client(live_url(), http_client=http, terminate_on_close=False) as (r, w, _), \
            ClientSession(r, w) as client:
        await client.initialize()
        res = await client.call_tool("run_query", {"sql": "SELECT readfile('C:/secret.txt')"})
        assert res.isError and "not authorized" in res.content[0].text and "functions" in res.content[0].text
        # Also not through a view (the assistant could create one itself)
        assert app_db.execute_sql("CREATE VIEW leak AS SELECT readfile('C:/secret.txt') AS x;")
        res = await client.call_tool("run_query", {"sql": "SELECT * FROM leak"})
        assert res.isError and "not authorized" in res.content[0].text
        assert (await tool(client, "run_query", sql="SELECT json_extract('{\"a\": 2}', '$.a'), "
                                                    "upper('x'), 'ab' REGEXP 'b'"))["rows"] == [[2, "X", 1]]
    assert app_db.get_table_by_name(sqlb.ObjectIdentifier("main", "customers")) is not None


def run_bridge_process(requests: list[dict]) -> list[dict]:
    """``python -m sqliteview.mcp --live`` sharing the tests' settings file, like a client would start it."""
    import json
    Settings.sync()
    env = dict(os.environ, PYTHONPATH=os.path.abspath(APP_DIR), SQLITEVIEW_SETTINGS=Settings._store().fileName())
    proc = subprocess.run([sys.executable, "-m", "sqliteview.mcp", "--live"], env=env, timeout=60,
                          input="".join(json.dumps(r) + "\n" for r in requests).encode(), capture_output=True)
    return [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]


HANDSHAKE = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "1"}}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"}]


def test_live_command_line_bridge(live):
    replies = run_bridge_process(HANDSHAKE + [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "run_query", "arguments": {"sql": "SELECT COUNT(*) FROM customers"}}}])
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"]["structuredContent"]["rows"] == [[500]]


def test_live_command_line_bridge_without_the_app():
    Settings.set("mcp", "live_port", free_port())  # nothing listens there
    replies = run_bridge_process(HANDSHAKE)
    assert replies[0]["id"] == 1 and "Allow AI Assistants" in replies[0]["error"]["message"]


# ---------------------------------------------------------------------------- live-mode review regressions

def test_ai_change_is_one_unit_against_write_and_revert(app_db, tmp_path):
    """The user's Write/Revert Changes (another thread here) waits until an AI change has finished."""
    import threading
    from sqliteview.mcp.session import DatabaseSession, Sandbox
    session = DatabaseSession(Sandbox([str(tmp_path)]), db=app_db)
    inside, go = threading.Event(), threading.Event()

    def ai_change(dry_run: bool) -> None:
        with session.staging("insert", dry_run=dry_run) as db:
            with db.get("test") as conn:
                conn.execute("INSERT INTO customers(name) VALUES ('ai')")
            # between two steps of the change (an import, say), no statement holds the connection
            inside.set()
            go.wait(5)

    def on_disk() -> int:
        import sqlite3
        con = sqlite3.connect(app_db.cur_file)
        try:
            return con.execute("SELECT COUNT(*) FROM customers WHERE name = 'ai'").fetchone()[0]
        finally:
            con.close()

    for dry_run, user_action in ((True, app_db.release_all_savepoints), (False, app_db.revert_all)):
        inside.clear()
        go.clear()
        ai = threading.Thread(target=ai_change, args=(dry_run,))
        ai.start()
        assert inside.wait(5)
        user = threading.Thread(target=user_action)
        user.start()
        user.join(0.3)
        assert user.is_alive()  # waits for the AI's change instead of landing in the middle of it
        go.set()
        ai.join(5)
        user.join(5)
        assert not ai.is_alive() and not user.is_alive()
        assert on_disk() == 0 and not app_db.dirty
        assert session.staged == [] or not dry_run


def test_stop_cuts_a_running_read_short_and_restarts(live, app_db):
    import threading
    from sqliteview.mcp.session import CLOSING_MESSAGE
    server, _changes = live
    slow = "WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r) SELECT COUNT(*) FROM r"
    replies = []

    def ask() -> None:
        replies.append(httpx.post(live_url(), timeout=60, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "run_query", "arguments": {"sql": slow}}},
            headers={"Authorization": f"Bearer {live_token()}", "Accept": "application/json, text/event-stream"}))

    request = threading.Thread(target=ask)
    request.start()
    time.sleep(0.5)  # the query is running
    started = time.monotonic()
    server.stop()
    assert time.monotonic() - started < 3 and not server.running
    request.join(10)
    assert replies and CLOSING_MESSAGE in replies[0].json()["result"]["content"][0]["text"]
    server.start()  # the same object starts again right away
    assert server.running


async def test_bridge_reconnects_when_the_app_comes_back(app_db):
    Settings.set("mcp", "live_port", free_port())
    server = LiveServer(app_db)
    async with create_client_server_memory_streams() as (client_streams, bridge_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(bridge, *bridge_streams, lambda: (live_url(), live_token()))
            async with ClientSession(*client_streams) as client:
                with pytest.raises(McpError, match="Allow AI Assistants"):
                    with anyio.fail_after(30):
                        await client.initialize()  # the app isn't serving yet
                server.start()
                try:
                    r = await tool(client, "run_query", sql="SELECT COUNT(*) FROM products")
                    assert r["rows"] == [[40]]
                    server.stop()
                    with pytest.raises(McpError, match="Allow AI Assistants"):
                        await client.call_tool("run_query", {"sql": "SELECT 1"})  # refused while off
                    server.start()
                    assert (await tool(client, "run_query", sql="SELECT 2"))["rows"] == [[2]]
                finally:
                    server.stop()
            tg.cancel_scope.cancel()
