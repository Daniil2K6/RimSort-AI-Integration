"""Shared, Qt-free value types for the AI subsystem.

Lives in its own module so both ``client.py`` (transport) and ``sse.py``
(stream parsing) can depend on it without forming an import cycle.
``client.py`` re-exports :class:`ToolCall` and :class:`AssistantReply`
so existing ``from app.ai.client import ToolCall`` call sites keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """One function call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistantReply:
    """Parsed `choices[0].message` from a chat completion."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: Provider-reported token usage, when the endpoint returns it.
    usage: dict[str, Any] = field(default_factory=dict)
    #: Tool calls whose arguments never parsed as a JSON object.
    incomplete_calls: list[ToolCall] = field(default_factory=list)
