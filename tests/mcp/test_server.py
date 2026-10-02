"""Server-level tests: registration surface and a real stdio session."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent, TextResourceContents

from app.mcp.server import build_server, run_stdio
from tests.mcp.conftest import MCPEnv
from tests.mcp.test_tools import EXPECTED_TOOLS, call

_STDIO_TIMEOUT = 90


def _tool_text(result: Any) -> str:
    """Return the text payload of a CallToolResult (mypy-safe)."""
    item = result.content[0]
    if not isinstance(item, TextContent):
        raise TypeError(f"expected TextContent, got {type(item).__name__}")
    return item.text


def _server_resource_text(server: Any, uri: str) -> str:
    """Read a server-side resource and return its text body (mypy-safe)."""
    result: Any = asyncio.run(server.read_resource(uri))
    contents: list[Any] = list(result)
    body: Any = contents[0].content
    if not isinstance(body, str):
        raise TypeError("expected text resource body")
    return body


def _client_resource_payload(result: Any) -> Any:
    """Parse a client-side ReadResourceResult into JSON (mypy-safe)."""
    item = result.contents[0]
    if not isinstance(item, TextResourceContents):
        raise TypeError(f"expected TextResourceContents, got {type(item).__name__}")
    return json.loads(item.text)


def test_registration_surface(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)

    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    # Descriptions exist for every tool (agents rely on them).
    for tool in tools:
        assert tool.description, f"tool {tool.name} has no description"

    prompts = asyncio.run(server.list_prompts())
    assert {prompt.name for prompt in prompts} == {
        "assemble_modpack",
        "troubleshoot_active_list",
    }

    resources = asyncio.run(server.list_resources())
    assert {str(resource.uri) for resource in resources} == {
        "rimsort://status",
        "rimsort://modlist/active",
    }


def test_resource_reads(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    payload = json.loads(_server_resource_text(server, "rimsort://status"))
    assert payload["instance"] == "Default"

    payload = json.loads(_server_resource_text(server, "rimsort://modlist/active"))
    assert payload["count"] == 4


def test_stdio_end_to_end(mcp_env: MCPEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn `python -m app mcp` and drive a full MCP session."""
    from app.mcp.context import SETTINGS_ENV_VAR

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(mcp_env.settings_path))
    project_root = Path(__file__).resolve().parents[2]

    async def session_flow() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "app", "mcp", "--log-level", "WARNING"],
            cwd=str(project_root),
            env=dict(os.environ),
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            init = await session.initialize()
            assert init.server_info.name == "rimsort"

            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS

            status = await session.call_tool("get_status", {})
            assert status.is_error is False
            payload = json.loads(_tool_text(status))
            assert payload["instance"] == "Default"
            assert payload["game_version"] == "1.5.4104 rev1234"

            active = await session.call_tool("get_active_modlist", {})
            assert active.is_error is False
            active_payload = json.loads(_tool_text(active))
            assert active_payload["count"] == 4

            error = await session.call_tool(
                "get_mod_details", {"package_id": "no.such.mod"}
            )
            assert error.is_error is True
            assert "No installed mod" in _tool_text(error)

            prompts = await session.list_prompts()
            assert {prompt.name for prompt in prompts.prompts} == {
                "assemble_modpack",
                "troubleshoot_active_list",
            }

            resources = await session.list_resources()
            assert {str(r.uri) for r in resources.resources} == {
                "rimsort://status",
                "rimsort://modlist/active",
            }

            resource = await session.read_resource("rimsort://status")
            resource_payload = _client_resource_payload(resource)
            assert resource_payload["instance"] == "Default"

    asyncio.run(asyncio.wait_for(session_flow(), timeout=_STDIO_TIMEOUT))


def test_run_stdio_requires_valid_log_level(mcp_env: MCPEnv) -> None:
    # Smoke-check the entry point wiring without actually blocking on
    # stdio: run_stdio(log_level) only configures logging before run(),
    # so patch run() away.
    from unittest.mock import patch

    from app.mcp.context import SETTINGS_ENV_VAR

    os_env_backup = os.environ.get(SETTINGS_ENV_VAR)
    os.environ[SETTINGS_ENV_VAR] = str(mcp_env.settings_path)
    try:
        with patch("app.mcp.server.build_server") as mocked_build:
            run_stdio("WARNING")
        mocked_build.assert_called_once()
    finally:
        if os_env_backup is None:
            os.environ.pop(SETTINGS_ENV_VAR, None)
        else:
            os.environ[SETTINGS_ENV_VAR] = os_env_backup


def test_cli_mcp_registered() -> None:
    from click.testing import CliRunner

    from app.cli.main import cli

    result = CliRunner().invoke(cli, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "MCP" in result.output


def test_expected_errors_are_tool_errors(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError):
        call(server, "load_modpack", {"name": "Missing Pack"})
