"""Word-completion popup for the chat input box.

The chat input is a :class:`QPlainTextEdit`, which has no built-in
completion support (unlike ``QLineEdit``), so a plain ``QCompleter`` cannot
just be attached. This helper drives a ``QCompleter`` manually from the
panel's event filter: on every edit it extracts the run of non-space
characters ending at the cursor, offers matching mod names/packageIds, and
replaces that run in place when the user accepts a suggestion.

The token/matching logic is kept in pure module-level functions so it can be
unit-tested without a Qt event loop.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from PySide6.QtCore import QAbstractItemModel, QObject, QRect, QStringListModel, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QCompleter, QListView, QPlainTextEdit

# Minimum characters typed before the popup appears (keeps noise down).
MIN_PREFIX = 2
# Cap the suggestion list so the popup stays scannable.
MAX_SUGGESTIONS = 8


def token_at(text: str, pos: int) -> tuple[str, int]:
    """Return ``(token, start)`` for the run of non-space chars ending at ``pos``.

    ``token`` is ``text[start:pos]``. ``pos`` is clamped into ``[0, len(text)]``.
    """
    pos = max(0, min(pos, len(text)))
    start = pos
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    return text[start:pos], start


def filter_labels(
    labels: Sequence[str], token: str, limit: int = MAX_SUGGESTIONS
) -> list[str]:
    """Return up to ``limit`` labels matching ``token`` (case-insensitive).

    Prefix matches rank before substring matches; both groups are sorted
    alphabetically (case-insensitively) so the order is stable.
    """
    needle = token.lower()
    if not needle:
        return []
    prefix: list[str] = []
    contains: list[str] = []
    for label in labels:
        low = label.lower()
        if low.startswith(needle):
            prefix.append(label)
        elif needle in low:
            contains.append(label)
    prefix.sort(key=str.lower)
    contains.sort(key=str.lower)
    return (prefix + contains)[:limit]


def completion_insertion(text: str, pos: int, token: str, completion: str) -> str:
    """Return ``text`` with ``token`` (ending at ``pos``) replaced by ``completion``."""
    pos = max(0, min(pos, len(text)))
    start = pos - len(token)
    if start < 0 or text[start:pos] != token:
        return text
    return text[:start] + completion + text[pos:]


class ModCompleter(QObject):
    """Prefix/substring completion popup bound to a ``QPlainTextEdit``."""

    def __init__(self, edit: QPlainTextEdit, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._edit = edit
        self._labels: list[str] = []

        self._model = QStringListModel(self)
        self._completer = QCompleter(self._model, self)
        self._completer.setWidget(edit)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        # The model is already pre-filtered per keystroke, but the completer
        # re-filters by prefix; allow substring so pre-filtered rows survive.
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        popup = self._completer.popup()
        assert popup is not None  # QCompleter always owns a popup view
        self._popup = cast(QListView, popup)
        self._popup.setObjectName("ChatCompleter")
        self._popup.setUniformItemSizes(True)

    # ------------------------------------------------------------- data
    def set_labels(self, labels: Sequence[str]) -> None:
        """Set the full pool of suggestion strings (names + packageIds)."""
        seen: set[str] = set()
        unique: list[str] = []
        for label in labels:
            low = label.lower()
            if low not in seen:
                seen.add(low)
                unique.append(label)
        self._labels = unique

    def labels(self) -> list[str]:
        return list(self._labels)

    # ----------------------------------------------------------- popup
    def is_visible(self) -> bool:
        return self._popup.isVisible()

    def hide(self) -> None:
        self._popup.hide()

    def update_popup(self) -> None:
        """Recompute the token under the cursor and refresh the popup."""
        text = self._edit.toPlainText()
        cursor = self._edit.textCursor()
        token, _ = token_at(text, cursor.position())
        if len(token) < MIN_PREFIX:
            self.hide()
            return
        matches = filter_labels(self._labels, token)
        if not matches:
            self.hide()
            return
        self._model.setStringList(matches)
        self._completer.setCompletionPrefix(token)
        self._completer.complete(self._popup_rect())
        # Make sure the first match is highlighted so Enter/Tab has an
        # obvious default target.
        first = self._completer.completionModel().index(0, 0)
        if first.isValid():
            self._popup.setCurrentIndex(first)

    def _popup_rect(self) -> QRect:
        """Rectangle (in widget coordinates) under the cursor for the popup."""
        rect = self._edit.cursorRect()
        width = max(
            rect.width(),
            self._popup.sizeHintForColumn(0)
            + self._popup.verticalScrollBar().sizeHint().width(),
        )
        rect.setWidth(width)
        return rect

    # -------------------------------------------------------------- keys
    def handle_key(self, event: QKeyEvent) -> bool:
        """Handle a key press for the popup; return True if it was consumed.

        Consumed keys: Up/Down navigate the popup, Tab/Enter accept the
        highlighted suggestion, Escape dismisses the popup. Anything else is
        left to the caller (and the popup is refreshed by ``update_popup``).
        """
        if not self.is_visible():
            return False
        key = event.key()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._move_selection(-1 if key == Qt.Key.Key_Up else 1)
            return True
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self._accept()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._accept()
            return True
        if key == Qt.Key.Key_Escape:
            self.hide()
            return True
        return False

    def _move_selection(self, delta: int) -> None:
        model = self._completer.completionModel()
        rows = model.rowCount()
        if rows == 0:
            return
        row = self._popup.currentIndex().row()
        # An invalid index yields -1; clamp after applying the delta.
        row = max(0, min(rows - 1, max(row, 0) + delta))
        index = model.index(row, 0)
        self._popup.setCurrentIndex(index)
        self._popup.scrollTo(index)

    def _accept(self) -> None:
        model: QAbstractItemModel = self._completer.completionModel()
        index = self._popup.currentIndex()
        if not index.isValid():
            index = model.index(0, 0)
        completion = model.data(index, Qt.ItemDataRole.DisplayRole)
        if completion:
            self._insert_completion(str(completion))
        self.hide()

    def _insert_completion(self, completion: str) -> None:
        text = self._edit.toPlainText()
        cursor = self._edit.textCursor()
        pos = cursor.position()
        token, _ = token_at(text, pos)
        new_text = completion_insertion(text, pos, token, completion)
        if new_text == text:
            return
        # Keep the cursor just after the inserted completion (+ trailing space).
        insert_len = len(completion) + 1
        new_pos = pos - len(token) + insert_len
        self._edit.setPlainText(new_text)
        cursor = self._edit.textCursor()
        cursor.setPosition(min(new_pos, len(new_text)))
        self._edit.setTextCursor(cursor)


def build_mod_labels(mods_metadata: object) -> list[str]:
    """Extract completion labels (display names + packageIds) from mod metadata.

    ``mods_metadata`` is a ``dict`` of path -> mod object. Only ``AboutXmlMod``
    entries contribute (they carry a ``package_id``); non-string names are
    skipped. Duplicates are removed case-insensitively while keeping order.
    """
    from app.models.metadata.metadata_structure import AboutXmlMod

    labels: list[str] = []
    seen: set[str] = set()
    items = getattr(mods_metadata, "items", None)
    if not callable(items):
        return labels
    for _path, mod in items():
        if not isinstance(mod, AboutXmlMod):
            continue
        name = mod.name if isinstance(mod.name, str) else ""
        package_id = str(mod.package_id)
        for label in (name.strip(), package_id.strip()):
            low = label.lower()
            if label and low not in seen:
                seen.add(low)
                labels.append(label)
    return labels
