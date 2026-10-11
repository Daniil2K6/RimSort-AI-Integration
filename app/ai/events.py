"""Structured, Qt-free events describing one agent run.

Events are plain dataclasses flowing from the worker thread to the GUI via
a queued Qt signal. They power the live action journal and give every text
delta / tool call a stable correlation id so identical tool calls never
merge and late events from a finished run never leak into a new chat.

This module intentionally has no Qt imports so it stays unit-testable and
safe to import from headless MCP code.
"""

from __future__ import annotations

import itertools
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: Bump when the serialized shape of :class:`AgentEvent` changes incompatibly.
SCHEMA_VERSION = 1


class EventKind(str, Enum):
    """Every kind of event an agent run can emit."""

    RUN_STARTED = "run_started"
    MESSAGE_STARTED = "message_started"
    TEXT_DELTA = "text_delta"
    MESSAGE_COMPLETED = "message_completed"
    TOOL_STARTED = "tool_started"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    TOOL_PROGRESS = "tool_progress"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    GUI_ACTION_REQUESTED = "gui_action_requested"
    GUI_ACTION_COMPLETED = "gui_action_completed"
    GUI_ACTION_FAILED = "gui_action_failed"
    STATE_CHANGED = "state_changed"
    RUN_COMPLETED = "run_completed"
    RUN_CANCELLED = "run_cancelled"
    RUN_FAILED = "run_failed"


#: Kinds that terminate a run; no further events should follow them.
TERMINAL_KINDS = frozenset(
    {
        EventKind.RUN_COMPLETED,
        EventKind.RUN_CANCELLED,
        EventKind.RUN_FAILED,
        EventKind.MESSAGE_COMPLETED,
    }
)


class EventStatus(str, Enum):
    """Lifecycle status surfaced to the user (distinct from EventKind)."""

    REQUESTED = "requested"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DECLINED = "declined"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class AgentEvent:
    """One structured event from an agent run.

    ``seq`` is a per-run monotonically increasing counter so the GUI can
    drop out-of-order/late events. ``run_id`` scopes events to one user
    turn; a new turn gets a fresh ``run_id`` and ignores stale events.
    """

    run_id: str
    seq: int
    kind: EventKind
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    summary: str = ""
    round_id: int = 0
    message_id: str = ""
    tool_call_id: str = ""
    action_id: str = ""
    status: EventStatus | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Compact JSON-able form (used by the session journal and tests)."""
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "timestamp": round(self.timestamp, 3),
            "kind": self.kind.value,
            "round_id": self.round_id,
            "message_id": self.message_id,
        }
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        if self.action_id:
            data["action_id"] = self.action_id
        if self.status is not None:
            data["status"] = self.status.value
        if self.summary:
            data["summary"] = self.summary
        if self.payload:
            data["payload"] = self.payload
        return data


class EventEmitter:
    """Allocates ``run_id``/``seq`` and stamps :class:`AgentEvent` objects.

    A single emitter serves one run. ``emit`` forwards to a sink (the GUI
    bridges it onto a queued Qt signal). Terminal events flip ``finished``
    so callers can refuse to emit after a run ended.
    """

    def __init__(
        self,
        run_id: str,
        sink: Any = None,
        *,
        round_id: int = 0,
    ) -> None:
        self.run_id = run_id
        self._sink = sink
        self._counter = itertools.count()
        self.round_id = round_id
        self.message_id = ""
        self.finished = False
        self.last_seq = 0

    def next_seq(self) -> int:
        self.last_seq = next(self._counter)
        return self.last_seq

    def emit(
        self,
        kind: EventKind,
        summary: str = "",
        *,
        status: EventStatus | None = None,
        tool_call_id: str = "",
        action_id: str = "",
        message_id: str | None = None,
        round_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent:
        """Build, stamp and forward one event; returns it for the journal."""
        event = AgentEvent(
            run_id=self.run_id,
            seq=self.next_seq(),
            kind=kind,
            summary=summary,
            round_id=self.round_id if round_id is None else round_id,
            message_id=(self.message_id if message_id is None else message_id),
            tool_call_id=tool_call_id,
            action_id=action_id,
            status=status,
            payload=payload or {},
        )
        if kind in TERMINAL_KINDS:
            self.finished = True
        if self._sink is not None:
            self._sink(event)
        return event


def new_run_id() -> str:
    """A fresh, collision-resistant id for one user turn."""
    return uuid.uuid4().hex
