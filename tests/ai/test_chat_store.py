"""Tests for JSON chat session persistence (app.ai.chat_store)."""

from __future__ import annotations

import json
from pathlib import Path

from app.ai.chat_store import ChatMessage, ChatSession, ChatStore, make_title


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


def test_session_serializes_current_schema_version(tmp_path: Path) -> None:
    store = ChatStore(tmp_path)
    session = store.new_session()
    session.messages.append(ChatMessage(role="user", content="hi"))
    path = store.save(session)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert session.schema_version == 2


def test_tool_calls_roundtrip_not_dropped(tmp_path: Path) -> None:
    """Stage F: assistant tool_calls and tool results survive save/load."""
    store = ChatStore(tmp_path)
    session = store.new_session()
    session.messages.append(ChatMessage(role="user", content="list mods"))
    session.messages.append(
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[{"id": "call_1", "function": {"name": "list_mods"}}],
        )
    )
    session.messages.append(
        ChatMessage(role="tool", content="[]", tool_call_id="call_1", name="list_mods")
    )
    session.messages.append(ChatMessage(role="assistant", content="done"))

    store.save(session)
    loaded = store.load(session.id)
    assert loaded is not None
    roles = [m.role for m in loaded.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    tool_msg = loaded.messages[2]
    assert tool_msg.tool_call_id == "call_1"
    assert tool_msg.name == "list_mods"
    call_msg = loaded.messages[1]
    assert call_msg.tool_calls is not None
    assert call_msg.tool_calls[0]["id"] == "call_1"


def test_legacy_v1_session_still_loads(tmp_path: Path) -> None:
    """A v1 file (no schema_version, content-only) loads as a legacy session."""
    store = ChatStore(tmp_path)
    legacy = {
        "id": "legacy1",
        "title": "Old",
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "yo"},
            # A v1 empty-content tool row would have been dropped; ensure a
            # message with content still loads.
        ],
        "provider_id": "",
        "model": "",
    }
    path = tmp_path / "legacy1.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = store.load("legacy1")
    assert loaded is not None
    assert [m.content for m in loaded.messages] == ["hi", "yo"]
    assert loaded.title == "Old"


def test_list_orders_by_created_at_desc(tmp_path: Path) -> None:
    """Sessions are listed newest-first by creation time."""
    (tmp_path / "older.json").write_text(
        json.dumps(
            {
                "id": "older",
                "title": "Older",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-03T00:00:00+00:00",
                "messages": [],
            }
        )
    )
    (tmp_path / "newer.json").write_text(
        json.dumps(
            {
                "id": "newer",
                "title": "Newer",
                "created_at": "2026-01-02T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "messages": [],
            }
        )
    )

    store = ChatStore(tmp_path)
    # "older" was updated more recently but must stay below "newer".
    assert [s.id for s in store.list_sessions()] == ["newer", "older"]


def test_list_order_survives_saving_an_open_session(tmp_path: Path) -> None:
    """Bumping updated_at by saving never reshuffles the sidebar list."""
    store = ChatStore(tmp_path)
    older = ChatSession(
        id="older", created_at="2026-01-01T00:00:00+00:00", updated_at=""
    )
    newer = ChatSession(
        id="newer", created_at="2026-01-02T00:00:00+00:00", updated_at=""
    )
    store.save(older)
    store.save(newer)

    store.save(older)  # re-saving bumps updated_at only

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
