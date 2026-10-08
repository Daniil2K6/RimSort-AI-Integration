"""Tests for JSON chat session persistence (app.ai.chat_store)."""

from __future__ import annotations

import json
from pathlib import Path

from app.ai.chat_store import ChatMessage, ChatStore, make_title


def test_new_session_is_not_listed(tmp_path: Path) -> None:
    """An unsaved session must not appear in the store listing."""
    store = ChatStore(tmp_path / "chats")
    session = store.new_session()
    assert session.is_empty
    assert store.list_sessions() == []


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """A saved session reloads with messages, title and provider info."""
    store = ChatStore(tmp_path)
    session = store.new_session()
    session.title = "Hello"
    session.messages.append(ChatMessage(role="user", content="hi"))
    session.messages.append(ChatMessage(role="assistant", content="yo"))
    session.provider_id = "provider-1"
    session.model = "model-1"

    path = store.save(session)
    assert path.is_file()

    loaded = store.load(session.id)
    assert loaded is not None
    assert loaded.title == "Hello"
    assert [m.content for m in loaded.messages] == ["hi", "yo"]
    assert loaded.provider_id == "provider-1"
    assert loaded.model == "model-1"


def test_list_orders_by_updated_at_desc(tmp_path: Path) -> None:
    """Sessions are listed most-recently-updated first."""
    (tmp_path / "older.json").write_text(
        json.dumps(
            {
                "id": "older",
                "title": "Older",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "messages": [],
            }
        )
    )
    (tmp_path / "newer.json").write_text(
        json.dumps(
            {
                "id": "newer",
                "title": "Newer",
                "updated_at": "2026-01-02T00:00:00+00:00",
                "messages": [],
            }
        )
    )

    store = ChatStore(tmp_path)
    assert [s.id for s in store.list_sessions()] == ["newer", "older"]


def test_corrupt_and_non_object_files_are_skipped(tmp_path: Path) -> None:
    """Broken JSON files never break the listing."""
    (tmp_path / "broken.json").write_text("not json at all")
    (tmp_path / "list.json").write_text("[]")

    store = ChatStore(tmp_path)
    assert store.list_sessions() == []
    assert store.load("broken") is None


def test_delete(tmp_path: Path) -> None:
    """Delete removes the file and reports whether it existed."""
    store = ChatStore(tmp_path)
    session = store.new_session()
    session.messages.append(ChatMessage(role="user", content="hi"))
    store.save(session)

    assert store.delete(session.id) is True
    assert store.load(session.id) is None
    assert store.delete(session.id) is False


def test_make_title_collapses_and_truncates() -> None:
    """Titles are single-line and capped at 48 characters."""
    assert make_title("hello") == "hello"
    assert make_title("a\n\n  b") == "a b"
    long_title = make_title("x" * 100)
    assert len(long_title) == 49  # 48 chars + ellipsis
    assert long_title.endswith("…")
