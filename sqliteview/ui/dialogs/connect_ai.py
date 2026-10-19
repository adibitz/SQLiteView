"""Help › Connect AI Assistant: ready-to-paste MCP configuration for Claude and other assistants."""
from __future__ import annotations

from typing import Optional

from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QPlainTextEdit, QVBoxLayout

from ...mcp.clientconfig import CLIENTS, server_args, server_available, server_command
from ..icons import icon
from ..theme import mono_font
from ..widgets import Banner, SegmentedControl, label, push_button

MODE_LIVE, MODE_FILE = 0, 1


class ConnectAiDialog(QDialog):
    def __init__(self, database: Optional[str] = None, read_only: bool = False,
                 live_action: Optional[QAction] = None, parent=None):
        """``live_action``: the window's checkable Tools › Allow AI Assistants action (live mode)."""
        super().__init__(parent)
        self.database = database
        self.live_action = live_action
        self.setWindowTitle("Connect AI Assistant")
        self.resize(660, 520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        lay.addWidget(label("Connect AI Assistant", "title"))
        lay.addWidget(label("Claude and other AI assistants can browse, query, edit, import and export your databases "
                            "through SQliteView's MCP server. Set your assistant up once with the snippet below; it "
                            "starts the server itself.", "muted", wrap=True))
        if not server_available():
            banner = Banner(danger=True)
            banner.icon.setPixmap(icon("alert", "danger").pixmap(16, 16))
            banner.text.setText('The MCP server isn\'t installed. Run: pip install "sqliteview[mcp]"')
            lay.addWidget(banner)

        self.mode = SegmentedControl([("This window (live)", "sparkle"), ("A database file", "database")])
        self.mode.set_current(MODE_LIVE if live_action is not None and live_action.isChecked() else MODE_FILE)
        self.mode.button(MODE_LIVE).setEnabled(live_action is not None)
        self.mode.changed.connect(lambda _i: self._update())
        row = QHBoxLayout()
        row.addWidget(self.mode)
        row.addStretch(1)
        lay.addLayout(row)
        self.mode_help = label("", "muted", wrap=True)
        lay.addWidget(self.mode_help)
        live_row = QHBoxLayout()
        self.live_state = label("", "chip-warning")
        live_row.addWidget(self.live_state)
        self.live_button = push_button("Turn on", "", "sparkle", self._turn_on_live)
        live_row.addWidget(self.live_button)
        live_row.addStretch(1)
        lay.addLayout(live_row)

        self.use_db = QCheckBox(f"Open {database} at start" if database else "Open a database at start")
        self.use_db.setChecked(bool(database))
        self.use_db.setEnabled(bool(database))
        self.use_db.setToolTip("Without a database the assistant can open any database file in the server's folder.")
        self.read_only = QCheckBox("Read-only (the assistant can't change anything)")
        self.read_only.setChecked(read_only)
        for box in (self.use_db, self.read_only):
            box.toggled.connect(lambda _on: self._update())
            lay.addWidget(box)

        self.clients = SegmentedControl([(c.name, "") for c in CLIENTS])
        self.clients.changed.connect(lambda _i: self._update())
        row = QHBoxLayout()
        row.addWidget(self.clients)
        row.addStretch(1)
        lay.addLayout(row)
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

    @property
    def live(self) -> bool:
        return self.mode.current() == MODE_LIVE

    def _update(self) -> None:
        live_on = self.live_action is not None and self.live_action.isChecked()
        if self.live:
            self.mode_help.setText("The assistant works on whatever database is open in this window, while it is "
                                   "open. Its changes appear right away but stay unsaved until you press Write "
                                   "Changes (or Revert Changes).")
        else:
            self.mode_help.setText("The assistant opens a database file itself, also when SQliteView isn't running. "
                                   "Its changes are saved when it commits them (it asks you first).")
        self.live_state.setText("Tools › Allow AI Assistants is on" if live_on else "Tools › Allow AI Assistants is off")
        self.live_state.setProperty("role", "chip-accent" if live_on else "chip-warning")
        self.live_state.style().unpolish(self.live_state)
        self.live_state.style().polish(self.live_state)
        for w in (self.live_state, self.live_button):
            w.setVisible(self.live)
        self.live_button.setVisible(self.live and not live_on)
        for box in (self.use_db, self.read_only):
            box.setVisible(not self.live)
        client = CLIENTS[self.clients.current()]
        args = server_args(self.database if self.use_db.isChecked() else None, self.read_only.isChecked(),
                           live=self.live)
        self.where.setText(client.where)
        self.snippet.setPlainText(client.snippet(server_command(), args))
        self.copy_button.setText("Copy")

    def _turn_on_live(self) -> None:
        if self.live_action is not None and not self.live_action.isChecked():
            self.live_action.trigger()
        self._update()

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.snippet.toPlainText())
        self.copy_button.setText("Copied")
