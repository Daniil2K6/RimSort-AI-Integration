"""Shared fixtures for MCP server tests.

Builds a self-contained fake RimWorld install + settings.json in the
test's tmp_path, then returns a ready-to-use :class:`MCPContext`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.mcp.context import MCPContext

_ABOUT_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<ModMetaData>
    <name>{name}</name>
    <packageId>{pid}</packageId>
    <author>tester</author>
    <supportedVersions>
        <li>1.5</li>
    </supportedVersions>
    <description>Description of {name}</description>
    {extra}
</ModMetaData>
"""

_MODSCONFIG_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<ModsConfigData>
  <version>1.5.4104 rev1234</version>
  <activeMods>
    <li>ludeon.rimworld</li>
    <li>ludeon.rimworld.royalty</li>
    <li>test.modb</li>
    <li>test.moda</li>
  </activeMods>
  <knownExpansions>
    <li>ludeon.rimworld.royalty</li>
  </knownExpansions>
</ModsConfigData>
"""


def write_mod(root: Path, folder: str, pid: str, name: str, extra: str = "") -> Path:
    """Create a minimal valid mod directory with an About.xml."""
    about = root / folder / "About" / "About.xml"
    about.parent.mkdir(parents=True, exist_ok=True)
    about.write_text(
        _ABOUT_TEMPLATE.format(name=name, pid=pid, extra=extra),
        encoding="utf-8",
    )
    return root / folder


@dataclass
class MCPEnv:
    """Filesystem layout + initialized context for one test."""

    ctx: MCPContext
    tmp_path: Path
    game: Path
    mods: Path
    workshop: Path
    config: Path
    settings_path: Path
    extra: dict[str, Path] = field(default_factory=dict)

    @property
    def mods_config(self) -> Path:
        return self.config / "ModsConfig.xml"

    def write_settings(self, **overrides: object) -> None:
        """Rewrite settings.json (merging overrides into Default instance)."""
        data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        instance = data["instances"]["Default"]
        for key, value in overrides.items():
            if key in ("current_instance", "instances"):
                data[key] = value
            else:
                instance[key] = value
        self.settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def active_ids(self) -> list[str]:
        config = self.ctx.read_active_config()
        assert config is not None
        return [str(pid) for pid in config.activeMods]


@pytest.fixture
def mcp_env(tmp_path: Path) -> MCPEnv:
    game = tmp_path / "RimWorld.app"
    (game / "Data").mkdir(parents=True)
    (game / "Version.txt").write_text("1.5.4104 rev1234", encoding="utf-8")
    # Executables for every platform so launch dry-run can resolve one.
    (game / "RimWorldWin64.exe").write_bytes(b"")
    rimworld_linux = game / "RimWorldLinux"
    rimworld_linux.write_bytes(b"")
    rimworld_linux.chmod(0o755)

    mods = tmp_path / "mods"
    mods.mkdir()
    workshop = tmp_path / "workshop"
    workshop.mkdir()
    config = tmp_path / "config"
    config.mkdir()

    # Ludeon content lives in <game>/Data (mod_type == LUDEON).
    write_mod(game / "Data", "Core", "ludeon.rimworld", "RimWorld")
    write_mod(game / "Data", "Royalty", "ludeon.rimworld.royalty", "Royalty")

    write_mod(mods, "ModB", "test.modb", "Beta Mod")
    write_mod(
        mods,
        "ModA",
        "test.moda",
        "Alpha Mod",
        "<loadAfter><li>test.modb</li></loadAfter>"
        "<incompatibleWith><li>test.conflict</li></incompatibleWith>",
    )
    write_mod(
        mods,
        "ModConflict",
        "test.conflict",
        "Conflict Mod",
        "<loadAfter><li>test.modb</li></loadAfter>",
    )
    write_mod(mods, "ModC", "test.modc", "Gamma Mod")
    write_mod(
        mods,
        "Cycle1",
        "test.cycle1",
        "Cycle One",
        "<loadAfter><li>test.cycle2</li></loadAfter>",
    )
    write_mod(
        mods,
        "Cycle2",
        "test.cycle2",
        "Cycle Two",
        "<loadAfter><li>test.cycle1</li></loadAfter>",
    )
    # Duplicate packageId across Local + Steam Workshop sources.
    write_mod(mods, "DupLocal", "test.dup", "Dup Local")
    write_mod(workshop, "99887766", "test.dup", "Dup Steam")

    write_mod(workshop, "11223344", "test.workshop", "Workshop Mod")

    # Invalid mod folder (no About.xml).
    (mods / "Broken").mkdir()

    (config / "ModsConfig.xml").write_text(_MODSCONFIG_TEMPLATE, encoding="utf-8")

    settings_path = tmp_path / "settings.json"
    settings = {
        "current_instance": "Default",
        "sorting_algorithm": "Topological",
        "use_moddependencies_as_loadTheseBefore": False,
        "use_alternative_package_ids_as_satisfying_dependencies": True,
        "prefer_versioned_about_tags": True,
        "case_insensitive_about_xml_lookup": True,
        "external_community_rules_metadata_source": "Disabled",
        "instances": {
            "Default": {
                "name": "Default",
                "game_folder": str(game),
                "config_folder": str(config),
                "local_folder": str(mods),
                "workshop_folder": str(workshop),
                "run_args": "",
                "steam_client_integration": False,
                "launch_via_steam_protocol": False,
                "steamcmd_ignore": True,
                "initial_setup": False,
            }
        },
    }
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    ctx = MCPContext(settings_path=settings_path)
    ctx.refresh()

    return MCPEnv(
        ctx=ctx,
        tmp_path=tmp_path,
        game=game,
        mods=mods,
        workshop=workshop,
        config=config,
        settings_path=settings_path,
    )
