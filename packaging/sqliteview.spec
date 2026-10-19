# PyInstaller spec for SQliteView. Build with:  python scripts/build.py
# (or directly:  pyinstaller --noconfirm packaging/sqliteview.spec)
# Produces dist/SQliteView/ (Windows, Linux) or dist/SQliteView.app (macOS) with two programs that share
# one set of libraries: SQliteView (the window app) and sqliteview-mcp (the console MCP server that AI
# assistants start; it needs real stdin/stdout, which a windowed program doesn't have on Windows).
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - SPECPATH is provided by PyInstaller
ICONS = os.path.join(ROOT, "sqliteview", "assets", "icons")

with open(os.path.join(ROOT, "sqliteview", "__init__.py"), encoding="utf-8") as fh:
    VERSION = re.search(r'__version__ = "([^"]+)"', fh.read()).group(1)

# Qt modules the app never imports; leaving them out keeps the bundle much smaller.
EXCLUDES = [
    "tkinter",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D", "PySide6.QtQml",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DExtras",
    "PySide6.Qt3DAnimation", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtBluetooth", "PySide6.QtLocation", "PySide6.QtPositioning",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtNfc", "PySide6.QtDesigner", "PySide6.QtTest",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtHttpServer",
]

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "sqliteview", "assets"), os.path.join("sqliteview", "assets"))],
    hiddenimports=["PySide6.QtSvg", "PySide6.QtCharts"],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SQliteView",
    console=False,
    icon=os.path.join(ICONS, "sqliteview.icns" if sys.platform == "darwin" else "sqliteview.ico"),
    upx=False,
)

# The MCP server never imports widgets, nor these packages that the MCP SDK's dependencies only reference
# optionally (HTTP transports, auth, plugins); leaving them out keeps ~70 MB out of the bundle.
# The Qt libraries themselves are shared with the app through COLLECT below.
MCP_EXCLUDES = EXCLUDES + [
    "PySide6.QtWidgets", "PySide6.QtGui", "PySide6.QtSvg", "PySide6.QtCharts",
    "numpy", "PIL", "setuptools", "pkg_resources", "yaml", "psutil", "simplejson", "tomli", "websockets",
    "cryptography", "jwt",
]
a_mcp = Analysis(  # noqa: F821
    [os.path.join(ROOT, "mcp_main.py")],
    pathex=[ROOT],
    hiddenimports=["anyio._backends._asyncio"],  # anyio loads its event-loop backend by name
    excludes=MCP_EXCLUDES,
    noarchive=False,
)
exe_mcp = EXE(  # noqa: F821
    PYZ(a_mcp.pure),  # noqa: F821
    a_mcp.scripts,
    [],
    exclude_binaries=True,
    name="sqliteview-mcp",
    console=True,
    icon=os.path.join(ICONS, "sqliteview.icns" if sys.platform == "darwin" else "sqliteview.ico"),
    upx=False,
)

# COLLECT drops duplicate entries, so both programs share one _internal folder
coll = COLLECT(exe, exe_mcp, a.binaries + a_mcp.binaries, a.datas + a_mcp.datas,  # noqa: F821
               name="SQliteView", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="SQliteView.app",
        icon=os.path.join(ICONS, "sqliteview.icns"),
        bundle_identifier="io.github.adibitz.sqliteview",
        version=VERSION,
        info_plist={
            "CFBundleName": "SQliteView",
            "CFBundleDisplayName": "SQliteView",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,  # follow macOS dark mode
            "LSMinimumSystemVersion": "11.0",
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "SQLite database",
                "CFBundleTypeRole": "Editor",
                "LSHandlerRank": "Alternate",
                "CFBundleTypeExtensions": ["db", "sqlite", "sqlite3", "db3", "sqbpro"],
            }],
        },
    )
