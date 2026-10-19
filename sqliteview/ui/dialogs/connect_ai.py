"""Help › Connect AI Assistant: ready-to-paste MCP configuration for Claude and other assistants."""
from __future__ import annotations

from typing import Optional

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QPlainTextEdit, QVBoxLayout

from ...mcp.clientconfig import CLIENTS, server_args, server_available, server_command
from ..icons import icon
from ..theme import mono_font
from ..widgets import Banner, SegmentedControl, label, push_button


class ConnectAiDialog(QDialog):
    def __init__(self, database: Optional[str] = None, read_only: bool = False, parent=None):
        super().__init__(parent)
        self.database = database
        self.setWindowTitle("Connect AI Assistant")
        self.resize(640, 480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        lay.addWidget(label("Connect AI Assistant", "title"))
        lay.addWidget(label("Claude and other AI assistants can browse, query, edit, import and export your databases "
                            "through SQliteView's MCP server. The assistant starts the server itself: set it up once "
                            "with the snippet below. Changes it makes stay uncommitted until you (or it, after asking) "
                            "commit them.", "muted", wrap=True))
        if not server_available():
            banner = Banner(danger=True)
            banner.icon.setPixmap(icon("alert", "danger").pixmap(16, 16))
            banner.text.setText('The MCP server isn\'t installed. Run: pip install "sqliteview[mcp]"')
            lay.addWidget(banner)
        self.clients = SegmentedControl([(c.name, "") for c in CLIENTS])
        self.clients.changed.connect(lambda _i: self._update())
        row = QHBoxLayout()
        row.addWidget(self.clients)
        row.addStretch(1)
        lay.addLayout(row)
        self.use_db = QCheckBox(f"Open {database} at start" if database else "Open a database at start")
        self.use_db.setChecked(bool(database))
        self.use_db.setEnabled(bool(database))
        self.use_db.setToolTip("Without a database the assistant can open any database file in the server's folder.")
        self.read_only = QCheckBox("Read-only (the assistant can't change anything)")
        self.read_only.setChecked(read_only)
        for box in (self.use_db, self.read_only):
            box.toggled.connect(lambda _on: self._update())
            lay.addWidget(box)
        self.where = label("", "muted", wrap=True)
        lay.addWidget(self.where)
        self.snippet = QPlainTextEdit()
        self.snippet.setReadOnly(True)
        self.snippet.setFont(mono_font())
        lay.addWidget(self.snippet, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        self.copy_button = push_button("Copy", "primary", "copy", self.copy)
        bb.addButton(self.copy_button, QDialogButtonBox.ActionRole)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._update()

    def _update(self) -> None:
        client = CLIENTS[self.clients.current()]
        args = server_args(self.database if self.use_db.isChecked() else None, self.read_only.isChecked())
        self.where.setText(client.where)
        self.snippet.setPlainText(client.snippet(server_command(), args))

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.snippet.toPlainText())
        self.copy_button.setText("Copied")
