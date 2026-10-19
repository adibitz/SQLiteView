# PyInstaller spec for SQliteView. Build with:  python scripts/build.py
# (or directly:  pyinstaller --noconfirm packaging/sqliteview.spec)
# Produces dist/SQliteView/ (Windows, Linux) or dist/SQliteView.app (macOS).
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

coll = COLLECT(exe, a.binaries, a.datas, name="SQliteView", upx=False)  # noqa: F821

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
