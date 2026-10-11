"""Tests for Stage E analysis tools (dependencies / conflicts / log / batch)."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.server import build_server
from tests.mcp.conftest import MCPEnv
from tests.mcp.test_tools import call


def test_resolve_dependencies_load_after(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    result = call(
        server,
        "resolve_dependencies",
        {"package_ids": ["test.moda"], "include_load_after": True},
    )
    deps = {entry["dependency"] for entry in result["dependencies"]}
    assert "test.modb" in deps
    # test.modb is installed and already active in the fixture.
    modb = next(e for e in result["dependencies"] if e["dependency"] == "test.modb")
    assert modb["installed"] is True
    assert modb["active"] is True
    assert result["missing_from_active"] == []


def test_resolve_dependencies_requires_ids(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="must not be empty"):
        call(server, "resolve_dependencies", {"package_ids": []})


def test_find_conflicts_explicit_pair(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    result = call(
        server,
        "find_conflicts",
        {"package_ids": ["test.moda", "test.conflict"]},
    )
    assert result["count"] == 1
    assert result["conflicting_pairs"] == [["test.conflict", "test.moda"]]


def test_find_conflicts_default_working_is_clean(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    # Default active list has no incompatible pair.
    result = call(server, "find_conflicts")
    assert result["count"] == 0
    assert result["conflicting_pairs"] == []


def test_analyze_player_log_not_found(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    result = call(server, "analyze_player_log")
    assert result["ok"] is False
    assert result["code"] == "log_not_found"


def test_analyze_player_log_parses_exceptions(mcp_env: MCPEnv) -> None:
    log_path = mcp_env.config.parent / "Player.log"
    log_path.write_text(
        "[some header line]\n"
        "NullReferenceException: Object reference not set\n"
        "  at MyMod.Hooks.Tick () [0x00000]\n"
        "  at RimWorld.GameComponentUtilityTick () [0x00000]\n"
        "  at /workspace/Mods/MyBrokenMod/Source/Hooks.cs:42\n"
        "[next section]\n",
        encoding="utf-8",
    )
    server = build_server(mcp_env.ctx)
    result = call(server, "analyze_player_log")
    assert result["ok"] is True
    assert result["error_count"] == 1
    block = result["errors"][0]
    assert "NullReferenceException" in block["message"]
    assert "MyBrokenMod" in block["mod_folders"]


def test_player_log_candidates_include_macos_logs(tmp_path: Path) -> None:
    """On macOS the log lives under Logs, not Application Support."""
    from types import SimpleNamespace

    from app.mcp.analysis import _player_log_candidates

    config = (
        tmp_path
        / "Library/Application Support/Ludeon Studio/RimWorld by Ludeon Studios"
    )
    ctx = SimpleNamespace(instance=SimpleNamespace(config_folder=str(config)))
    candidates = [str(p) for p in _player_log_candidates(ctx)]  # type: ignore[arg-type]
    assert any("Logs" in p and p.endswith("Player.log") for p in candidates)


def test_analyze_player_log_finds_macos_logs_path(
    mcp_env: MCPEnv, tmp_path: Path
) -> None:
    """The analyzer picks the macOS Logs copy when the config sibling is absent."""
    from app.mcp import analysis

    config = tmp_path / "mac/Library/Application Support/Ludeon Studio/RW Studios"
    logs = tmp_path / "mac/Library/Logs/Ludeon Studio/RW Studios"
    logs.mkdir(parents=True)
    (logs / "Player.log").write_text(
        "FormatException: bad thing\n  at MyMod.Do () [0x1]\n[x]\n",
        encoding="utf-8",
    )
    mcp_env.write_settings(config_folder=str(config))
    result = analysis.analyze_player_log(mcp_env.ctx)
    assert result["ok"] is True
    assert "Logs" in result["path"]


def test_apply_modlist_batch_one_atomic_commit(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    before_rev = call(server, "get_modlist_state")["revision"]
    result = call(
        server,
        "apply_modlist_batch",
        {
            "ops": [
                {"op": "remove", "package_id": "test.moda"},
                {"op": "add", "package_id": "test.workshop"},
                {"op": "move", "package_id": "test.modb", "move_to_index": 0},
            ]
        },
    )
    assert result["ok"] is True
    assert result["count"] == 3
    # Exactly one revision bump for the whole batch.
    assert result["revision"] == before_rev + 1
    active = call(server, "get_active_modlist")["mods"]
    assert "test.workshop" in active
    assert "test.moda" not in active
    assert active[0] == "test.modb"
    assert result["state"]["dirty"] is False


def test_apply_modlist_batch_revision_interlock(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    call(server, "apply_modlist_plan", {"package_ids": ["ludeon.rimworld"]})
    rejected = call(
        server,
        "apply_modlist_batch",
        {
            "ops": [{"op": "add", "package_id": "test.modc"}],
            "expected_revision": 0,
        },
    )
    assert rejected["ok"] is False
    assert rejected["code"] == "revision_mismatch"


def test_apply_modlist_batch_unknown_raises(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="Unknown packageIds"):
        call(
            server,
            "apply_modlist_batch",
            {"ops": [{"op": "add", "package_id": "nope.mod"}]},
        )


def test_apply_modlist_batch_empty_ops_raises(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="must not be empty"):
        call(server, "apply_modlist_batch", {"ops": []})


def test_tool_surface_includes_stage_e(mcp_env: MCPEnv) -> None:
    import asyncio

    server = build_server(mcp_env.ctx)
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert {
        "resolve_dependencies",
        "find_conflicts",
        "analyze_player_log",
        "apply_modlist_batch",
    } <= names
