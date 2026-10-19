import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

_app = None


def pytest_configure(config):
    import tempfile

    from PySide6.QtWidgets import QApplication

    from sqliteview.core.settings import Settings

    global _app
    # One QApplication for all tests (engine tests only need the core event loop)
    _app = QApplication.instance() or QApplication([])
    # Never touch the user's real settings from tests
    Settings.use_file(os.path.join(tempfile.mkdtemp(prefix="dbb-tests-"), "settings.ini"))
