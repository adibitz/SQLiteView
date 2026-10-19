"""Small reusable widgets that make up the design system."""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from .icons import ICON_SIZE, bind_icon


def set_role(widget: QWidget, role: str) -> QWidget:
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    return widget


def label(text: str = "", role: str = "", wrap: bool = False, parent: Optional[QWidget] = None) -> QLabel:
    lb = QLabel(text, parent)
    if role:
        lb.setProperty("role", role)
    lb.setWordWrap(wrap)
    return lb


def hsep() -> QFrame:
    f = QFrame()
    f.setProperty("role", "hsep")
    return f


def vsep() -> QFrame:
    f = QFrame()
    f.setProperty("role", "sep")
    f.setFixedWidth(1)
    f.setFixedHeight(18)
    return f


def tool_button(icon_name: str, tooltip: str = "", text: str = "", kind: str = "",
                action: Optional[QAction] = None, checkable: bool = False,
                on_click: Optional[Callable] = None, shortcut: str = "") -> QToolButton:
    b = QToolButton()
    if action is not None:
        b.setDefaultAction(action)
    else:
        # Primary buttons sit on the accent colour, so their icon must match the button text
        bind_icon(b, icon_name, "accent_text" if kind == "primary" else None)
        if checkable:
            b.setCheckable(True)
        if on_click is not None:
            b.clicked.connect(on_click)
    if text:
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    else:
        b.setToolButtonStyle(Qt.ToolButtonIconOnly)
    b.setIconSize(ICON_SIZE)
    if tooltip:
        tip = tooltip + (f"  ({QKeySequence(shortcut).toString(QKeySequence.NativeText)})" if shortcut else "")
        b.setToolTip(tip)
    if kind:
        b.setProperty("kind", kind)
    b.setCursor(Qt.PointingHandCursor)
    b.setAutoRaise(True)
    return b


def push_button(text: str, kind: str = "", icon_name: str = "", on_click: Optional[Callable] = None) -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    if icon_name:
        bind_icon(b, icon_name, "accent_text" if kind == "primary" else None)
    if on_click is not None:
        b.clicked.connect(on_click)
    b.setCursor(Qt.PointingHandCursor)
    return b


def make_action(parent, text: str, icon_name: str = "", shortcut: str | QKeySequence | Sequence = "",
                triggered: Optional[Callable] = None, tip: str = "", checkable: bool = False) -> QAction:
    a = QAction(text, parent)
    if icon_name:
        bind_icon(a, icon_name)
    if shortcut:
        if isinstance(shortcut, (list, tuple)):
            a.setShortcuts([QKeySequence(s) for s in shortcut])
        else:
            a.setShortcut(QKeySequence(shortcut))
    if tip:
        a.setToolTip(tip)
        a.setStatusTip(tip)
    a.setCheckable(checkable)
    if triggered is not None:
        a.triggered.connect(triggered)
    return a


class SearchLineEdit(QLineEdit):
    """Rounded search field with a leading magnifier and a clear button."""

    escapePressed = Signal()

    def __init__(self, placeholder: str = "Search", parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.setProperty("role", "search")
        act = QAction(self)
        bind_icon(act, "search")
        self.addAction(act, QLineEdit.LeadingPosition)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self.text():
                self.clear()
            else:
                self.escapePressed.emit()
            return
        super().keyPressEvent(e)


class SegmentedControl(QFrame):
    """A row of mutually exclusive toggle buttons (macOS/Fluent style)."""

    changed = Signal(int)

    def __init__(self, items: Sequence[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setProperty("role", "segmented")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for i, (text, icon_name) in enumerate(items):
            b = QToolButton()
            b.setText(text)
            if icon_name:
                bind_icon(b, icon_name)
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setIconSize(QSize(14, 14))
            self._group.addButton(b, i)
            lay.addWidget(b)
        self._group.button(0).setChecked(True)
        self._group.idClicked.connect(self.changed)

    def current(self) -> int:
        return self._group.checkedId()

    def set_current(self, i: int) -> None:
        b = self._group.button(i)
        if b is not None:
            b.setChecked(True)

    def button(self, i: int) -> QAbstractButton:
        return self._group.button(i)


class EmptyState(QWidget):
    """Centered illustration + title + hint + actions."""

    def __init__(self, icon_name: str, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        from .icons import icon

        lay = QVBoxLayout(self)
        lay.addStretch(1)
        ic = QLabel()
        ic.setPixmap(icon(icon_name, "text_faint").pixmap(44, 44))
        ic.setAlignment(Qt.AlignCenter)
        lay.addWidget(ic)
        t = label(title, "h2")
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)
        self.subtitle = label(subtitle, "muted", wrap=True)
        self.subtitle.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.subtitle)
        self.actions_row = QHBoxLayout()
        self.actions_row.addStretch(1)
        self._row_end = QWidget()
        lay.addLayout(self.actions_row)
        lay.addStretch(2)

    def add_button(self, button: QPushButton) -> None:
        self.actions_row.addWidget(button)

    def finish(self) -> None:
        self.actions_row.addStretch(1)


class Toast(QFrame):
    """Transient, non-blocking notification shown at the bottom center of its parent."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setProperty("role", "toast")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 9, 14, 9)
        self._label = QLabel()
        lay.addWidget(self._label)
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self.hide()

    def show_message(self, text: str, msecs: int = 2600) -> None:
        self._label.setText(text)
        self.adjustSize()
        p = self.parentWidget()
        self.move(QPoint((p.width() - self.width()) // 2, p.height() - self.height() - 48))
        self.raise_()
        self.show()
        self._anim.stop()
        self._anim.setDuration(160)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._timer.start(msecs)

    def _fade_out(self) -> None:
        self._anim.stop()
        self._anim.setDuration(300)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self._hide_once)
        self._anim.start()

    def _hide_once(self) -> None:
        try:
            self._anim.finished.disconnect(self._hide_once)
        except (RuntimeError, TypeError):
            pass
        if self._effect.opacity() < 0.05:
            self.hide()


class Banner(QFrame):
    """Inline message bar with optional action buttons (e.g. pending changes, read-only)."""

    def __init__(self, parent=None, danger: bool = False):
        super().__init__(parent)
        self.setProperty("role", "banner-danger" if danger else "banner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 8, 6)
        self.icon = QLabel()
        lay.addWidget(self.icon)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay.addWidget(self.text, 1)
        self.buttons = QHBoxLayout()
        lay.addLayout(self.buttons)


class IntField(QLineEdit):
    """Compact integer input (QSpinBox look-alike without spin buttons, which eat space in narrow fields)."""

    def __init__(self, minimum: int = 1, maximum: int = 1, parent=None):
        super().__init__(parent)
        from PySide6.QtGui import QIntValidator
        self._validator = QIntValidator(minimum, maximum, self)
        self.setValidator(self._validator)
        self.setAlignment(Qt.AlignCenter)
        self.setText(str(minimum))

    def setRange(self, minimum: int, maximum: int) -> None:  # noqa: N802 - mirrors QSpinBox
        self._validator.setRange(minimum, min(maximum, 2**31 - 1))
        if self.text() and not (minimum <= self.value() <= maximum):
            self.setValue(max(minimum, min(self.value(), maximum)))

    def value(self) -> int:
        try:
            return int(self.text())
        except ValueError:
            return self._validator.bottom()

    def setValue(self, v: int) -> None:  # noqa: N802 - mirrors QSpinBox
        self.setText(str(int(v)))
