"""Headless context for the RimSort MCP server.

Loads settings.json directly (no Qt), resolves the active RimWorld
instance, scans installed mods and provides ModsConfig.xml IO with
backups. All functions are safe to call without a QApplication.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import msgspec
from loguru import logger
from mcp.server.mcpserver.exceptions import ToolError
from natsort import natsorted

from app.models.instance import Instance
from app.models.metadata.metadata_factory import (
    create_listed_mod_from_path,
    create_rules_from_external_rules,
    read_mods_config,
    read_rules_db,
)
from app.models.metadata.metadata_structure import (
    AboutXmlMod,
    CompiledDependencyData,
    ListedMod,
    ModsConfig,
    ModType,
)
from app.models.mod_list import (
    _SOURCE_PRIORITY_DEFAULT,
    _SOURCE_PRIORITY_STEAM,
    _STEAM_SUFFIX,
)
from app.utils.app_info import AppInfo
from app.utils.constants import DEFAULT_INSTANCE_NAME, RIMWORLD_PACKAGE_IDS
from app.utils.xml import json_to_xml_write

SETTINGS_ENV_VAR = "RIMSORT_MCP_SETTINGS"
_MAX_CONFIG_BACKUPS = 20
_BACKUP_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

# Sentinel paths for mod-type detection when an instance folder is unset.
# They never equal a real mod's parent directory, so detection simply
# never matches instead of crashing.
_MISSING_FOLDER = Path(os.devnull)


class MCPContextError(ToolError):
    """Expected context failure (bad settings, missing folders).

    Subclasses the MCP SDK's ``ToolError`` so it surfaces as a clean
    ``is_error`` tool result instead of an internal server crash.
    """


def resolve_db_path(
    source: str, file_path: str, repo_url: str, file_name: str
) -> Path | None:
    """Resolve the on-disk path for an external database file.

    Mirror of ``MetadataController._resolve_db_path`` without importing
    the Qt controller.
    """
    if source == "Disabled":
        return None
    if source == "Configured file path":
        return Path(file_path) if file_path else None
    repo_name = Path(repo_url).name
    return AppInfo().databases_folder / repo_name / file_name


class MCPContext:
    """Session-scoped state: settings, instance, scanned mods."""

    def __init__(self, settings_path: Path | None = None) -> None:
        self.settings_path = (
            settings_path
            if settings_path is not None
            else self._default_settings_path()
        )
        self._instance_override: str | None = None
        self._mods_metadata: dict[str, ListedMod] = {}
        self._packageid_to_paths: dict[str, set[str]] = {}
        self._compiled: CompiledDependencyData | None = None
        self.game_version: str = "Unknown"
        self.last_scan_error: str | None = None

    @staticmethod
    def _default_settings_path() -> Path:
        env_value = os.environ.get(SETTINGS_ENV_VAR, "").strip()
        if env_value:
            return Path(env_value)
        return AppInfo().app_settings_file

    # ---- Settings / instances ----

    def load_settings(self) -> dict[str, Any]:
        """Read settings.json as a plain dict."""
        if not self.settings_path.exists():
            raise MCPContextError(
                f"settings.json not found at {self.settings_path}. "
                "Start RimSort once to create it, or set "
                f"{SETTINGS_ENV_VAR} to its location."
            )
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MCPContextError(
                f"Failed to read {self.settings_path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise MCPContextError(f"Unexpected settings format in {self.settings_path}")
        return data

    def instances(self) -> dict[str, Instance]:
        """All configured instances keyed by name."""
        raw = self.load_settings().get("instances") or {}
        result: dict[str, Instance] = {}
        if isinstance(raw, dict):
            for name, value in raw.items():
                if not isinstance(value, dict):
                    continue
                try:
                    result[str(name)] = msgspec.convert(value, type=Instance)
                except (msgspec.ValidationError, TypeError) as exc:
                    logger.warning(f"Skipping invalid instance {name!r}: {exc}")
        return result

    @property
    def instance_name(self) -> str:
        """Name of the active (or session-overridden) instance."""
        available = self.instances()
        if self._instance_override is not None:
            if self._instance_override in available:
                return self._instance_override
            raise MCPContextError(
                f"Instance {self._instance_override!r} no longer exists. "
                f"Available: {sorted(available)}"
            )
        raw_name = self.load_settings().get("current_instance")
        name = str(raw_name) if raw_name else DEFAULT_INSTANCE_NAME
        if name in available:
            return name
        if available:
            fallback = sorted(available)[0]
            logger.warning(
                f"current_instance {name!r} not found, using {fallback!r} instead"
            )
            return fallback
        raise MCPContextError(f"No instances configured in {self.settings_path}")

    @property
    def instance(self) -> Instance:
        available = self.instances()
        name = self.instance_name
        if name not in available:  # pragma: no cover - guarded above
            raise MCPContextError(f"Instance {name!r} not found")
        return available[name]

    def set_instance(self, name: str) -> Instance:
        """Switch the session to another instance (does not touch disk)."""
        available = self.instances()
        if name not in available:
            raise MCPContextError(
                f"Unknown instance {name!r}. Available: {sorted(available)}"
            )
        self._instance_override = name
        self.refresh()
        return available[name]

    # ---- Paths ----

    @property
    def game_folder(self) -> Path | None:
        value = self.instance.game_folder
        return Path(value) if value else None

    @property
    def config_folder(self) -> Path:
        value = self.instance.config_folder
        if not value:
            raise MCPContextError(
                f"Instance {self.instance_name!r} has no config folder configured"
            )
        return Path(value)

    @property
    def local_folder(self) -> Path | None:
        value = self.instance.local_folder
        return Path(value) if value else None

    @property
    def workshop_folder(self) -> Path | None:
        value = self.instance.workshop_folder
        return Path(value) if value else None

    @property
    def mods_config_path(self) -> Path:
        return self.config_folder / "ModsConfig.xml"

    # ---- Game version / settings helpers ----

    def _read_game_version(self, game_folder: Path | None) -> str:
        if game_folder is None:
            return "Unknown"
        version_file = game_folder / "Version.txt"
        try:
            if version_file.is_file():
                return version_file.read_text(encoding="utf-8").strip() or "Unknown"
        except OSError as exc:
            logger.warning(f"Unable to read {version_file}: {exc}")
        return "Unknown"

    @property
    def sort_settings(self) -> dict[str, Any]:
        """Sorting-related settings with GUI defaults."""
        data = self.load_settings()
        return {
            "sorting_algorithm": str(data.get("sorting_algorithm", "Topological")),
            "use_moddependencies_as_loadTheseBefore": bool(
                data.get("use_moddependencies_as_loadTheseBefore", False)
            ),
            "use_alternative_package_ids_as_satisfying_dependencies": bool(
                data.get("use_alternative_package_ids_as_satisfying_dependencies", True)
            ),
        }

    def community_rules_path(self) -> Path | None:
        data = self.load_settings()
        return resolve_db_path(
            str(data.get("external_community_rules_metadata_source", "Disabled")),
            str(data.get("external_community_rules_file_path", "")),
            str(data.get("external_community_rules_repo", "")),
            "communityRules.json",
        )

    # ---- Mod scanning ----

    def refresh(self) -> int:
        """Rescan installed mods. Returns the number of mods found."""
        data = self.load_settings()
        prefer_versioned = bool(data.get("prefer_versioned_about_tags", True))
        case_insensitive = bool(
            data.get("case_insensitive_about_xml_lookup", sys.platform == "linux")
        )

        game_folder = self.game_folder
        self.game_version = self._read_game_version(game_folder)

        local_folder = self.local_folder
        if local_folder is None and game_folder is not None:
            local_folder = game_folder / "Mods"
        workshop_folder = self.workshop_folder
        game_data_folder = game_folder / "Data" if game_folder is not None else None

        search_dirs = [
            folder
            for folder in (local_folder, workshop_folder, game_data_folder)
            if folder is not None and folder.is_dir()
        ]
        mod_paths: list[Path] = []
        missing_dirs: list[str] = []
        for configured in (local_folder, workshop_folder, game_data_folder):
            if configured is not None and not configured.is_dir():
                missing_dirs.append(str(configured))
        if missing_dirs:
            logger.warning(f"Mod search paths missing, skipping: {missing_dirs}")

        for folder in search_dirs:
            try:
                mod_paths.extend(sorted(p for p in folder.iterdir() if p.is_dir()))
            except OSError as exc:
                logger.warning(f"Unable to list {folder}: {exc}")

        user_rules = read_rules_db(AppInfo().user_rules_file)
        community_rules_path = self.community_rules_path()
        community_rules = (
            read_rules_db(community_rules_path)
            if community_rules_path is not None
            else None
        )

        local_arg = local_folder if local_folder is not None else _MISSING_FOLDER
        game_arg = game_folder if game_folder is not None else _MISSING_FOLDER

        def _parse(mod_path: Path) -> ListedMod | None:
            try:
                _, mod = create_listed_mod_from_path(
                    mod_path,
                    self.game_version,
                    local_arg,
                    game_arg,
                    workshop_folder,
                    prefer_versioned,
                    case_insensitive,
                )
                if isinstance(mod, AboutXmlMod):
                    if user_rules is not None and mod.package_id in user_rules.rules:
                        mod.user_rules = create_rules_from_external_rules(
                            external_rule=user_rules.rules[mod.package_id]
                        )
                    if (
                        community_rules is not None
                        and mod.package_id in community_rules.rules
                    ):
                        mod.community_rules = create_rules_from_external_rules(
                            external_rule=community_rules.rules[mod.package_id]
                        )
                return mod
            except Exception as exc:  # noqa: BLE001 - one bad mod must not kill the scan
                logger.error(f"Error parsing mod at path: {mod_path}: {exc}")
                return None

        self.last_scan_error = None
        if mod_paths:
            workers = min(8, os.cpu_count() or 4)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                parsed = list(pool.map(_parse, mod_paths))
        else:
            parsed = []

        self._mods_metadata = {mod.uuid: mod for mod in parsed if mod is not None}
        pid_map: dict[str, set[str]] = {}
        for path, mod in self._mods_metadata.items():
            if isinstance(mod, AboutXmlMod):
                pid_map.setdefault(str(mod.package_id), set()).add(path)
        self._packageid_to_paths = pid_map
        self._compiled = None

        if not search_dirs:
            self.last_scan_error = "No valid mod folders found for this instance"
        logger.info(
            f"MCP scan complete: {len(self._mods_metadata)} mods "
            f"(game version {self.game_version})"
        )
        return len(self._mods_metadata)

    # ---- Scanned data ----

    @property
    def mods_metadata(self) -> dict[str, ListedMod]:
        if not self._mods_metadata and self.last_scan_error is None:
            self.refresh()
        return self._mods_metadata

    @property
    def packageid_to_paths(self) -> dict[str, set[str]]:
        if not self._packageid_to_paths and self.last_scan_error is None:
            self.refresh()
        return self._packageid_to_paths

    @property
    def compiled_data(self) -> CompiledDependencyData:
        if self._compiled is None:
            settings = self.sort_settings
            self._compiled = CompiledDependencyData.build(
                self.mods_metadata,
                settings["use_moddependencies_as_loadTheseBefore"],
                settings["use_alternative_package_ids_as_satisfying_dependencies"],
            )
        return self._compiled

    def pid_for_path(self, path: str) -> str | None:
        mod = self._mods_metadata.get(path)
        if isinstance(mod, AboutXmlMod):
            return str(mod.package_id)
        return None

    def paths_for_pid(self, package_id: str) -> list[str]:
        return sorted(self.packageid_to_paths.get(package_id.lower(), set()))

    # ---- ModsConfig.xml IO ----

    def read_active_config(self) -> ModsConfig | None:
        return read_mods_config(self.mods_config_path)

    def backup_mods_config(self) -> Path | None:
        """Copy the current ModsConfig.xml into the backup folder."""
        source = self.mods_config_path
        if not source.is_file():
            return None
        backup_dir = AppInfo().backups_folder / "mcp"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime(_BACKUP_TIMESTAMP_FORMAT)
        target = backup_dir / f"ModsConfig-{timestamp}.xml"
        counter = 1
        while target.exists():
            target = backup_dir / f"ModsConfig-{timestamp}-{counter}.xml"
            counter += 1
        target.write_bytes(source.read_bytes())
        self._prune_backups(backup_dir)
        logger.info(f"Backed up ModsConfig.xml to {target}")
        return target

    @staticmethod
    def _prune_backups(backup_dir: Path) -> None:
        backups = sorted(backup_dir.glob("ModsConfig-*.xml"))
        excess = len(backups) - _MAX_CONFIG_BACKUPS
        for old in backups[: max(excess, 0)]:
            try:
                old.unlink()
            except OSError as exc:
                logger.warning(f"Unable to remove old backup {old}: {exc}")

    def write_active_config(
        self,
        package_ids: Iterable[str],
        known_expansions: Iterable[str] | None = None,
        backup: bool = True,
    ) -> dict[str, Any]:
        """Write the active mod list to ModsConfig.xml.

        :return: ``{"path": str, "written": int, "backup": str | None}``
        """
        package_id_list = [str(pid) for pid in package_ids]
        if known_expansions is None:
            existing = self.read_active_config()
            if existing is not None:
                known_expansions = [str(x) for x in existing.knownExpansions]
            else:
                known_expansions = [
                    pid for pid in RIMWORLD_PACKAGE_IDS if pid != "ludeon.rimworld"
                ]
        version = self.game_version
        if version == "Unknown":
            existing = self.read_active_config()
            if existing is not None:
                version = existing.version

        backup_path = self.backup_mods_config() if backup else None
        payload = {
            "ModsConfigData": {
                "version": version,
                "activeMods": {"li": package_id_list},
                "knownExpansions": {"li": list(known_expansions)},
            }
        }
        self.mods_config_path.parent.mkdir(parents=True, exist_ok=True)
        json_to_xml_write(payload, str(self.mods_config_path), raise_errs=True)
        logger.info(
            f"Wrote {len(package_id_list)} active mods to {self.mods_config_path}"
        )
        return {
            "path": str(self.mods_config_path),
            "written": len(package_id_list),
            "backup": str(backup_path) if backup_path is not None else None,
        }

    # ---- Resolution ----

    def resolve_active_paths(self, config: ModsConfig) -> tuple[list[str], list[str]]:
        """Map config packageIds to installed mod paths.

        Mirrors ``ModList.from_mods_config`` resolution rules (case-insensitive
        ids, ``_steam`` suffix, source-priority tiebreaking).

        :return: ``(resolved_paths_in_order, missing_package_ids)``
        """
        resolved: list[str] = []
        missing: list[str] = []
        seen: set[str] = set()

        for config_id in config.activeMods:
            config_id_str = str(config_id)
            is_steam = config_id_str.endswith(_STEAM_SUFFIX)
            raw_pid = (
                config_id_str[: -len(_STEAM_SUFFIX)] if is_steam else config_id_str
            )
            candidate_paths = self.packageid_to_paths.get(raw_pid, set())
            if not candidate_paths:
                missing.append(raw_pid)
                continue
            priority = _SOURCE_PRIORITY_STEAM if is_steam else _SOURCE_PRIORITY_DEFAULT
            resolved_path = self._pick_path(candidate_paths, priority, seen)
            if resolved_path is None:
                missing.append(raw_pid)
                continue
            seen.add(resolved_path)
            resolved.append(resolved_path)

        return resolved, missing

    def _pick_path(
        self,
        candidate_paths: set[str],
        source_priority: list[ModType],
        seen_paths: set[str],
    ) -> str | None:
        """Pick the best candidate path using source priority and natsort.

        Mirrors ``ModList._resolve_path`` without needing a MetadataController.
        """
        metadata = self.mods_metadata
        for source_type in source_priority:
            matching = [
                p
                for p in candidate_paths
                if p not in seen_paths
                and metadata.get(p) is not None
                and metadata[p].mod_type == source_type
            ]
            if matching:
                return natsorted(matching)[0]
        remaining = [p for p in candidate_paths if p not in seen_paths]
        if remaining:
            return natsorted(remaining)[0]
        return None
