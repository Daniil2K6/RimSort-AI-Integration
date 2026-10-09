"""JSON-file persistence for AI chat sessions.

Each chat is one JSON file in ``<app storage>/ai_chats/<id>.json``.
Only user/assistant messages are persisted; transient activity and
error lines live for the session only.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.utils.json_utils import atomic_json_dump


@dataclass
class ChatMessage:
    """One persisted chat message."""

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatMessage:
        return cls(role=str(data.get("role", "")), content=str(data.get("content", "")))


@dataclass
class ChatSession:
    """One chat conversation, backed by a single JSON file."""

    id: str
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    messages: list[ChatMessage] = field(default_factory=list)
    provider_id: str = ""
    model: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.messages

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [message.to_dict() for message in self.messages],
            "provider_id": self.provider_id,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatSession:
        raw_messages = data.get("messages")
        messages: list[ChatMessage] = []
        if isinstance(raw_messages, list):
            for item in raw_messages:
                if isinstance(item, dict) and item.get("content"):
                    messages.append(ChatMessage.from_dict(item))
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            title=str(data.get("title", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            messages=messages,
            provider_id=str(data.get("provider_id", "")),
            model=str(data.get("model", "")),
        )


def make_title(text: str) -> str:
    """Derive a short single-line title from the first user message."""
    collapsed = " ".join(text.split())
    return collapsed[:48] + ("…" if len(collapsed) > 48 else "")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ChatStore:
    """Load/save/delete chat sessions as JSON files."""

    def __init__(self, folder: Path | None = None) -> None:
        if folder is None:
            from app.utils.app_info import AppInfo

            folder = AppInfo().app_storage_folder / "ai_chats"
        self._folder = folder

    @property
    def folder(self) -> Path:
        return self._folder

    def new_session(self) -> ChatSession:
        """Create an unsaved in-memory session (not persisted until used)."""
        now = _now()
        return ChatSession(id=uuid.uuid4().hex, created_at=now, updated_at=now)

    def save(self, session: ChatSession) -> Path:
        """Persist the session, creating the folder when missing."""
        self._folder.mkdir(parents=True, exist_ok=True)
        session.updated_at = _now()
        path = self._folder / f"{session.id}.json"
        atomic_json_dump(session.to_dict(), str(path), indent=2)
        return path

    def load(self, chat_id: str) -> ChatSession | None:
        """Load one session by id, returning ``None`` when missing/broken."""
        path = self._folder / f"{chat_id}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("AI chat store: could not read {}", path)
            return None
        if not isinstance(data, dict):
            logger.warning("AI chat store: unexpected contents in {}", path)
            return None
        return ChatSession.from_dict(data)

    def list_sessions(self) -> list[ChatSession]:
        """All readable sessions, newest first by creation time.

        Sorting by ``created_at`` (not ``updated_at``) keeps the history
        list stable: opening or sending in a chat never reshuffles it.
        """
        if not self._folder.is_dir():
            return []
        sessions: list[ChatSession] = []
        for path in self._folder.glob("*.json"):
            session = self.load(path.stem)
            if session is not None:
                sessions.append(session)
        sessions.sort(
            key=lambda session: (session.created_at, session.id), reverse=True
        )
        return sessions

    def delete(self, chat_id: str) -> bool:
        """Delete one session file; returns whether it existed."""
        path = self._folder / f"{chat_id}.json"
        existed = path.is_file()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("AI chat store: could not delete {}", path)
            return False
        return existed
