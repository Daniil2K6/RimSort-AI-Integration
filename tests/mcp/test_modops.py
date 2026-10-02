"""Tests for app.mcp.modops (validation, sorting, updates, modpacks)."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp import modops
from app.utils.app_info import AppInfo
from tests.mcp.conftest import MCPEnv


def test_current_active_ids(mcp_env: MCPEnv) -> None:
    result = modops.current_active_ids(mcp_env.ctx)
    assert result["mods"] == [
        "ludeon.rimworld",
        "ludeon.rimworld.royalty",
        "test.modb",
        "test.moda",
    ]
    assert result["count"] == 4
    assert result["missing"] == []


def test_validate_clean_list(mcp_env: MCPEnv) -> None:
    report = modops.validate_active_list(mcp_env.ctx)
    assert report["ok"] is True
    assert report["missing"]["count"] == 0
    assert report["duplicates"]["count"] == 0
    assert report["order_violations"]["count"] == 0
    assert report["incompatible_pairs"]["count"] == 0
    assert report["cycles"]["count"] == 0


def test_validate_detects_order_violation(mcp_env: MCPEnv) -> None:
    # test.moda must load after test.modb, so put moda first.
    mcp_env.ctx.write_active_config(["ludeon.rimworld", "test.moda", "test.modb"])
    report = modops.validate_active_list(mcp_env.ctx)
    assert report["ok"] is False
    items = report["order_violations"]["items"]
    assert report["order_violations"]["count"] == 1
    assert items[0]["mod"] == "test.moda"
    assert items[0]["must_load_before_is_at"] == "test.modb"


def test_validate_detects_missing(mcp_env: MCPEnv) -> None:
    mcp_env.ctx.write_active_config(["ludeon.rimworld", "test.modb", "ghost.mod"])
    report = modops.validate_active_list(mcp_env.ctx)
    assert report["ok"] is False
    assert report["missing"]["items"] == ["ghost.mod"]


def test_validate_detects_duplicates(mcp_env: MCPEnv) -> None:
    mcp_env.ctx.write_active_config(["ludeon.rimworld", "test.modb", "test.modb"])
    report = modops.validate_active_list(mcp_env.ctx)
    assert report["ok"] is False
    assert report["duplicates"]["items"] == ["test.modb"]


def test_validate_detects_incompatible_pair(mcp_env: MCPEnv) -> None:
    mcp_env.ctx.write_active_config(
        ["ludeon.rimworld", "test.modb", "test.moda", "test.conflict"]
    )
    report = modops.validate_active_list(mcp_env.ctx)
    assert report["ok"] is False
    assert report["incompatible_pairs"]["count"] == 1
    pair = report["incompatible_pairs"]["items"][0]
    assert set(pair) == {"test.moda", "test.conflict"}


def test_validate_detects_cycle(mcp_env: MCPEnv) -> None:
    mcp_env.ctx.write_active_config(["ludeon.rimworld", "test.cycle1", "test.cycle2"])
    report = modops.validate_active_list(mcp_env.ctx)
    assert report["ok"] is False
    assert report["cycles"]["count"] == 1
    assert "test.cycle1" in report["cycles"]["items"][0]


def test_sort_topological_fixes_order(mcp_env: MCPEnv) -> None:
    mcp_env.ctx.write_active_config(["ludeon.rimworld", "test.moda", "test.modb"])
    result = modops.sort_active_list(mcp_env.ctx, method="Topological")
    assert result["method"] == "Topological"
    sorted_ids = result["sorted"]
    assert sorted_ids.index("test.modb") < sorted_ids.index("test.moda")


def test_sort_defaults_to_settings_algorithm(mcp_env: MCPEnv) -> None:
    result = modops.sort_active_list(mcp_env.ctx)
    assert result["method"] == "Topological"


def test_sort_alphabetical(mcp_env: MCPEnv) -> None:
    modops.set_active_list(
        mcp_env.ctx, ["ludeon.rimworld", "test.workshop", "test.modc"]
    )
    result = modops.sort_active_list(mcp_env.ctx, method="Alphabetical")
    assert result["method"] == "Alphabetical"
    sorted_ids = result["sorted"]
    # Names: "Gamma Mod" (test.modc) < "Workshop Mod" (test.workshop).
    assert sorted_ids.index("test.modc") < sorted_ids.index("test.workshop")
    assert set(sorted_ids) == {
        "ludeon.rimworld",
        "test.modc",
        "test.workshop",
    }


def test_sort_rejects_unknown_method(mcp_env: MCPEnv) -> None:
    with pytest.raises(ToolError, match="Unknown sort method"):
        modops.sort_active_list(mcp_env.ctx, method="Chronological")


def test_sort_reports_cycle_as_tool_error(mcp_env: MCPEnv) -> None:
    mcp_env.ctx.write_active_config(["ludeon.rimworld", "test.cycle1", "test.cycle2"])
    with pytest.raises(ToolError, match="circular dependencies"):
        modops.sort_active_list(mcp_env.ctx, method="Topological")


def test_set_active_list_rejects_unknown(mcp_env: MCPEnv) -> None:
    with pytest.raises(ToolError, match="Unknown packageIds"):
        modops.set_active_list(mcp_env.ctx, ["ludeon.rimworld", "nope.mod"])


def test_set_active_list_rejects_duplicates(mcp_env: MCPEnv) -> None:
    with pytest.raises(ToolError, match="duplicates"):
        modops.set_active_list(mcp_env.ctx, ["test.modb", "test.modb"])


def test_set_active_list_rejects_empty(mcp_env: MCPEnv) -> None:
    with pytest.raises(ToolError, match="empty"):
        modops.set_active_list(mcp_env.ctx, [])


def test_set_active_list_happy_path(mcp_env: MCPEnv) -> None:
    result = modops.set_active_list(
        mcp_env.ctx, ["ludeon.rimworld", "test.modb", "test.moda"]
    )
    assert result["written"] == 3
    assert result["backup"] is not None
    assert mcp_env.active_ids() == ["ludeon.rimworld", "test.modb", "test.moda"]


def test_set_active_list_allow_missing(mcp_env: MCPEnv) -> None:
    result = modops.set_active_list(
        mcp_env.ctx, ["ludeon.rimworld", "ghost.mod"], allow_missing=True
    )
    assert result["written"] == 2
    assert mcp_env.active_ids() == ["ludeon.rimworld", "ghost.mod"]


def test_update_add_remove_move(mcp_env: MCPEnv) -> None:
    result = modops.update_active_list(
        mcp_env.ctx,
        add=["test.workshop"],
        remove=["test.moda"],
        move_id="test.workshop",
        move_to_index=1,
    )
    assert result["added"] == ["test.workshop"]
    assert result["removed"] == ["test.moda"]
    assert result["moved"] is not None
    assert result["moved"]["to"] == 1
    assert mcp_env.active_ids() == [
        "ludeon.rimworld",
        "test.workshop",
        "ludeon.rimworld.royalty",
        "test.modb",
    ]


def test_update_add_skips_existing(mcp_env: MCPEnv) -> None:
    result = modops.update_active_list(mcp_env.ctx, add=["test.moda"])
    assert result["added"] == []
    assert result["after"] == 4


def test_update_add_unknown_raises(mcp_env: MCPEnv) -> None:
    with pytest.raises(ToolError, match="Unknown packageIds in add"):
        modops.update_active_list(mcp_env.ctx, add=["nope.mod"])


def test_update_remove_unknown_is_noop(mcp_env: MCPEnv) -> None:
    result = modops.update_active_list(mcp_env.ctx, remove=["nope.mod"])
    assert result["removed"] == []
    assert result["after"] == 4


def test_update_move_unknown_raises(mcp_env: MCPEnv) -> None:
    with pytest.raises(ToolError, match="not in the active list"):
        modops.update_active_list(mcp_env.ctx, move_id="nope.mod", move_to_index=0)


def test_update_requires_an_operation(mcp_env: MCPEnv) -> None:
    with pytest.raises(ToolError, match="Nothing to do"):
        modops.update_active_list(mcp_env.ctx)


def test_update_refuses_empty_result(mcp_env: MCPEnv) -> None:
    with pytest.raises(ToolError, match="empty active mod list"):
        modops.update_active_list(
            mcp_env.ctx,
            remove=[
                "ludeon.rimworld",
                "ludeon.rimworld.royalty",
                "test.modb",
                "test.moda",
            ],
        )


def test_modpack_save_load_list_apply(mcp_env: MCPEnv) -> None:
    saved = modops.save_modpack(mcp_env.ctx, "Test Pack")
    assert saved["count"] == 4

    listing = modops.list_modpacks()
    names = [entry["name"] for entry in listing["modpacks"]]
    assert "Test Pack" in names

    loaded = modops.load_modpack(mcp_env.ctx, "Test Pack")
    assert loaded["package_ids"] == [
        "ludeon.rimworld",
        "ludeon.rimworld.royalty",
        "test.modb",
        "test.moda",
    ]

    modops.set_active_list(mcp_env.ctx, ["ludeon.rimworld"])
    applied = modops.apply_modpack(mcp_env.ctx, "Test Pack")
    assert applied["count"] == 4
    assert len(mcp_env.active_ids()) == 4


def test_modpack_load_missing_raises(mcp_env: MCPEnv) -> None:
    with pytest.raises(ToolError, match="No saved modpack"):
        modops.load_modpack(mcp_env.ctx, "NoSuchPack")


def test_modpack_invalid_name_raises(mcp_env: MCPEnv) -> None:
    with pytest.raises(ToolError, match="Invalid modpack name"):
        modops.save_modpack(mcp_env.ctx, "../evil")


def test_modpacks_stored_in_modlists_folder(mcp_env: MCPEnv) -> None:
    modops.save_modpack(mcp_env.ctx, "PathCheck")
    folder = AppInfo().saved_modlists_folder
    assert (folder / "PathCheck.json").is_file()


def test_missing_modsconfig_raises(mcp_env: MCPEnv) -> None:
    mcp_env.mods_config.unlink()
    with pytest.raises(ToolError, match="No ModsConfig.xml"):
        modops.current_active_ids(mcp_env.ctx)


def test_installed_mod_summary(mcp_env: MCPEnv) -> None:
    summary = modops.installed_mod_summary(mcp_env.ctx)
    assert summary["installed"] == 12
    assert summary["listed"] == 11
    assert summary["by_source"]["Ludeon"] == 2
    assert summary["by_source"]["Steam Workshop"] == 2
    assert summary["by_source"]["Local"] == 7
