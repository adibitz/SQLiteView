"""How to start this installation's MCP server, and ready-to-paste configuration for MCP clients.

Used by the app's Help › Connect AI Assistant dialog, so it must not import the MCP SDK (the
window app runs without it when installed from source without the ``mcp`` extra).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional

SERVER_KEY = "sqliteview"
MCP_PROGRAM = "sqliteview-mcp"


def server_command() -> list[str]:
    """The command line that starts the MCP server of this installation."""
    if getattr(sys, "frozen", False):
        appimage = os.environ.get("APPIMAGE")
        if appimage:  # inside an AppImage sys.executable lives in a temporary mount
            return [appimage, "mcp"]
        exe = MCP_PROGRAM + (".exe" if sys.platform == "win32" else "")
        return [os.path.join(os.path.dirname(sys.executable), exe)]
    scripts = os.path.dirname(sys.executable)
    for folder in (scripts, os.path.join(scripts, "Scripts"), os.path.join(scripts, "bin")):
        found = shutil.which(MCP_PROGRAM, path=folder)
        if found:
            return [found]
    found = shutil.which(MCP_PROGRAM)
    return [found] if found else [sys.executable, "-m", "sqliteview.mcp"]


def server_available() -> bool:
    """Whether the MCP server can run (packaged builds always include it)."""
    return bool(getattr(sys, "frozen", False)) or importlib.util.find_spec("mcp") is not None


def server_args(database: Optional[str] = None, read_only: bool = False) -> list[str]:
    args = ["--db", database] if database else []
    return args + (["--read-only"] if read_only else [])


def _shell_join(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)


def _mcp_servers_json(command: list[str], args: list[str], key: str = "mcpServers", **extra: str) -> str:
    entry = {**extra, "command": command[0], "args": command[1:] + args}
    return json.dumps({key: {SERVER_KEY: entry}}, indent=2)


def claude_desktop_config_path() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "Claude", "claude_desktop_config.json")


@dataclass(frozen=True)
class ClientSetup:
    name: str
    where: str  # what to do with the snippet
    snippet: Callable[[list[str], list[str]], str]


CLIENTS: list[ClientSetup] = [
    ClientSetup("Claude Desktop",
                f"Add this to {claude_desktop_config_path()} (Settings › Developer › Edit Config), merging it with "
                "any mcpServers already there, then restart Claude Desktop.",
                lambda cmd, args: _mcp_servers_json(cmd, args)),
    ClientSetup("Claude Code",
                "Run this in a terminal (add --scope user to use it in every project).",
                lambda cmd, args: _shell_join(["claude", "mcp", "add", SERVER_KEY, "--"] + cmd + args)),
    ClientSetup("Cursor",
                "Add this to ~/.cursor/mcp.json (or .cursor/mcp.json in a project).",
                lambda cmd, args: _mcp_servers_json(cmd, args)),
    ClientSetup("VS Code",
                "Add this to .vscode/mcp.json in your workspace (or run MCP: Add Server).",
                lambda cmd, args: _mcp_servers_json(cmd, args, key="servers", type="stdio")),
]
