"""Tests for app.mcp.context (settings, scanning, ModsConfig IO)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.mcp.context import MCPContext, MCPContextError
from app.models.metadata.metadata_structure import (
    AboutXmlMod,
    CaseInsensitiveStr,
    ModsConfig,
    ModType,
)
from app.utils.app_info import AppInfo
from tests.mcp.conftest import MCPEnv


def test_scan_finds_mods_and_sources(mcp_env: MCPEnv) -> None:
    ctx = mcp_env.ctx
    about_mods = [
        mod for mod in ctx.mods_metadata.values() if isinstance(mod, AboutXmlMod)
    ]
    # 2 ludeon + 5 local (A/B/C/Conflict/Cycle1/Cycle2/Dup=7 actually) counted below
    assert len(about_mods) == 11
    assert len(ctx.mods_metadata) == 12  # + invalid "Broken" folder
    assert ctx.game_version == "1.5.4104 rev1234"

    core_paths = ctx.paths_for_pid("ludeon.rimworld")
    assert len(core_paths) == 1
    core = ctx.mods_metadata[core_paths[0]]
    assert core.mod_type == ModType.LUDEON

    workshop_paths = ctx.paths_for_pid("test.workshop")
    assert len(workshop_paths) == 1
    assert ctx.mods_metadata[workshop_paths[0]].mod_type == ModType.STEAM_WORKSHOP


def test_packageid_resolution_with_steam_suffix(mcp_env: MCPEnv) -> None:
    ctx = mcp_env.ctx
    plain = ctx.paths_for_pid("test.dup")
    assert len(plain) == 2
    types = {ctx.mods_metadata[p].mod_type for p in plain}
    assert types == {ModType.LOCAL, ModType.STEAM_WORKSHOP}

    config = ModsConfig(
        version="1.5",
        activeMods=[
            CaseInsensitiveStr("test.dup"),
            CaseInsensitiveStr("test.dup_steam"),
        ],
        knownExpansions=[],
    )
    resolved, missing = ctx.resolve_active_paths(config)
    assert missing == []
    assert len(resolved) == 2
    assert resolved[0] != resolved[1]
    assert ctx.mods_metadata[resolved[0]].mod_type == ModType.LOCAL
    assert ctx.mods_metadata[resolved[1]].mod_type == ModType.STEAM_WORKSHOP


def test_resolve_reports_missing(mcp_env: MCPEnv) -> None:
    ctx = mcp_env.ctx
    config = ModsConfig(
        version="1.5",
        activeMods=[
            CaseInsensitiveStr("ludeon.rimworld"),
            CaseInsensitiveStr("ghost.mod"),
            CaseInsensitiveStr("test.moda"),
        ],
        knownExpansions=[],
    )
    resolved, missing = ctx.resolve_active_paths(config)
    assert missing == ["ghost.mod"]
    assert len(resolved) == 2


def test_write_active_config_roundtrip_and_backup(mcp_env: MCPEnv) -> None:
    ctx = mcp_env.ctx
    before = mcp_env.mods_config.read_bytes()

    result = ctx.write_active_config(["ludeon.rimworld", "test.modb", "test.moda"])
    assert result["written"] == 3
    assert result["backup"] is not None
    backup = Path(str(result["backup"]))
    assert backup.is_file()
    assert backup.read_bytes() == before

    reloaded = ctx.read_active_config()
    assert reloaded is not None
    assert [str(pid) for pid in reloaded.activeMods] == [
        "ludeon.rimworld",
        "test.modb",
        "test.moda",
    ]
    # knownExpansions preserved from the previous config.
    assert [str(x) for x in reloaded.knownExpansions] == ["ludeon.rimworld.royalty"]


def test_backup_lives_under_app_backups_folder(mcp_env: MCPEnv) -> None:
    ctx = mcp_env.ctx
    result = ctx.write_active_config(["ludeon.rimworld", "test.modb"])
    backup = Path(str(result["backup"]))
    assert AppInfo().backups_folder / "mcp" in backup.parents
    backups = sorted((AppInfo().backups_folder / "mcp").glob("ModsConfig-*.xml"))
    assert len(backups) >= 1


def test_write_without_backup(mcp_env: MCPEnv) -> None:
    ctx = mcp_env.ctx
    result = ctx.write_active_config(["ludeon.rimworld"], backup=False)
    assert result["backup"] is None


def test_missing_settings_file_raises() -> None:
    ctx = MCPContext(settings_path=Path("/nonexistent/settings.json"))
    with pytest.raises(MCPContextError) as excinfo:
        ctx.load_settings()
    assert "settings.json not found" in str(excinfo.value)


def test_unknown_instance_raises(mcp_env: MCPEnv) -> None:
    with pytest.raises(MCPContextError, match="Unknown instance"):
        mcp_env.ctx.set_instance("DoesNotExist")


def test_set_instance_roundtrip(mcp_env: MCPEnv) -> None:
    settings = json.loads(mcp_env.settings_path.read_text(encoding="utf-8"))
    second = dict(settings["instances"]["Default"])
    second["name"] = "Second"
    second["local_folder"] = str(mcp_env.tmp_path / "mods2")
    Path(second["local_folder"]).mkdir(exist_ok=True)
    settings["instances"]["Second"] = second
    mcp_env.settings_path.write_text(json.dumps(settings), encoding="utf-8")

    mcp_env.ctx.set_instance("Second")
    assert mcp_env.ctx.instance_name == "Second"
    # Rescanned against the second instance: its local folder is empty,
    # so local-only mods are gone while workshop/ludeon mods remain.
    pids = set(mcp_env.ctx.packageid_to_paths)
    assert "test.moda" not in pids
    assert "test.workshop" in pids


def test_env_var_overrides_settings_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.mcp.context import SETTINGS_ENV_VAR

    monkeypatch.setenv(SETTINGS_ENV_VAR, "/tmp/whatever.json")
    assert MCPContext()._default_settings_path() == Path("/tmp/whatever.json")


def test_sort_settings_defaults(mcp_env: MCPEnv) -> None:
    settings = mcp_env.ctx.sort_settings
    assert settings["sorting_algorithm"] == "Topological"
    assert settings["use_moddependencies_as_loadTheseBefore"] is False
    assert settings["use_alternative_package_ids_as_satisfying_dependencies"] is True
