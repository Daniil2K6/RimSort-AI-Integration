"""Token-budget helpers for the AI chat context (Stage F).

The system prompt and the conversation are the two context costs the app
controls. This module estimates tokens cheaply and trims the *oldest*
conversation turns so a long chat stays within a budget without losing the
most recent, relevant exchange. The system prompt is always kept.
"""

from __future__ import annotations

import json
from typing import Any

#: Default context budget in approximate tokens (~32k chars).
DEFAULT_CONTEXT_BUDGET_TOKENS = 8000


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars/token plus paragraph breaks.

    Deliberately cheap and deterministic — good enough to bound context
    without a tokenizer dependency.
    """
    if not text:
        return 0
    base = len(text) // 4
    paragraphs = text.count("\n\n")
    return base + paragraphs + 1


def message_tokens(message: dict[str, Any]) -> int:
    """Approximate tokens for one chat message dict."""
    content = message.get("content") or ""
    tokens = estimate_tokens(str(content))
    # Arguments can contain a whole list: fixed overhead alone undercounts it.
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        tokens += estimate_tokens(json.dumps(tool_calls, ensure_ascii=False))
    tokens += 8
    return tokens


def total_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(message_tokens(m) for m in messages)


def trim_messages_to_budget(
    messages: list[dict[str, Any]],
    budget_tokens: int = DEFAULT_CONTEXT_BUDGET_TOKENS,
) -> tuple[list[dict[str, Any]], int]:
    """Trim oldest non-system messages until the list fits ``budget_tokens``.

    The leading system message (role="system") is always kept. Returns the
    trimmed list and the number of messages dropped. If everything already
    fits, returns the input unchanged with a drop count of 0.
    """
    if budget_tokens <= 0:
        return list(messages), 0
    if total_tokens(messages) <= budget_tokens:
        return list(messages), 0

    system = [m for m in messages if m.get("role") == "system"]
    conversation = [m for m in messages if m.get("role") != "system"]

    # A user turn includes all assistant tool calls and their results. Drop
    # whole turns so the API never receives an orphan result/call or loses
    # the question that gave rise to its most recent answer.
    groups: list[list[dict[str, Any]]] = []
    for message in conversation:
        if message.get("role") == "user" or not groups:
            groups.append([])
        groups[-1].append(message)
    kept_groups: list[list[dict[str, Any]]] = []
    budget_left = budget_tokens - total_tokens(system)
    for group in reversed(groups):
        cost = total_tokens(group)
        if cost > budget_left and kept_groups:
            break
        # The newest turn is required even when it exceeds the estimate.
        kept_groups.append(group)
        budget_left -= cost
    kept = [message for group in reversed(kept_groups) for message in group]
    dropped = len(conversation) - len(kept)
    return system + kept, dropped


__all__ = [
    "DEFAULT_CONTEXT_BUDGET_TOKENS",
    "estimate_tokens",
    "message_tokens",
    "total_tokens",
    "trim_messages_to_budget",
]
