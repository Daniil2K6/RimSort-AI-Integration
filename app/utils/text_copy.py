"""Consistent copying of display text without altering editable widgets.

Installed once on the application. Native text editors retain their own copy
and selection behavior; labels and data tables gain selection and shortcuts.
Existing custom context menus and drag/drop mod lists are left intact.
"""

from __future__ import annotations

import re
from typing import cast

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QContextMenuEvent, QKeyEvent, QKeySequence, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QMenu,
    QTableView,
    QTreeView,
)


def label_text(label: QLabel) -> str:
    """Copy visible rich text without tags, preserving explicit plain text."""
    text = label.text()
    if label.textFormat() == Qt.TextFormat.RichText or (
        label.textFormat() == Qt.TextFormat.AutoText
        and re.search(r"</?[A-Za-z][^>]*>", text)
    ):
        document = QTextDocument()
        document.setHtml(text)
        return document.toPlainText()
    return text


def table_selection_text(view: QTableView | QTreeView) -> str:
    """Selected cells as TSV in visual order; copy underlying link targets too."""
    selection = view.selectionModel()
    if selection is None:
        return ""
    indexes = selection.selectedIndexes()
    if not indexes:
        return ""
    header = view.horizontalHeader() if isinstance(view, QTableView) else view.header()
    columns = sorted({i.column() for i in indexes}, key=header.visualIndex)
    rows: dict[tuple[int, ...], dict[int, str]] = {}
    for index in indexes:
        # Parent path makes identically numbered rows in tree branches distinct.
        path = [index.row()]
        parent = index.parent()
        while parent.isValid():
            path.insert(0, parent.row())
            parent = parent.parent()
        value = index.data(Qt.ItemDataRole.DisplayRole)
        text = "" if value is None else str(value)
        widget = view.indexWidget(index)
        if widget is not None:
            target = widget.property("copyText")
            if isinstance(target, str):
                text = target
            elif isinstance(widget, QLabel):
                text = label_text(widget)
        rows.setdefault(tuple(path), {})[index.column()] = text
    # Checkbox-only columns aren't useful text and should not add leading tabs.
    columns = [c for c in columns if any(row.get(c, "") for row in rows.values())]
    return (
        "\n".join(
            "\t".join(
                row.get(c, "").replace("\t", " ").replace("\n", " ") for c in columns
            )
            for _, row in sorted(rows.items())
        )
        if columns
        else ""
    )


class TextCopySupport(QObject):
    """Application-wide support for labels/tables created now or later."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QLabel):
            if watched.property("textCopyDisabled"):
                return False
            if event.type() == QEvent.Type.Polish:
                # Embedded mod-list labels must keep drag/drop and row selection.
                parent = watched.parentWidget()
                while parent is not None:
                    if isinstance(parent, QAbstractItemView):
                        return False
                    parent = parent.parentWidget()
                watched.setTextInteractionFlags(
                    watched.textInteractionFlags()
                    | Qt.TextInteractionFlag.TextSelectableByMouse
                    | Qt.TextInteractionFlag.TextSelectableByKeyboard
                )
            if event.type() == QEvent.Type.KeyPress:
                key = cast(QKeyEvent, event)
                if key.matches(QKeySequence.StandardKey.Copy):
                    text = watched.selectedText() or label_text(watched)
                    if text:
                        QApplication.clipboard().setText(text)
                        return True
            if (
                event.type() == QEvent.Type.ContextMenu
                and watched.contextMenuPolicy()
                == Qt.ContextMenuPolicy.DefaultContextMenu
            ):
                text = watched.selectedText() or label_text(watched)
                if text:
                    menu = QMenu(watched)
                    action = menu.addAction(self.tr("Copy"))
                    if menu.exec(cast(QContextMenuEvent, event).globalPos()) is action:
                        QApplication.clipboard().setText(text)
                    return True
        view = (
            watched
            if isinstance(watched, (QTableView, QTreeView))
            else watched.parent()
        )
        if isinstance(view, (QTableView, QTreeView)) and (
            watched is view or watched is view.viewport()
        ):
            if event.type() == QEvent.Type.KeyPress and cast(QKeyEvent, event).matches(
                QKeySequence.StandardKey.Copy
            ):
                text = table_selection_text(view)
                if text:
                    QApplication.clipboard().setText(text)
                    return True
            if (
                event.type() == QEvent.Type.ContextMenu
                and view.contextMenuPolicy() == Qt.ContextMenuPolicy.DefaultContextMenu
            ):
                text = table_selection_text(view)
                if text:
                    menu = QMenu(view)
                    action = menu.addAction(self.tr("Copy"))
                    if menu.exec(cast(QContextMenuEvent, event).globalPos()) is action:
                        QApplication.clipboard().setText(text)
                    return True
        return False


def install_text_copy_support(app: QApplication) -> TextCopySupport:
    """Keep one parent-owned filter alive for the application's lifetime."""
    for child in app.children():
        if isinstance(child, TextCopySupport):
            app.installEventFilter(child)
            return child
    support = TextCopySupport(app)
    app.installEventFilter(support)
    # Also handle labels created before installation (e.g. during startup).
    for widget in app.allWidgets():
        if isinstance(widget, QLabel):
            QApplication.sendEvent(widget, QEvent(QEvent.Type.Polish))
    return support
