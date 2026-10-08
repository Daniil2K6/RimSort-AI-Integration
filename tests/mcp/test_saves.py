"""Tests for list_game_saves / import_modlist (RimWorld save import)."""

from __future__ import annotations

import os

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.server import build_server
from tests.mcp.conftest import MCPEnv
from tests.mcp.test_tools import call

_SAVE_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<savegame>
 <meta>
  <gameVersion>{version}</gameVersion>
  <modIds>
{mod_ids}
  </modIds>
 </meta>
</savegame>
"""


def _write_save(env: MCPEnv, name: str, package_ids: list[str]) -> str:
    saves = env.config.parent / "Saves"
    saves.mkdir(parents=True, exist_ok=True)
    lis = "\n".join(f"    <li>{pid}</li>" for pid in package_ids)
    target = saves / name
    target.write_text(
        _SAVE_TEMPLATE.format(version="1.5.4104 rev1234", mod_ids=lis),
        encoding="utf-8",
    )
    return str(target)


def test_list_game_saves_empty(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    result = call(server, "list_game_saves")
    assert result["count"] == 0
    assert result["latest"] is None
    assert result["saves"] == []


def test_list_game_saves_newest_first(mcp_env: MCPEnv) -> None:
    old = _write_save(mcp_env, "old.rws", ["ludeon.rimworld"])
    new = _write_save(mcp_env, "new.rws", ["ludeon.rimworld"])
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))

    server = build_server(mcp_env.ctx)
    result = call(server, "list_game_saves")
    assert result["count"] == 2
    assert result["latest"] == "new.rws"
    assert [entry["name"] for entry in result["saves"]] == ["new.rws", "old.rws"]
    assert "modified" in result["saves"][0]


def test_import_modlist_latest_save(mcp_env: MCPEnv) -> None:
    _write_save(
        mcp_env,
        "colony.rws",
        ["ludeon.rimworld", "test.modb", "test.moda", "test.missing"],
    )
    server = build_server(mcp_env.ctx)
    result = call(server, "import_modlist")
    assert result["source_format"] == "rws"
    assert result["game_version"] == "1.5.4104 rev1234"
    assert result["package_ids"] == [
        "ludeon.rimworld",
        "test.modb",
        "test.moda",
        "test.missing",
    ]
    assert result["unknown"] == ["test.missing"]
    assert result["unknown_count"] == 1
    assert result["applied"] is False


def test_import_modlist_no_saves(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="No .rws saves"):
        call(server, "import_modlist")


def test_import_modlist_missing_file(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="Could not parse mod list"):
        call(server, "import_modlist", {"path": "/does/not/exist.rws"})


def test_import_modlist_apply_writes_config(mcp_env: MCPEnv) -> None:
    path = _write_save(
        mcp_env, "colony.rws", ["ludeon.rimworld", "test.modb", "test.moda"]
    )
    server = build_server(mcp_env.ctx)
    result = call(server, "import_modlist", {"path": path, "apply": True})
    assert result["applied"] is True
    assert result["written"] == 3
    assert result["backup"] is not None
    assert mcp_env.active_ids() == [
        "ludeon.rimworld",
        "test.modb",
        "test.moda",
    ]


def test_import_modlist_apply_rejects_unknown(mcp_env: MCPEnv) -> None:
    path = _write_save(mcp_env, "colony.rws", ["test.moda", "test.nope"])
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="Unknown packageIds"):
        call(server, "import_modlist", {"path": path, "apply": True})
    # Active list untouched.
    assert mcp_env.active_ids() == [
        "ludeon.rimworld",
        "ludeon.rimworld.royalty",
        "test.modb",
        "test.moda",
    ]


def test_import_modlist_apply_allow_missing(mcp_env: MCPEnv) -> None:
    path = _write_save(mcp_env, "colony.rws", ["test.moda", "test.nope"])
    server = build_server(mcp_env.ctx)
    result = call(
        server,
        "import_modlist",
        {"path": path, "apply": True, "allow_missing": True},
    )
    assert result["applied"] is True
    assert mcp_env.active_ids() == ["test.moda", "test.nope"]
