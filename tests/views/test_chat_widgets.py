"""Tests for chat widgets: one-click copy, selectable text, tool rows."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QEnterEvent
from PySide6.QtWidgets import QApplication

from app.views.chat_widgets import MessageBubble, ToolCallRow

if TYPE_CHECKING:
    from PySide6.QtCore import QCoreApplication


def _bubble(text: str = "hello world copy test") -> MessageBubble:
    bubble = MessageBubble("#346792", "#ffffff", role="user", markdown=False)
    bubble.set_plain(text)
    bubble.resize(300, 80)
    bubble.show()
    return bubble


def test_bubble_label_is_selectable_by_mouse_and_keyboard(
    qapp: QApplication | QCoreApplication,
) -> None:
    bubble = _bubble()
    flags = bubble._label.textInteractionFlags()
    assert flags & Qt.TextInteractionFlag.TextSelectableByMouse
    assert flags & Qt.TextInteractionFlag.TextSelectableByKeyboard
    bubble.deleteLater()


def test_tool_row_label_is_selectable_by_mouse_and_keyboard(
    qapp: QApplication | QCoreApplication,
) -> None:
    row = ToolCallRow("→ toolname", "#888888", "#ff0000")
    flags = row._label.textInteractionFlags()
    assert flags & Qt.TextInteractionFlag.TextSelectableByMouse
    assert flags & Qt.TextInteractionFlag.TextSelectableByKeyboard
    row.deleteLater()


def test_copy_button_hidden_until_hover(qapp: QApplication | QCoreApplication) -> None:
    bubble = _bubble()
    assert not bubble._copy_button.isVisible()
    bubble.enterEvent(QEnterEvent(QPoint(0, 0), QPoint(0, 0), QPoint(0, 0)))
    assert bubble._copy_button.isVisible()
    bubble.deleteLater()


def test_copy_button_click_copies_plain_text_to_clipboard(
    qapp: QApplication | QCoreApplication,
) -> None:
    bubble = _bubble("copy me to the clipboard")
    bubble._copy_button.click()
    assert QApplication.clipboard().text() == "copy me to the clipboard"
    bubble.deleteLater()


def test_copy_button_anchored_to_top_right_on_resize(
    qapp: QApplication | QCoreApplication,
) -> None:
    bubble = _bubble()
    bubble.resize(300, 80)
    button = bubble._copy_button
    assert button.x() + button.width() <= bubble.width()
    assert button.x() >= bubble.width() // 2
    bubble.deleteLater()


def test_context_menu_copy_falls_back_to_whole_text(
    qapp: QApplication | QCoreApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no selection, Copy in the context menu copies the whole text."""
    from app.views import chat_widgets

    class _FakeAction:
        def __init__(self, text: str) -> None:
            self.text = text
            self.enabled = True

        def setEnabled(self, enabled: bool) -> None:
            self.enabled = enabled

    class _FakeMenu:
        def __init__(self, parent: object = None) -> None:
            self._actions: list[_FakeAction] = []

        def addAction(self, text: str) -> _FakeAction:
            action = _FakeAction(text)
            self._actions.append(action)
            return action

        def exec(self, _pos: QPoint) -> _FakeAction:
            # Return the first ("Copy") action.
            return self._actions[0]

    monkeypatch.setattr(chat_widgets, "QMenu", _FakeMenu)

    bubble = _bubble("fallback whole text")
    bubble._show_context_menu(QPoint(1, 1))
    assert QApplication.clipboard().text() == "fallback whole text"
    bubble.deleteLater()
