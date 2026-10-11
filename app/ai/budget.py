"""Token-budget helpers for the AI chat context (Stage F).

The system prompt and the conversation are the two context costs the app
controls. This module estimates tokens cheaply and trims the *oldest*
conversation turns so a long chat stays within a budget without losing the
most recent, relevant exchange. The system prompt is always kept.
"""

from __future__ import annotations

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
    # Tool calls add a small fixed overhead per call.
    tool_calls = message.get("tool_calls") or []
    tokens += 32 * len(tool_calls)
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

    kept: list[dict[str, Any]] = []
    dropped = 0
    # Walk newest -> oldest, keeping a suffix that fits the remaining budget.
    budget_left = budget_tokens - total_tokens(system)
    for message in reversed(conversation):
        cost = message_tokens(message)
        if cost > budget_left and kept:
            # Stop at the first message that no longer fits (keep pairs intact
            # by not dropping a lone assistant reply that follows a kept user).
            break
        if cost > budget_left and not kept:
            # A single oversized newest message: keep it anyway (truncated
            # content is better than an empty context) and drop the rest.
            kept.append(message)
            budget_left -= cost
            continue
        kept.append(message)
        budget_left -= cost
    kept.reverse()
    dropped = len(conversation) - len(kept)
    return system + kept, dropped


__all__ = [
    "DEFAULT_CONTEXT_BUDGET_TOKENS",
    "estimate_tokens",
    "message_tokens",
    "total_tokens",
    "trim_messages_to_budget",
]
