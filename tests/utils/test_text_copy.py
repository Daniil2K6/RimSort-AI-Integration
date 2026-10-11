"""Copy user-facing values while retaining native editor and mod-list behavior."""

from collections.abc import Generator
from typing import Any

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QListWidget,
    QPushButton,
    QTableView,
    QTextEdit,
)

from app.utils.text_copy import (
    TextCopySupport,
    install_text_copy_support,
    label_text,
    table_selection_text,
)


@pytest.fixture
def support(qapp: QApplication) -> Generator[TextCopySupport, None, None]:
    copier = install_text_copy_support(qapp)
    yield copier
    qapp.removeEventFilter(copier)
    copier.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def test_count_label_can_be_selected_and_copied(
    qapp: QApplication, qtbot: Any, support: TextCopySupport
) -> None:
    label = QLabel("Активных модов: 123")
    qtbot.addWidget(label)
    label.show()
    qapp.processEvents()
    assert (
        label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByKeyboard
    )
    label.setSelection(16, 3)
    label.setFocus()
    qtbot.keyClick(label, Qt.Key.Key_C, modifier=Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == label.selectedText() == "123"
    label.setSelection(0, 0)
    qtbot.keyClick(label, Qt.Key.Key_C, modifier=Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "Активных модов: 123"


def test_rich_text_copy_preserves_plain_text_and_links(
    qapp: QApplication, support: TextCopySupport
) -> None:
    label = QLabel('<b>Автор</b>: <a href="https://example.test">Example</a>')
    label.setOpenExternalLinks(True)
    label.ensurePolished()
    assert label_text(label) == "Автор: Example"
    assert label.textInteractionFlags() & Qt.TextInteractionFlag.LinksAccessibleByMouse
    label.setTextFormat(Qt.TextFormat.PlainText)
    assert label_text(label).startswith("<b>")
    label.deleteLater()


def test_table_copy_uses_display_order_and_underlying_urls(
    qapp: QApplication, qtbot: Any, support: TextCopySupport
) -> None:
    model = QStandardItemModel(2, 3)
    for row, values in enumerate((("A", "10", ""), ("B", "20", ""))):
        for column, value in enumerate(values):
            model.setItem(row, column, QStandardItem(value))
    table = QTableView()
    qtbot.addWidget(table)
    table.setModel(model)
    link = QPushButton("Open Page")
    link.setProperty("copyText", "https://example.test/mod")
    table.setIndexWidget(model.index(0, 2), link)
    table.horizontalHeader().moveSection(1, 0)
    table.selectAll()
    expected = "10\tA\thttps://example.test/mod\n20\tB\t"
    assert table_selection_text(table) == expected
    qtbot.keyClick(table, Qt.Key.Key_C, modifier=Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == expected


def test_native_log_editor_and_drag_labels_keep_their_behavior(
    qapp: QApplication, qtbot: Any, support: TextCopySupport
) -> None:
    editor = QTextEdit()
    qtbot.addWidget(editor)
    editor.setReadOnly(True)
    editor.setPlainText("first\nsecond")
    editor.selectAll()
    qtbot.keyClick(editor, Qt.Key.Key_C, modifier=Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "first\nsecond"
    mods = QListWidget()
    qtbot.addWidget(mods)
    label = QLabel("Draggable mod", mods)
    QApplication.sendEvent(label, QEvent(QEvent.Type.Polish))
    assert (
        not label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    )


def test_copy_support_is_installed_once(
    qapp: QApplication, support: TextCopySupport
) -> None:
    assert install_text_copy_support(qapp) is support
