"""Tests for the chat-input mod-name completer."""

from __future__ import annotations

from typing import ClassVar

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit

from app.models.metadata.metadata_structure import (
    AboutXmlMod,
    CaseInsensitiveStr,
    ListedMod,
)
from app.views.chat_completer import (
    ModCompleter,
    build_mod_labels,
    completion_insertion,
    filter_labels,
    token_at,
)

_LABELS = ["Vanilla Furniture Expanded", "Alpha Mods", "Beta Pack"]


def _about_mod(name: str, package_id: str) -> AboutXmlMod:
    return AboutXmlMod(name=name, package_id=CaseInsensitiveStr(package_id))


def _make_edit(text: str = "") -> QPlainTextEdit:
    edit = QPlainTextEdit()
    edit.setPlainText(text)
    # setPlainText parks the cursor at the start; typing leaves it at the end,
    # so mimic that for the completer's token extraction.
    edit.moveCursor(QTextCursor.MoveOperation.End)
    return edit


class TestTokenAt:
    def test_empty_text(self) -> None:
        assert token_at("", 0) == ("", 0)

    def test_single_word(self) -> None:
        assert token_at("hello", 5) == ("hello", 0)

    def test_token_after_space(self) -> None:
        assert token_at("add mod", 7) == ("mod", 4)

    def test_token_is_last_word_only(self) -> None:
        assert token_at("vanilla furniture", 17) == ("furniture", 8)

    def test_pos_clamped_to_length(self) -> None:
        assert token_at("abc", 10) == ("abc", 0)

    def test_cursor_in_middle(self) -> None:
        assert token_at("foo bar", 3) == ("foo", 0)

    def test_leading_space(self) -> None:
        assert token_at("  ab", 4) == ("ab", 2)


class TestFilterLabels:
    LABELS: ClassVar[list[str]] = [
        "Vanilla Furniture Expanded",
        "Alpha Mods",
        "vanilla.furnitureexpanded",
    ]

    def test_prefix_matches_first(self) -> None:
        result = filter_labels(self.LABELS, "van")
        assert result[0] in ("Vanilla Furniture Expanded", "vanilla.furnitureexpanded")

    def test_contains_matches_later_word(self) -> None:
        result = filter_labels(self.LABELS, "furn")
        assert "Vanilla Furniture Expanded" in result
        assert "vanilla.furnitureexpanded" in result

    def test_case_insensitive(self) -> None:
        assert filter_labels(self.LABELS, "ALPHA") == ["Alpha Mods"]

    def test_no_match_returns_empty(self) -> None:
        assert filter_labels(self.LABELS, "zzz") == []

    def test_empty_token_returns_empty(self) -> None:
        assert filter_labels(self.LABELS, "") == []

    def test_limit_respected(self) -> None:
        many = [f"mod-{i}" for i in range(20)]
        assert len(filter_labels(many, "mod-", limit=5)) == 5

    def test_prefix_ranks_before_contains(self) -> None:
        labels = ["x alpha", "alpha beta"]
        result = filter_labels(labels, "alpha")
        assert result[0] == "alpha beta"


class TestCompletionInsertion:
    def test_replaces_token(self) -> None:
        assert completion_insertion("add van", 7, "van", "Vanilla") == "add Vanilla"

    def test_no_surrounding_space(self) -> None:
        assert completion_insertion("van", 3, "van", "Vanilla") == "Vanilla"

    def test_token_mismatch_is_noop(self) -> None:
        assert completion_insertion("abc", 3, "xyz", "Vanilla") == "abc"

    def test_preserves_suffix(self) -> None:
        assert (
            completion_insertion("add van now", 7, "van", "Vanilla")
            == "add Vanilla now"
        )


class TestBuildModLabels:
    def test_includes_name_and_package_id(self) -> None:
        mods = {"/a": _about_mod("Alpha", "mod.alpha")}
        labels = build_mod_labels(mods)
        assert "Alpha" in labels
        assert "mod.alpha" in labels

    def test_skips_non_aboutxml_mods(self) -> None:
        mods: dict[str, ListedMod] = {"/a": ListedMod(name="NoPackageId")}
        assert build_mod_labels(mods) == []

    def test_dedupes_case_insensitively(self) -> None:
        mods = {
            "/a": _about_mod("Alpha", "mod.alpha"),
            "/b": _about_mod("alpha", "mod.alpha"),
        }
        labels = build_mod_labels(mods)
        assert labels.count("Alpha") + labels.count("alpha") == 1

    def test_skips_blank_name(self) -> None:
        mods = {"/a": _about_mod("", "mod.alpha")}
        labels = build_mod_labels(mods)
        assert "" not in labels
        assert "mod.alpha" in labels

    def test_non_mapping_returns_empty(self) -> None:
        assert build_mod_labels(None) == []
        assert build_mod_labels(["not", "a", "dict"]) == []


class TestModCompleterQt:
    """Integration tests driving a real QPlainTextEdit + completer."""

    def test_popup_shows_on_matching_prefix(self, qapp: object) -> None:
        edit = _make_edit("va")
        completer = ModCompleter(edit)
        completer.set_labels(_LABELS)
        completer.update_popup()
        assert completer.is_visible()

    def test_popup_hidden_below_min_prefix(self, qapp: object) -> None:
        edit = _make_edit("v")
        completer = ModCompleter(edit)
        completer.set_labels(_LABELS)
        completer.update_popup()
        assert not completer.is_visible()

    def test_popup_hidden_no_match(self, qapp: object) -> None:
        edit = _make_edit("zzz")
        completer = ModCompleter(edit)
        completer.set_labels(_LABELS)
        completer.update_popup()
        assert not completer.is_visible()

    def test_enter_accepts_first_match(self, qapp: object) -> None:
        edit = _make_edit("alpha")
        completer = ModCompleter(edit)
        completer.set_labels(_LABELS)
        completer.update_popup()
        assert completer.is_visible()
        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier
        )
        assert completer.handle_key(event)
        assert edit.toPlainText().startswith("Alpha Mods")
        assert not completer.is_visible()

    def test_escape_dismisses_popup(self, qapp: object) -> None:
        edit = _make_edit("va")
        completer = ModCompleter(edit)
        completer.set_labels(_LABELS)
        completer.update_popup()
        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
        )
        assert completer.handle_key(event)
        assert not completer.is_visible()

    def test_keys_not_consumed_when_hidden(self, qapp: object) -> None:
        edit = _make_edit()
        completer = ModCompleter(edit)
        completer.set_labels(_LABELS)
        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier
        )
        assert not completer.handle_key(event)

    def test_set_labels_dedupes(self, qapp: object) -> None:
        edit = _make_edit()
        completer = ModCompleter(edit)
        completer.set_labels(["Alpha", "alpha", "Beta"])
        assert completer.labels() == ["Alpha", "Beta"]
