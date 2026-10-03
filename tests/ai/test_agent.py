"""Tests for the AI agent loop, tool schemas and confirmation rules."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.ai.agent import (
    CANCELLED_TEXT,
    MAX_ROUNDS,
    build_agent_tools,
    call_tool_payload,
    requires_confirmation,
    run_agent,
    summarize_args,
)
from app.ai.client import AssistantReply, ToolCall
from app.mcp.context import MCPContext
from app.mcp.modops import current_active_ids
from app.mcp.server import build_server
from tests.mcp.conftest import MCPEnv
from tests.mcp.test_tools import EXPECTED_TOOLS

READ_TOOLS = (
    "get_status",
    "list_mods",
    "get_mod_details",
    "get_active_modlist",
    "validate_modlist",
    "load_modpack",
    "list_modpacks",
)

WRITE_TOOLS = (
    "set_active_modlist",
    "update_modlist",
    "apply_modpack",
    "save_modpack",
)


class ScriptedClient:
    """Deterministic ChatClient replaying pre-programmed replies."""

    def __init__(self, replies: list[AssistantReply]) -> None:
        self._replies = list(replies)
        self.seen_messages: list[list[dict[str, Any]]] = []
        self.seen_tools: list[list[dict[str, Any]] | None] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AssistantReply:
        self.seen_messages.append(list(messages))
        self.seen_tools.append(tools)
        if not self._replies:
            raise AssertionError("ScriptedClient ran out of replies")
        return self._replies.pop(0)


def run_scripted(
    ctx: MCPContext,
    replies: list[AssistantReply],
    *,
    confirm: Callable[[str], bool] | None = None,
    cancelled: bool = False,
) -> tuple[str, ScriptedClient, list[str], list[dict[str, Any]]]:
    """Run `run_agent` against a real server built from `ctx`."""
    client = ScriptedClient(replies)
    emitted: list[str] = []
    messages: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
    text = run_agent(
        client=client,
        server=build_server(ctx),
        messages=messages,
        confirm=confirm if confirm is not None else (lambda pretty: True),
        emit=emitted.append,
        is_cancelled=lambda: cancelled,
    )
    return text, client, emitted, messages


def tool_payloads(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payloads = [m for m in messages if m["role"] == "tool"]
    return [json.loads(m["content"]) for m in payloads]


def test_requires_confirmation_for_read_tools() -> None:
    for name in READ_TOOLS:
        assert not requires_confirmation(name, {}), name
    assert not requires_confirmation("unknown_tool", {"dry_run": False})


def test_requires_confirmation_for_write_tools() -> None:
    for name in WRITE_TOOLS:
        assert requires_confirmation(name, {"package_ids": ["ludeon.rimworld"]})


def test_requires_confirmation_dry_run_semantics() -> None:
    # sort_modlist defaults to a dry run -> no confirmation needed.
    assert not requires_confirmation("sort_modlist", {})
    assert not requires_confirmation("sort_modlist", {"dry_run": True})
    assert requires_confirmation("sort_modlist", {"dry_run": False})
    # launch_game defaults to a real launch -> confirmation always needed.
    assert requires_confirmation("launch_game", {})
    assert requires_confirmation("launch_game", {"dry_run": False})
    assert not requires_confirmation("launch_game", {"dry_run": True})


def test_build_agent_tools_matches_surface_without_set_instance(
    mcp_env: MCPEnv,
) -> None:
    tools = build_agent_tools(build_server(mcp_env.ctx))
    names = {entry["function"]["name"] for entry in tools}
    assert names == EXPECTED_TOOLS - {"set_instance"}
    for entry in tools:
        assert entry["type"] == "function"
        params = entry["function"]["parameters"]
        assert isinstance(params, dict)
        assert params.get("type") == "object"
        assert isinstance(entry["function"]["description"], str)


def test_call_tool_payload_success_and_unknown_tool(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    ok = call_tool_payload(server, "get_status", {"refresh": False})
    assert ok["instance"] == "Default"
    failed = call_tool_payload(server, "not_a_registered_tool", {})
    assert "error" in failed


def test_run_agent_reads_status_and_appends_history(mcp_env: MCPEnv) -> None:
    text, client, emitted, messages = run_scripted(
        mcp_env.ctx,
        [
            AssistantReply("", [ToolCall("c1", "get_status", {"refresh": False})]),
            AssistantReply("Everything looks fine", []),
        ],
    )
    assert text == "Everything looks fine"
    payloads = tool_payloads(messages)
    assert len(payloads) == 1
    assert payloads[0]["instance"] == "Default"
    assert messages[-1] == {"role": "assistant", "content": "Everything looks fine"}
    assert any("get_status" in line for line in emitted)
    assert client.seen_tools[0] is not None


def test_run_agent_declined_write_keeps_modlist(mcp_env: MCPEnv) -> None:
    before = current_active_ids(mcp_env.ctx)["mods"]
    text, _, emitted, messages = run_scripted(
        mcp_env.ctx,
        [
            AssistantReply(
                "",
                [ToolCall("c1", "set_active_modlist", {"package_ids": ["test.modb"]})],
            ),
            AssistantReply("Understood, nothing changed", []),
        ],
        confirm=lambda pretty: False,
    )
    assert text == "Understood, nothing changed"
    assert tool_payloads(messages)[0] == {"error": "Declined by user"}
    assert current_active_ids(mcp_env.ctx)["mods"] == before
    assert any("declined" in line for line in emitted)


def test_run_agent_approved_write_replaces_modlist(mcp_env: MCPEnv) -> None:
    text, _, _, messages = run_scripted(
        mcp_env.ctx,
        [
            AssistantReply(
                "",
                [
                    ToolCall(
                        "c1",
                        "set_active_modlist",
                        {"package_ids": ["ludeon.rimworld"]},
                    )
                ],
            ),
            AssistantReply("List replaced", []),
        ],
        confirm=lambda pretty: True,
    )
    assert text == "List replaced"
    assert "error" not in tool_payloads(messages)[0]
    mcp_env.ctx.refresh()
    assert current_active_ids(mcp_env.ctx)["mods"] == ["ludeon.rimworld"]


def test_run_agent_unknown_tool_feeds_error_back(mcp_env: MCPEnv) -> None:
    text, _, _, messages = run_scripted(
        mcp_env.ctx,
        [
            AssistantReply("", [ToolCall("c1", "missing_tool", {"x": 1})]),
            AssistantReply("Recovered after the error", []),
        ],
    )
    assert text == "Recovered after the error"
    assert "error" in tool_payloads(messages)[0]


def test_run_agent_cancelled_before_first_round(mcp_env: MCPEnv) -> None:
    client = ScriptedClient([])
    messages: list[dict[str, Any]] = []
    text = run_agent(
        client=client,
        server=build_server(mcp_env.ctx),
        messages=messages,
        confirm=lambda pretty: True,
        emit=lambda line: None,
        is_cancelled=lambda: True,
    )
    assert text == CANCELLED_TEXT
    assert client.seen_messages == []
    assert messages == []


def test_run_agent_stops_at_round_limit_and_summarises(mcp_env: MCPEnv) -> None:
    replies = [
        AssistantReply("", [ToolCall(f"c{i}", "get_status", {"refresh": False})])
        for i in range(MAX_ROUNDS)
    ]
    replies.append(AssistantReply("Final summary", []))

    text, client, _, messages = run_scripted(mcp_env.ctx, replies)

    assert text == "Final summary"
    assert len(client.seen_tools) == MAX_ROUNDS + 1
    assert client.seen_tools[-1] is None
    assert len(tool_payloads(messages)) == MAX_ROUNDS


def test_summarize_args_caps_length() -> None:
    summary = summarize_args(
        "update_modlist", {"add": [f"test.mod{i:03d}" for i in range(60)]}
    )
    assert summary.startswith("update_modlist(")
    assert summary.endswith("...)")
    assert len(summary) < 140
    assert summarize_args("get_status", {}) == "get_status()"
