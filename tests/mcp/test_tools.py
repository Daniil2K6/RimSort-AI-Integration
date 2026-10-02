"""Tests for MCP tools (invoked through the MCPServer API)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.context import MCPContext
from app.mcp.server import build_server
from tests.mcp.conftest import MCPEnv

EXPECTED_TOOLS = {
    "apply_modpack",
    "get_active_modlist",
    "get_mod_details",
    "get_status",
    "launch_game",
    "list_modpacks",
    "list_mods",
    "load_modpack",
    "save_modpack",
    "set_active_modlist",
    "set_instance",
    "sort_modlist",
    "update_modlist",
    "validate_modlist",
}


def call(server: Any, name: str, args: dict[str, Any] | None = None) -> Any:
    """Call a tool and return its JSON payload (raises ToolError on failure)."""
    result = asyncio.run(server.call_tool(name, args or {}))
    if result.is_error:
        raise ToolError(result.content[0].text)
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    return json.loads(result.content[0].text)


def test_tool_surface(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


def test_get_status(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    status = call(server, "get_status")
    assert status["instance"] == "Default"
    assert status["game_version"] == "1.5.4104 rev1234"
    assert status["mods"]["listed"] == 11
    assert status["mods"]["active"] == 4
    assert status["mods_config_exists"] is True
    assert status["paths"]["game"] == str(mcp_env.game)


def test_get_status_refresh(mcp_env: MCPEnv) -> None:
    from tests.mcp.conftest import write_mod

    write_mod(mcp_env.mods, "NewMod", "test.newmod", "New Mod")
    server = build_server(mcp_env.ctx)
    before = call(server, "get_status")
    assert before["mods"]["listed"] == 11
    after = call(server, "get_status", {"refresh": True})
    assert after["mods"]["listed"] == 12


def test_list_mods_query_and_pagination(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    result = call(server, "list_mods", {"query": "alpha"})
    assert result["total"] == 1
    assert result["mods"][0]["id"] == "test.moda"

    page = call(server, "list_mods", {"limit": 2, "offset": 0})
    assert page["returned"] == 2
    assert page["total"] == 11

    paged = call(server, "list_mods", {"limit": 2, "offset": 10})
    assert paged["returned"] == 1


def test_list_mods_filters(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    active = call(server, "list_mods", {"active_only": True})
    active_ids = {entry["id"] for entry in active["mods"]}
    assert active_ids == {
        "ludeon.rimworld",
        "ludeon.rimworld.royalty",
        "test.modb",
        "test.moda",
    }

    inactive = call(server, "list_mods", {"inactive_only": True})
    assert "test.workshop" in {entry["id"] for entry in inactive["mods"]}
    assert "test.moda" not in {entry["id"] for entry in inactive["mods"]}

    by_source = call(server, "list_mods", {"source": "Steam Workshop"})
    assert by_source["total"] == 2


def test_get_mod_details(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    details = call(server, "get_mod_details", {"package_id": "test.moda"})
    assert details["name"] == "Alpha Mod"
    assert details["active"] is True
    assert details["source"] == "Local"
    assert details["rules"]["load_after"] == ["test.modb"]
    assert details["rules"]["incompatible_with"] == ["test.conflict"]
    assert details["supported_versions"] == ["1.5"]


def test_get_mod_details_duplicate_packageid(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    result = call(server, "get_mod_details", {"package_id": "test.dup"})
    assert result["duplicate_count"] == 2
    assert len(result["matches"]) == 2


def test_get_mod_details_unknown_raises(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="No installed mod"):
        call(server, "get_mod_details", {"package_id": "no.such.mod"})


def test_get_and_set_active_modlist(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    before = call(server, "get_active_modlist")
    assert before["count"] == 4

    result = call(
        server,
        "set_active_modlist",
        {"package_ids": ["ludeon.rimworld", "test.modb", "test.moda"]},
    )
    assert result["written"] == 3
    after = call(server, "get_active_modlist")
    assert after["mods"] == ["ludeon.rimworld", "test.modb", "test.moda"]


def test_set_active_modlist_unknown_raises(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="Unknown packageIds"):
        call(
            server,
            "set_active_modlist",
            {"package_ids": ["ludeon.rimworld", "nope.mod"]},
        )


def test_update_modlist_tool(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    result = call(
        server,
        "update_modlist",
        {"add": ["test.workshop"], "remove": ["test.moda"]},
    )
    assert result["added"] == ["test.workshop"]
    assert result["removed"] == ["test.moda"]
    active = call(server, "get_active_modlist")
    assert "test.workshop" in active["mods"]
    assert "test.moda" not in active["mods"]


def test_validate_modlist_tool(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    report = call(server, "validate_modlist")
    assert report["ok"] is True

    call(
        server,
        "set_active_modlist",
        {"package_ids": ["ludeon.rimworld", "test.moda", "test.modb"]},
    )
    report = call(server, "validate_modlist")
    assert report["ok"] is False
    assert report["order_violations"]["count"] == 1


def test_sort_modlist_dry_run_does_not_write(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    original = mcp_env.mods_config.read_bytes()
    call(
        server,
        "set_active_modlist",
        {"package_ids": ["ludeon.rimworld", "test.moda", "test.modb"]},
    )
    written = mcp_env.mods_config.read_bytes()
    assert written != original

    result = call(server, "sort_modlist", {"dry_run": True})
    assert result["dry_run"] is True
    assert "sorted" in result
    assert mcp_env.mods_config.read_bytes() == written


def test_sort_modlist_apply_writes(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    call(
        server,
        "set_active_modlist",
        {"package_ids": ["ludeon.rimworld", "test.moda", "test.modb"]},
    )
    result = call(server, "sort_modlist", {"dry_run": False})
    assert result["written"] == 3
    assert result["backup"] is not None
    active = call(server, "get_active_modlist")
    ids = active["mods"]
    assert ids.index("test.modb") < ids.index("test.moda")


def test_sort_modlist_cycle_is_error(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    call(
        server,
        "set_active_modlist",
        {"package_ids": ["ludeon.rimworld", "test.cycle1", "test.cycle2"]},
    )
    with pytest.raises(ToolError, match="circular dependencies"):
        call(server, "sort_modlist", {"dry_run": True})


def test_modpack_tools_roundtrip(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    saved = call(server, "save_modpack", {"name": "Tool Pack"})
    assert saved["count"] == 4

    listing = call(server, "list_modpacks")
    assert "Tool Pack" in [entry["name"] for entry in listing["modpacks"]]

    loaded = call(server, "load_modpack", {"name": "Tool Pack"})
    assert loaded["package_ids"][0] == "ludeon.rimworld"

    call(server, "set_active_modlist", {"package_ids": ["ludeon.rimworld"]})
    applied = call(server, "apply_modpack", {"name": "Tool Pack"})
    assert applied["count"] == 4
    active = call(server, "get_active_modlist")
    assert active["count"] == 4


def test_launch_game_dry_run_direct(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    result = call(server, "launch_game", {"dry_run": True})
    assert result["dry_run"] is True
    assert result["method"] == "direct"
    assert "RimWorld" in result["executable"]
    assert "launched" not in result
    assert "pid" not in result


def test_launch_game_dry_run_steam_protocol(mcp_env: MCPEnv) -> None:
    mcp_env.write_settings(
        launch_via_steam_protocol=True, steam_client_integration=True
    )
    server = build_server(mcp_env.ctx)
    result = call(server, "launch_game", {"dry_run": True})
    assert result["method"] == "steam_protocol"
    assert result["uri"] == "steam://rungameid/294100"


def test_launch_game_steam_without_integration_errors(
    mcp_env: MCPEnv,
) -> None:
    mcp_env.write_settings(
        launch_via_steam_protocol=True, steam_client_integration=False
    )
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="Steam Client Integration"):
        call(server, "launch_game", {"dry_run": True})


def test_launch_game_missing_game_folder(mcp_env: MCPEnv) -> None:
    mcp_env.write_settings(game_folder=str(mcp_env.tmp_path / "nowhere.app"))
    ctx = MCPContext(settings_path=mcp_env.settings_path)
    server = build_server(ctx)
    with pytest.raises(ToolError, match="Game folder does not exist"):
        call(server, "launch_game", {"dry_run": True})


def test_set_instance_tool(mcp_env: MCPEnv) -> None:
    settings = json.loads(mcp_env.settings_path.read_text(encoding="utf-8"))
    second = dict(settings["instances"]["Default"])
    second["name"] = "Alt"
    settings["instances"]["Alt"] = second
    mcp_env.settings_path.write_text(json.dumps(settings), encoding="utf-8")

    server = build_server(mcp_env.ctx)
    status = call(server, "set_instance", {"name": "Alt"})
    assert status["instance"] == "Alt"
    assert "Alt" in status["instances"]
