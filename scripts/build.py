"""Build a distributable package of SQliteView for the current operating system.

    python scripts/build.py              # bundle with PyInstaller, then package
    python scripts/build.py --skip-bundle  # only re-package an existing dist/SQliteView build

Output in dist/:
  Windows  SQliteView-<ver>-windows-x64.zip  (+ ...-setup.exe when Inno Setup 6 / iscc is installed)
  macOS    SQliteView-<ver>-macos-<arch>.dmg  (+ .zip of the .app)
  Linux    SQliteView-<ver>-linux-<arch>.tar.gz  (+ .AppImage when appimagetool is on PATH or $APPIMAGETOOL)

PyInstaller can't cross-compile: run this on each target OS (CI does it, see .github/workflows/release.yml).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")
SPEC = os.path.join(ROOT, "packaging", "sqliteview.spec")
ICONS = os.path.join(ROOT, "sqliteview", "assets", "icons")
APP_NAME = "SQliteView"
MCP_NAME = "sqliteview-mcp"  # console MCP server bundled next to the app (see packaging/sqliteview.spec)


def version() -> str:
    with open(os.path.join(ROOT, "sqliteview", "__init__.py"), encoding="utf-8") as fh:
        return re.search(r'__version__ = "([^"]+)"', fh.read()).group(1)


def arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x64", "x86_64": "x86_64" if sys.platform.startswith("linux") else "x64",
            "arm64": "arm64", "aarch64": "aarch64"}.get(m, m)


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def bundle() -> None:
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", DIST, "--workpath", BUILD, SPEC])


def mcp_executable() -> str:
    if sys.platform == "darwin":
        return os.path.join(DIST, f"{APP_NAME}.app", "Contents", "MacOS", MCP_NAME)
    return os.path.join(DIST, APP_NAME, MCP_NAME + (".exe" if sys.platform == "win32" else ""))


def check_mcp_server() -> None:
    """Start the bundled MCP server like an AI client would and fail the build unless it answers.
    Catches modules PyInstaller missed and anything that writes non-protocol output to stdout."""
    exe = mcp_executable()
    with tempfile.TemporaryDirectory() as folder:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "build-check", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "open_database", "arguments": {"path": "check.db", "create": True}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "run_query", "arguments": {"sql": "SELECT sqlite_version()"}}},
        ]
        print("+", exe, "--root", folder, flush=True)
        proc = subprocess.run([exe, "--root", folder], input="".join(json.dumps(r) + "\n" for r in requests).encode(),
                              capture_output=True, timeout=120)
    try:
        replies = {r["id"]: r for r in (json.loads(line) for line in proc.stdout.decode("utf-8").splitlines()
                                         if line.strip())}
        tools = len(replies[2]["result"]["tools"])
        version_rows = replies[4]["result"]["structuredContent"]["rows"]
        if replies[3]["result"].get("isError") or replies[4]["result"].get("isError"):
            raise KeyError("tool call failed")
    except (ValueError, KeyError, TypeError) as e:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
        raise SystemExit(f"{MCP_NAME} self-check failed ({e!r}). stdout was:\n{proc.stdout[:2000]!r}")
    print(f"{MCP_NAME} OK: {tools} tools, SQLite {version_rows[0][0]}")


def zip_dir(src: str, dest_zip: str, root_name: str) -> None:
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for folder, _dirs, files in os.walk(src):
            for f in files:
                full = os.path.join(folder, f)
                zf.write(full, os.path.join(root_name, os.path.relpath(full, src)))
    print("created", dest_zip)


def find_iscc() -> str | None:
    found = shutil.which("iscc")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
        cand = os.path.join(base, "Inno Setup 6", "ISCC.exe")
        if base and os.path.exists(cand):
            return cand
    return None


def package_windows(ver: str) -> list[str]:
    src = os.path.join(DIST, APP_NAME)
    out = [os.path.join(DIST, f"{APP_NAME}-{ver}-windows-{arch()}.zip")]
    zip_dir(src, out[0], APP_NAME)
    iscc = find_iscc()
    if iscc:
        run([iscc, f"/DAppVersion={ver}", f"/DSourceDir={src}", f"/DOutputDir={DIST}",
             os.path.join(ROOT, "packaging", "windows", "sqliteview.iss")])
        out.append(os.path.join(DIST, f"{APP_NAME}-{ver}-windows-x64-setup.exe"))
    else:
        print("Inno Setup (iscc) not found: skipping the installer, the .zip is the portable build")
    return out


def package_macos(ver: str) -> list[str]:
    app = os.path.join(DIST, f"{APP_NAME}.app")
    base = os.path.join(DIST, f"{APP_NAME}-{ver}-macos-{arch()}")
    # zip keeps symlinks and permissions intact when made with ditto
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, base + ".zip"])
    staging = os.path.join(BUILD, "dmg")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    run(["cp", "-R", app, staging])
    os.symlink("/Applications", os.path.join(staging, "Applications"))
    run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", staging, "-ov", "-format", "UDZO", base + ".dmg"])
    return [base + ".dmg", base + ".zip"]


def package_linux(ver: str) -> list[str]:
    src = os.path.join(DIST, APP_NAME)
    shutil.copy(os.path.join(ROOT, "packaging", "linux", "sqliteview.desktop"), src)
    shutil.copy(os.path.join(ICONS, "sqliteview.png"), src)
    tgz = os.path.join(DIST, f"{APP_NAME}-{ver}-linux-{arch()}.tar.gz")
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(src, arcname=APP_NAME)
    print("created", tgz)
    out = [tgz]
    tool = os.environ.get("APPIMAGETOOL") or shutil.which("appimagetool")
    if not tool:
        print("appimagetool not found: skipping the AppImage")
        return out
    appdir = os.path.join(BUILD, f"{APP_NAME}.AppDir")
    shutil.rmtree(appdir, ignore_errors=True)
    shutil.copytree(src, os.path.join(appdir, "usr", "lib", "sqliteview"), symlinks=True)
    shutil.copy(os.path.join(ROOT, "packaging", "linux", "sqliteview.desktop"), appdir)
    shutil.copy(os.path.join(ICONS, "sqliteview.png"), appdir)
    apprun = os.path.join(appdir, "AppRun")
    with open(apprun, "w", encoding="utf-8", newline="\n") as fh:
        # An AppImage has one entry point: "SQliteView.AppImage mcp …" starts the MCP server instead of the app
        fh.write('#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\n'
                 f'if [ "$1" = "mcp" ]; then shift; exec "$HERE/usr/lib/sqliteview/{MCP_NAME}" "$@"; fi\n'
                 'exec "$HERE/usr/lib/sqliteview/SQliteView" "$@"\n')
    os.chmod(apprun, os.stat(apprun).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    image = os.path.join(DIST, f"{APP_NAME}-{ver}-linux-{arch()}.AppImage")
    env = dict(os.environ, ARCH=platform.machine())
    run([tool, "--no-appstream", appdir, image], env=env)
    return out + [image]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-bundle", action="store_true", help="re-use an existing PyInstaller build in dist/")
    args = ap.parse_args()
    ver = version()
    if not args.skip_bundle:
        bundle()
    check_mcp_server()
    if sys.platform == "win32":
        files = package_windows(ver)
    elif sys.platform == "darwin":
        files = package_macos(ver)
    else:
        files = package_linux(ver)
    print("\nBuilt:")
    for f in files:
        print(f"  {os.path.relpath(f, ROOT)}  ({os.path.getsize(f) / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
