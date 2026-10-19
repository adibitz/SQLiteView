"""Live mode: the SQliteView window serves the MCP tools for the database it has open.

    App:     LiveServer(window.db).start()   Tools › Allow AI Assistants
    Client:  sqliteview-mcp --live           a stdio bridge that MCP clients start; it finds the app's port
                                             and token in the app's settings, so no secret goes into their config

The server listens on 127.0.0.1 only and every request must carry the token. Tool calls run on the
server's thread and reach the window's DBBrowserDB through its locking, like the SQL editor's worker
does; each change holds the lock from start to end. Changes stay uncommitted until the user presses
Write Changes (or Revert Changes) in the app.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import socket
import threading
import time
from typing import Any, Callable, Optional

import anyio
import httpx
from anyio import EndOfStream
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import ErrorData, JSONRPCError, JSONRPCMessage, JSONRPCRequest
from PySide6.QtCore import QObject, Signal

from .. import APP_DISPLAY_NAME
from ..core.db import DBBrowserDB
from ..core.settings import Settings
from .server import build_server
from .session import DatabaseSession, Sandbox

log = logging.getLogger(__name__)

LIVE_HOST = "127.0.0.1"
LIVE_PATH = "/mcp"
START_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 5.0
DRAIN_TIMEOUT_SECONDS = 300.0  # how long the bridge waits for replies after the client closed its input
NOT_RUNNING = (f"{APP_DISPLAY_NAME} isn't reachable. Open it and turn on Tools › Allow AI Assistants, then "
               "try again.")
LIVE_ROOT_HINT = "In live mode files must be in the folder of the database open in SQliteView."
_JSONRPC_SERVER_ERROR = -32000


class LiveServerBusy(OSError):
    """The previous server is still finishing a request; it can't be started again yet."""


def live_token(create: bool = True) -> str:
    """The secret every live request must carry; made once and kept in the app's settings."""
    token = Settings.get("mcp", "live_token") or ""
    if not token and create:
        token = secrets.token_urlsafe(32)
        Settings.set("mcp", "live_token", token)
    return token


def live_url() -> str:
    return f"http://{LIVE_HOST}:{int(Settings.get('mcp', 'live_port'))}{LIVE_PATH}"


def current_endpoint() -> tuple[str, str]:
    """(URL, token) as the app last saved them; re-read every time, the app may have restarted."""
    Settings.reload()
    return live_url(), live_token(create=False)


class _RequireToken:
    """ASGI middleware: HTTP requests without ``Authorization: Bearer <token>`` get 401."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            given = dict(scope.get("headers") or ()).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"Missing or wrong token"})
                return
        await self.app(scope, receive, send)


class LiveServer(QObject):
    """Serves the MCP tools for ``db`` (the window's engine) while running."""

    # A change was staged (description). Emitted on the server thread; Qt queues it to receivers in the UI thread.
    changed = Signal(str)

    def __init__(self, db: DBBrowserDB, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.db = db
        self._server: Any = None
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[DatabaseSession] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _roots(self) -> list[str]:
        """Files may be read and written next to the open database."""
        if self.db.is_open() and self.db.cur_file != ":memory:":
            return [os.path.dirname(os.path.abspath(self.db.cur_file))]
        return []

    @staticmethod
    def _listen(port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: no other process may bind the same port
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:  # POSIX: allow a restart while old connections linger in TIME_WAIT (still one listener only)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((LIVE_HOST, port))
        except OSError:
            sock.close()
            raise
        return sock

    def start(self) -> str:
        """Start serving and return the URL. Raises OSError when the port is taken (e.g. by another window) and
        :class:`LiveServerBusy` while a request of the previous run is still finishing."""
        import uvicorn

        if self._thread is not None:
            if self._thread.is_alive():
                if self._session is not None and not self._session.closing:
                    return live_url()
                raise LiveServerBusy("The AI assistant is still finishing its last change; try again in a moment.")
            self._thread = None
        sock = self._listen(int(Settings.get("mcp", "live_port")))
        try:
            self._session = DatabaseSession(Sandbox(self._roots, hint=LIVE_ROOT_HINT), db=self.db,
                                            on_change=self.changed.emit)
            app = _RequireToken(build_server(self._session).streamable_http_app(), live_token())
            Settings.sync()  # sqliteview-mcp --live, another process, reads the port and token
            # log_config=None: a windowed app has no stderr for uvicorn's default handlers
            config = uvicorn.Config(app, log_config=None, log_level="warning", lifespan="on", ws="none",
                                    timeout_graceful_shutdown=2)
            self._server = uvicorn.Server(config)
        except BaseException:
            sock.close()
            raise
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]}, name="mcp-live",
                                        daemon=True)
        self._thread.start()
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise OSError(f"The AI assistant server didn't start on {live_url()}.")
            time.sleep(0.02)
        log.info("live MCP server on %s", live_url())
        return live_url()

    def stop(self) -> None:
        """Stop serving. A running read is cut short; a running change is allowed to finish (interrupting it
        would roll back the user's unsaved edits too), so the thread may outlive this call briefly."""
        if self._session is not None:
            self._session.closing = True
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(STOP_TIMEOUT_SECONDS)
            if self._thread.is_alive():
                log.warning("the AI assistant server is still finishing a request")
                return  # start() reports it until the thread has ended
        if self._session is not None:
            self._session.close()
        self._server = self._thread = self._session = None


# --------------------------------------------------------------------------- stdio bridge

def _error_reply(request_id: Any, message: str) -> SessionMessage:
    return SessionMessage(JSONRPCMessage(JSONRPCError(jsonrpc="2.0", id=request_id,
                                                      error=ErrorData(code=_JSONRPC_SERVER_ERROR, message=message))))


def _request_id(msg: Any) -> Any:
    if isinstance(msg, SessionMessage) and isinstance(msg.message.root, JSONRPCRequest):
        return msg.message.root.id
    return None


class _ConnectionLost(Exception):
    pass


async def bridge(client_read: Any, client_write: Any,
                 endpoint: Callable[[], tuple[str, str]] = current_endpoint) -> None:
    """Relay MCP messages between a client (stdio streams) and the app's live server until the client closes
    its input. The live server keeps no per-client state, so after a failure (app closed, live mode off) the
    next request simply connects again; requests that can't be delivered get an error that says what to do."""
    in_flight: set[Any] = set()
    pending: list[SessionMessage] = []  # the message that is waiting for a (re)connection
    client_done = False

    async def refuse(ids: set[Any]) -> None:
        for request_id in ids:
            await client_write.send(_error_reply(request_id, NOT_RUNNING))
        ids.clear()

    while not client_done:
        if not pending:  # wait for the client's next message before (re)connecting
            try:
                msg = await client_read.receive()
            except (EndOfStream, anyio.ClosedResourceError):
                return
            if isinstance(msg, Exception):
                log.warning("bad message from the client: %s", msg)
                continue
            pending.append(msg)
        url, token = endpoint()
        if not token:
            await refuse({rid for rid in map(_request_id, pending) if rid is not None})
            pending.clear()
            continue
        try:
            async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                         timeout=httpx.Timeout(30, read=300)) as http, \
                    streamable_http_client(url, http_client=http, terminate_on_close=False) as (server_read,
                                                                                                server_write, _):
                async with anyio.create_task_group() as tg:
                    async def to_server() -> None:
                        nonlocal client_done
                        while pending:
                            msg = pending.pop(0)
                            if _request_id(msg) is not None:
                                in_flight.add(_request_id(msg))
                            await server_write.send(msg)
                        async for msg in client_read:
                            if isinstance(msg, Exception):
                                log.warning("bad message from the client: %s", msg)
                                continue
                            if _request_id(msg) is not None:
                                in_flight.add(_request_id(msg))
                            await server_write.send(msg)
                        client_done = True
                        # The client closed its input: deliver the replies still on their way, then stop
                        with anyio.move_on_after(DRAIN_TIMEOUT_SECONDS):
                            while in_flight:
                                await anyio.sleep(0.02)
                        tg.cancel_scope.cancel()

                    async def to_client() -> None:
                        async for msg in server_read:
                            if isinstance(msg, Exception):
                                raise _ConnectionLost(msg)
                            in_flight.discard(getattr(msg.message.root, "id", None))
                            await client_write.send(msg)

                    tg.start_soon(to_server)
                    tg.start_soon(to_client)
        except Exception as e:  # noqa: BLE001 - connection errors arrive wrapped in task-group exception groups
            log.warning("live server unreachable: %r", e)
            await refuse(in_flight)
            await refuse({rid for rid in map(_request_id, pending) if rid is not None})
            pending.clear()


def run_bridge() -> int:
    """``sqliteview-mcp --live``: serve stdio and relay to the running app."""
    async def main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            async with write_stream:  # closing it lets the stdio transport finish
                await bridge(read_stream, write_stream)

    anyio.run(main)
    return 0
