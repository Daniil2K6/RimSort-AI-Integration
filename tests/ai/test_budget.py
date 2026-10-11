"""Tests for the AI context token budget (Stage F)."""

from __future__ import annotations

from app.ai.budget import (
    estimate_tokens,
    message_tokens,
    total_tokens,
    trim_messages_to_budget,
)


def _msg(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


def test_estimate_tokens_scales_with_length() -> None:
    assert estimate_tokens("") == 0
    short = estimate_tokens("hello world")
    long = estimate_tokens("hello world " * 100)
    assert 0 < short < long


def test_message_tokens_counts_tool_calls() -> None:
    plain = message_tokens(_msg("user", "hello"))
    with_call = message_tokens(
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}
    )
    assert with_call > plain


def test_no_trim_when_under_budget() -> None:
    messages = [_msg("system", "sys"), _msg("user", "hi"), _msg("assistant", "yo")]
    trimmed, dropped = trim_messages_to_budget(messages, budget_tokens=10_000)
    assert dropped == 0
    assert trimmed == messages


def test_trims_oldest_keeps_system_and_recent() -> None:
    system = _msg("system", "sys")
    old_flood = _msg("user", "old question " * 200)
    recent_user = _msg("user", "latest?")
    recent_assistant = _msg("assistant", "latest answer")
    messages = [system, old_flood, recent_user, recent_assistant]

    # Budget fits system + the recent pair but not the old flood.
    budget = total_tokens([system, recent_user, recent_assistant]) + 50
    trimmed, dropped = trim_messages_to_budget(messages, budget_tokens=budget)

    assert dropped == 1
    assert trimmed[0]["role"] == "system"  # system always kept
    assert trimmed[-1]["content"] == "latest answer"  # newest kept
    assert all("old question" not in (m["content"] or "") for m in trimmed)


def test_oversized_newest_message_is_kept() -> None:
    system = _msg("system", "sys")
    huge_newest = _msg("user", "x" * 40_000)
    messages = [system, huge_newest]
    trimmed, _dropped = trim_messages_to_budget(messages, budget_tokens=1000)
    # The newest message is kept (truncated context beats empty context).
    assert trimmed[-1]["content"] == huge_newest["content"]
    assert trimmed[0]["role"] == "system"
