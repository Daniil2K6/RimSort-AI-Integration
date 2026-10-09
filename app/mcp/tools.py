"""MCP tool / resource / prompt registration for RimSort.

All tools are synchronous and return compact ``dict`` payloads (the SDK
serializes them as JSON). Expected failures raise ``ToolError`` so the
client receives ``is_error=True`` with a readable message.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp import launcher, modops, updates
from app.mcp.context import MCPContext
from app.models.metadata.metadata_structure import AboutXmlMod
from app.utils.app_info import AppInfo

_DESCRIPTION_LIMIT = 1200
_DEFAULT_LIST_LIMIT = 100
_MAX_LIST_LIMIT = 500


def _truncate(text: str, limit: int = _DESCRIPTION_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "… [truncated]"


def register_tools(mcp: MCPServer, ctx: MCPContext) -> None:
    """Attach all RimSort tools, resources and prompts to ``mcp``."""

    def _status(refresh: bool) -> dict[str, Any]:
        if refresh:
            ctx.refresh()
        instance = ctx.instance
        config_exists = ctx.mods_config_path.is_file()
        active_count = 0
        missing_count = 0
        if config_exists:
            config = ctx.read_active_config()
            if config is not None:
                active_count = len(config.activeMods)
                _, missing = ctx.resolve_active_paths(config)
                missing_count = len(missing)
        return {
            "rimsort_version": AppInfo().app_version,
            "instance": ctx.instance_name,
            "instances": sorted(ctx.instances()),
            "game_version": ctx.game_version,
            "paths": {
                "game": instance.game_folder or None,
                "config": instance.config_folder or None,
                "mods_config": str(ctx.mods_config_path),
                "local_mods": instance.local_folder or None,
                "workshop": instance.workshop_folder or None,
            },
            "mods": {
                **modops.installed_mod_summary(ctx),
                "active": active_count,
                "active_missing_from_disk": missing_count,
            },
            "sorting": ctx.sort_settings,
            "mods_config_exists": config_exists,
            "scan_error": ctx.last_scan_error,
        }

    @mcp.tool()
    def get_status(refresh: bool = False) -> dict[str, Any]:
        """Current instance, folders, game version and mod counts.

        Call this first. Set refresh=true after installing/removing mods
        or editing settings.json to rescan (otherwise results use the
        cached scan from server start).
        """
        return _status(refresh)

    @mcp.tool()
    def set_instance(name: str) -> dict[str, Any]:
        """Switch to another RimSort instance by name and rescan.

        Session-only: does not modify settings.json. Use get_status to
        see available instance names.
        """
        ctx.set_instance(name)
        return _status(False)

    @mcp.tool()
    def list_mods(
        query: str = "",
        source: str = "",
        active_only: bool = False,
        inactive_only: bool = False,
        limit: int = _DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List installed mods (paged). Returns id/name/active/source.

        query: case-insensitive substring match on packageId or name.
        source: filter by "Local", "Steam Workshop", "Steam Cmd",
        "Ludeon", "Git" or "Unknown".
        active_only/inactive_only: restrict to the active/inactive set.
        limit: page size (max 500). offset: skip this many matches.
        """
        limit = max(1, min(limit, _MAX_LIST_LIMIT))
        offset = max(offset, 0)

        active_config = ctx.read_active_config()
        active_paths: set[str] = set()
        if active_config is not None:
            resolved, _ = ctx.resolve_active_paths(active_config)
            active_paths = set(resolved)

        query_lower = query.strip().lower()
        source_lower = source.strip().lower()

        matches: list[dict[str, Any]] = []
        for path, mod in ctx.mods_metadata.items():
            if not isinstance(mod, AboutXmlMod):
                continue
            is_active = path in active_paths
            if active_only and not is_active:
                continue
            if inactive_only and is_active:
                continue
            pid = str(mod.package_id)
            name = mod.name if isinstance(mod.name, str) else ""
            if (
                query_lower
                and query_lower not in pid
                and query_lower not in name.lower()
            ):
                continue
            if source_lower and mod.mod_type.value.lower() != source_lower:
                continue
            matches.append(
                {
                    "id": pid,
                    "name": name,
                    "active": is_active,
                    "source": mod.mod_type.value,
                    "publishedfileid": mod.published_file_id,
                }
            )

        matches.sort(key=lambda entry: (entry["name"].lower(), entry["id"]))
        total = len(matches)
        page = matches[offset : offset + limit]
        return {
            "total": total,
            "offset": offset,
            "returned": len(page),
            "mods": page,
        }

    @mcp.tool()
    def get_mod_details(package_id: str) -> dict[str, Any]:
        """Full details and load-order rules for an installed mod.

        If several installed folders share the packageId, returns a
        "matches" list (duplicate installs); otherwise a single object.
        """
        paths = ctx.paths_for_pid(package_id)
        if not paths:
            raise ToolError(
                f"No installed mod with packageId {package_id!r}. "
                "Use list_mods to search."
            )

        active_config = ctx.read_active_config()
        active_paths: set[str] = set()
        if active_config is not None:
            resolved, _ = ctx.resolve_active_paths(active_config)
            active_paths = set(resolved)

        details: list[dict[str, Any]] = []
        for path in paths:
            mod = ctx.mods_metadata.get(path)
            if not isinstance(mod, AboutXmlMod):
                continue
            rules = mod.overall_rules
            details.append(
                {
                    "id": str(mod.package_id),
                    "name": mod.name if isinstance(mod.name, str) else "",
                    "authors": list(mod.authors),
                    "version": mod.mod_version,
                    "source": mod.mod_type.value,
                    "publishedfileid": mod.published_file_id,
                    "path": path,
                    "active": path in active_paths,
                    "supported_versions": sorted(mod.supported_versions),
                    "url": mod.url,
                    "description": _truncate(
                        mod.description if isinstance(mod.description, str) else ""
                    ),
                    "rules": {
                        "load_after": sorted(str(x) for x in rules.load_after),
                        "load_before": sorted(str(x) for x in rules.load_before),
                        "incompatible_with": sorted(
                            str(x) for x in rules.incompatible_with
                        ),
                        "dependencies": sorted(str(x) for x in rules.dependencies),
                        "load_first": rules.load_first,
                        "load_last": rules.load_last,
                    },
                }
            )

        if len(details) == 1:
            return details[0]
        return {
            "package_id": package_id,
            "duplicate_count": len(details),
            "matches": details,
        }

    @mcp.tool()
    def get_active_modlist() -> dict[str, Any]:
        """Active mod list in load order, as ModsConfig.xml stores it.

        Also reports which entries cannot be resolved on disk (missing).
        """
        return modops.current_active_ids(ctx)

    @mcp.tool()
    def set_active_modlist(
        package_ids: list[str],
        allow_missing: bool = False,
        backup: bool = True,
    ) -> dict[str, Any]:
        """Replace the active mod list wholesale and save ModsConfig.xml.

        Rejects unknown packageIds unless allow_missing=true. backup=true
        copies the previous ModsConfig.xml into RimSort's backup folder.
        Prefer validate_modlist/set via update_modlist for smaller edits.
        """
        return modops.set_active_list(
            ctx, package_ids, allow_missing=allow_missing, backup=backup
        )

    @mcp.tool()
    def update_modlist(
        add: list[str] | None = None,
        remove: list[str] | None = None,
        move_id: str = "",
        move_to_index: int = -1,
        backup: bool = True,
        allow_missing: bool = False,
    ) -> dict[str, Any]:
        """Apply delta edits to the active list (cheaper than full rewrite).

        add: packageIds to append (skips ones already active).
        remove: packageIds to remove (case-insensitive).
        move_id + move_to_index: reposition one mod; index 0 = top,
        -1 (default) = bottom. Changes are saved immediately.
        """
        return modops.update_active_list(
            ctx,
            add=add,
            remove=remove,
            move_id=move_id,
            move_to_index=move_to_index,
            allow_missing=allow_missing,
            backup=backup,
        )

    @mcp.tool()
    def validate_modlist() -> dict[str, Any]:
        """Check the active list: missing mods, duplicates, load-order
        violations (loadAfter/loadBefore), incompatible pairs, cycles.
        ok=true means no problems found.
        """
        return modops.validate_active_list(ctx)

    @mcp.tool()
    def sort_modlist(dry_run: bool = True, method: str = "") -> dict[str, Any]:
        """Sort the active list (tiered topological by default).

        dry_run=true (default) only returns the sorted order;
        dry_run=false saves ModsConfig.xml (with backup).
        method: "Topological" or "Alphabetical"; empty = instance setting.
        On circular dependencies returns is_error with the cycle list.
        """
        result = modops.sort_active_list(ctx, method=method)
        result["dry_run"] = dry_run
        if not dry_run:
            write = ctx.write_active_config(result["sorted"], backup=True)
            result["written"] = write["written"]
            result["backup"] = write["backup"]
        return result

    @mcp.tool()
    def save_modpack(name: str) -> dict[str, Any]:
        """Save the current active list as a named modpack (JSON file).

        Reuse names to overwrite. Modpacks live in RimSort's modlists
        folder and survive ModsConfig changes.
        """
        return modops.save_modpack(ctx, name)

    @mcp.tool()
    def load_modpack(name: str) -> dict[str, Any]:
        """Read a saved modpack's packageIds (does not change the game).

        Use set_active_modlist with the returned package_ids to apply it.
        """
        return modops.load_modpack(ctx, name)

    @mcp.tool()
    def apply_modpack(
        name: str, allow_missing: bool = False, backup: bool = True
    ) -> dict[str, Any]:
        """Load a saved modpack directly into ModsConfig.xml."""
        return modops.apply_modpack(
            ctx, name, allow_missing=allow_missing, backup=backup
        )

    @mcp.tool()
    def list_modpacks() -> dict[str, Any]:
        """List saved modpacks with mod counts and timestamps."""
        return modops.list_modpacks()

    @mcp.tool()
    def list_game_saves() -> dict[str, Any]:
        """RimWorld save games (.rws) for this instance, newest first.

        Use a returned path with import_modlist to load the mod list a
        save was created with.
        """
        return modops.list_game_saves(ctx)

    @mcp.tool()
    def import_modlist(
        path: str = "",
        apply: bool = False,
        allow_missing: bool = False,
        backup: bool = True,
    ) -> dict[str, Any]:
        """Import a mod list from a RimWorld save (.rws), ModsConfig XML,
        .rml list or RimSort JSON.

        path: file to read; "" or "latest" picks the newest .rws save
        (see list_game_saves). Returns package_ids in load order plus
        unknown ids. apply=true writes ModsConfig.xml (with backup);
        unknown ids reject the write unless allow_missing=true.
        """
        return modops.import_modlist(
            ctx, path, apply=apply, allow_missing=allow_missing, backup=backup
        )

    @mcp.tool()
    def check_workshop_updates(sources: str = "") -> dict[str, Any]:
        """Compare installed Workshop/SteamCMD mods with the Steam WebAPI.

        sources: comma-separated filter — "Steam CMD", "Steam Workshop"
        or ""/all (default). Returns outdated mods whose installed copy
        is older than the workshop time_updated, plus API failures.
        Uses ACF timestamps when present, otherwise local file mtimes.
        """
        return updates.check_workshop_updates(ctx, sources=sources)

    # Signature mirrors app.mcp.updates.update_mods for the tool schema.
    # jscpd:ignore-start
    @mcp.tool()
    def update_mods(
        package_ids: list[str] | None = None,
        publishedfileids: list[str] | None = None,
        outdated_only: bool = False,
        sources: str = "Steam CMD",
        validate: bool | None = None,
        install_steamcmd: bool = False,
        login: str = "anonymous",
        dry_run: bool = False,
        batch_timeout: int = 1800,
    ) -> dict[str, Any]:
        """Download/update mods via SteamCMD (blocking, headless).

        Targets: package_ids (installed mods with a PublishedFileId),
        explicit publishedfileids, or outdated_only=true (runs the
        update check first). sources filters outdated_only — default
        "Steam CMD"; Steam Workshop copies refresh through the Steam
        client. install_steamcmd=true downloads SteamCMD into the
        instance prefix when missing. login: "anonymous" (default) or a
        Steam account name — for a real account run `rimsort steam-login`
        once first and export RIMSORT_STEAM_PASSWORD (and
        RIMSORT_STEAM_GUARD_CODE if needed); credentials are never
        stored. dry_run=true only resolves targets. batch_timeout:
        seconds allowed per SteamCMD batch.
        """
        return updates.update_mods(
            ctx,
            package_ids=package_ids,
            publishedfileids=publishedfileids,
            outdated_only=outdated_only,
            sources=sources,
            validate=validate,
            install_steamcmd=install_steamcmd,
            login=login,
            dry_run=dry_run,
            batch_timeout=batch_timeout,
        )

    # jscpd:ignore-end

    @mcp.tool()
    def delete_mod(
        package_id: str,
        remove_from_active: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete an installed mod's files from disk.

        Refuses official Ludeon expansions. remove_from_active=true (default)
        also drops the packageId from ModsConfig.xml (with a backup). Use
        dry_run=true first to preview what would be deleted. For Workshop
        mods this only removes the local copy — the Steam subscription is
        unchanged (unsubscribe in the Steam client if you want it gone for
        good).
        """
        return modops.delete_mod(
            ctx,
            package_id,
            remove_from_active=remove_from_active,
            dry_run=dry_run,
        )

    @mcp.tool()
    def launch_game(dry_run: bool = False) -> dict[str, Any]:
        """Launch RimWorld for the current instance.

        dry_run=true returns the launch plan (executable/args or Steam
        URI) without starting anything — use it to verify first.
        Honors the instance's Steam-protocol and run_args settings.
        """
        return launcher.launch_game(ctx, dry_run=dry_run)

    # ---- Resources ----

    @mcp.resource(
        "rimsort://status",
        name="Instance status",
        description="JSON status of the current RimWorld instance.",
    )
    def _resource_status() -> dict[str, Any]:
        return _status(False)

    @mcp.resource(
        "rimsort://modlist/active",
        name="Active mod list",
        description="Active mod list (packageIds) in load order.",
    )
    def _resource_active_modlist() -> dict[str, Any]:
        return modops.current_active_ids(ctx)

    # ---- Prompts ----

    @mcp.prompt()
    def assemble_modpack(goal: str) -> str:
        """Guided workflow for building a mod list from a goal."""
        return (
            "You are managing RimWorld mods via the RimSort MCP server.\n"
            f"Goal: {goal}\n\n"
            "Workflow:\n"
            "1. get_status to learn the instance and current state.\n"
            "2. list_mods (query=...) to discover candidate mods.\n"
            "3. get_mod_details for each candidate: check rules.load_after, "
            "rules.incompatible_with and supported_versions.\n"
            "4. Build the list bottom-up: dependencies first, then the goal "
            "mods, then compatibility patches.\n"
            "5. set_active_modlist (or update_modlist) to apply, then "
            "sort_modlist dry_run=false to order it.\n"
            "6. validate_modlist — must return ok=true.\n"
            "7. save_modpack to persist the result; launch_game dry_run=true "
            "to show the plan before launch_game.\n"
        )

    @mcp.prompt()
    def troubleshoot_active_list() -> str:
        """Guided workflow for diagnosing a broken active mod list."""
        return (
            "Diagnose the current RimWorld active mod list with RimSort MCP.\n"
            "1. get_status — note counts and scan_error.\n"
            "2. validate_modlist — inspect missing, duplicates, "
            "order_violations, incompatible_pairs and cycles.\n"
            "3. For each reported mod, get_mod_details to understand its rules.\n"
            "4. Fix with update_modlist (targeted add/remove/move) or "
            "sort_modlist for ordering issues.\n"
            "5. Re-run validate_modlist until ok=true, then save_modpack.\n"
        )
