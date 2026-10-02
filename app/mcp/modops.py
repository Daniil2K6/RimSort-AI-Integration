"""Mod-list operations for the MCP server.

Validation, sorting, delta updates and named modpack persistence. All
entry points are headless: no Qt dialogs are ever shown; expected
failures raise ``ToolError`` so MCP clients get a readable error.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from mcp.server.mcpserver.exceptions import ToolError
from toposort import CircularDependencyError, toposort

from app.controllers.sort_controller import Sorter
from app.mcp.context import MCPContext
from app.models.metadata.metadata_structure import AboutXmlMod, ListedMod, ModsConfig
from app.sort.alphabetical_sort import do_alphabetical_sort
from app.sort.topo_sort import describe_circular_dependencies, order_topo_levels
from app.utils.app_info import AppInfo

_MODPACK_NAME_RE = re.compile(r"^[\w][\w\- .]{0,63}$", re.UNICODE)
_REPORT_CAP = 50


def _require_config(ctx: MCPContext) -> ModsConfig:
    config = ctx.read_active_config()
    if config is None:
        raise ToolError(
            f"No ModsConfig.xml found at {ctx.mods_config_path}. "
            "Launch RimWorld once, or point the instance config folder "
            "at a valid RimWorld Config directory."
        )
    return config


def current_active_ids(ctx: MCPContext) -> dict[str, Any]:
    """Active mod list exactly as the game sees it.

    :return: ``{"mods": [...], "count": int, "missing": [...]}``
    """
    config = _require_config(ctx)
    _, missing = ctx.resolve_active_paths(config)
    mods = [str(pid) for pid in config.activeMods]
    return {"mods": mods, "count": len(mods), "missing": missing}


def _resolved_ids(ctx: MCPContext) -> tuple[list[str], list[str]]:
    """Resolve the active config to installed packageIds (drop-in order)."""
    config = _require_config(ctx)
    paths, missing = ctx.resolve_active_paths(config)
    ids: list[str] = []
    for path in paths:
        pid = ctx.pid_for_path(path)
        ids.append(pid if pid is not None else path)
    return ids, missing


def validate_active_list(ctx: MCPContext) -> dict[str, Any]:
    """Validate the active mod list (missing, duplicates, order, cycles)."""
    config = _require_config(ctx)
    raw_ids = [str(pid) for pid in config.activeMods]
    duplicates = sorted({pid for pid, count in Counter(raw_ids).items() if count > 1})

    paths, missing = ctx.resolve_active_paths(config)
    active_pids: list[str] = []
    for path in paths:
        pid = ctx.pid_for_path(path)
        if pid is not None:
            active_pids.append(pid)
    active_set = set(active_pids)
    positions: dict[str, int] = {}
    for index, pid in enumerate(active_pids):
        positions.setdefault(pid, index)

    deps_graph = ctx.compiled_data.deps_graph
    order_violations: list[dict[str, Any]] = []
    for pid, deps in deps_graph.items():
        if pid not in positions:
            continue
        for dep in deps:
            if dep not in active_set:
                continue
            if positions[dep] > positions[pid]:
                order_violations.append(
                    {
                        "mod": pid,
                        "mod_index": positions[pid],
                        "must_load_before_is_at": dep,
                        "other_index": positions[dep],
                    }
                )

    incompatible: list[list[str]] = []
    for pid, targets in ctx.compiled_data.incompatibilities.items():
        if pid not in active_set:
            continue
        for target in targets:
            if target in active_set and pid < target:
                incompatible.append([pid, target])

    active_graph = {
        pid: deps & active_set for pid, deps in deps_graph.items() if pid in active_set
    }
    cycles: list[str] = []
    try:
        list(toposort(active_graph))
    except CircularDependencyError:
        cycles = describe_circular_dependencies(active_graph)

    def _cap(items: list[Any]) -> dict[str, Any]:
        return {
            "items": items[:_REPORT_CAP],
            "count": len(items),
            "truncated": len(items) > _REPORT_CAP,
        }

    ok = not (missing or duplicates or order_violations or incompatible or cycles)
    return {
        "ok": ok,
        "active_count": len(raw_ids),
        "installed_active_count": len(active_pids),
        "missing": _cap(missing),
        "duplicates": _cap(duplicates),
        "order_violations": _cap(order_violations),
        "incompatible_pairs": _cap(incompatible),
        "cycles": _cap(cycles),
    }


def _headless_topo_sort(
    dependency_graph: dict[str, set[str]],
    active_mod_paths: set[str],
    mods_metadata: Mapping[str, ListedMod],
) -> list[str]:
    """Topological sort that raises ``ToolError`` instead of showing a dialog."""
    try:
        levels = list(toposort(dependency_graph))
    except CircularDependencyError:
        cycles = describe_circular_dependencies(dependency_graph)
        detail = "\n".join(cycles) if cycles else "cycle details unavailable"
        raise ToolError(
            "Cannot sort: circular dependencies found:\n" + detail
        ) from None
    return order_topo_levels(levels, active_mod_paths, mods_metadata)


def sort_active_list(ctx: MCPContext, method: str = "") -> dict[str, Any]:
    """Sort the active list. Returns packageIds in the new order.

    :param method: ``"Topological"`` or ``"Alphabetical"``; defaults to
        the instance's configured sorting algorithm.
    """
    config = _require_config(ctx)
    paths, missing = ctx.resolve_active_paths(config)
    algorithm = method or ctx.sort_settings["sorting_algorithm"]

    if algorithm.lower().startswith("alpha"):
        sort_method: Any = do_alphabetical_sort
    elif algorithm.lower().startswith("topo"):
        sort_method = _headless_topo_sort
    else:
        raise ToolError(
            f"Unknown sort method {method!r}. Use 'Topological' or 'Alphabetical'."
        )

    sorter = Sorter(
        sort_method,
        ctx.compiled_data,
        ctx.mods_metadata,
        set(paths),
    )
    success, sorted_paths = sorter.sort()
    if not success:
        raise ToolError("Sort failed (circular dependencies or invalid graph).")

    sorted_ids: list[str] = []
    for path in sorted_paths:
        pid = ctx.pid_for_path(path)
        sorted_ids.append(pid if pid is not None else path)
    return {
        "method": algorithm,
        "sorted": sorted_ids,
        "count": len(sorted_ids),
        "missing": missing,
    }


def set_active_list(
    ctx: MCPContext,
    package_ids: list[str],
    allow_missing: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    """Replace the active mod list wholesale."""
    if not package_ids:
        raise ToolError("package_ids must not be empty")
    if len(package_ids) != len(set(pid.lower() for pid in package_ids)):
        raise ToolError("package_ids contains duplicates")

    if not allow_missing:
        unknown = sorted({pid for pid in package_ids if not ctx.paths_for_pid(pid)})
        if unknown:
            raise ToolError(
                "Unknown packageIds (use allow_missing=true to write anyway): "
                + ", ".join(unknown)
            )

    result = ctx.write_active_config(package_ids, backup=backup)
    return {"written": result["written"], "backup": result["backup"]}


def update_active_list(
    ctx: MCPContext,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    move_id: str = "",
    move_to_index: int = -1,
    allow_missing: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    """Apply delta edits to the active list and save it.

    Operates on the raw ModsConfig order so entries pointing at
    temporarily uninstalled mods are preserved.
    """
    if not (add or remove or move_id):
        raise ToolError("Nothing to do: provide add, remove and/or move_id")

    config = _require_config(ctx)
    ids = [str(pid) for pid in config.activeMods]
    before = len(ids)

    added: list[str] = []
    if add:
        if not allow_missing:
            unknown = sorted({pid for pid in add if not ctx.paths_for_pid(pid)})
            if unknown:
                raise ToolError(
                    "Unknown packageIds in add (use allow_missing=true): "
                    + ", ".join(unknown)
                )
        for pid in add:
            if not any(pid.lower() == existing.lower() for existing in ids):
                ids.append(pid)
                added.append(pid)

    removed: list[str] = []
    if remove:
        remove_set = {pid.lower() for pid in remove}
        kept = [pid for pid in ids if pid.lower() not in remove_set]
        removed = [pid for pid in ids if pid.lower() in remove_set]
        ids = kept

    moved: dict[str, Any] | None = None
    if move_id:
        source_index = next(
            (i for i, pid in enumerate(ids) if pid.lower() == move_id.lower()),
            -1,
        )
        if source_index == -1:
            raise ToolError(f"move_id {move_id!r} is not in the active list")
        target_index = move_to_index if move_to_index >= 0 else len(ids) - 1
        target_index = min(max(target_index, 0), max(len(ids) - 1, 0))
        pid = ids.pop(source_index)
        ids.insert(target_index, pid)
        moved = {"id": pid, "from": source_index, "to": target_index}

    if len(ids) < 1:
        raise ToolError("Refusing to write an empty active mod list")

    result = ctx.write_active_config(ids, backup=backup)
    return {
        "before": before,
        "after": len(ids),
        "added": added,
        "removed": removed,
        "moved": moved,
        "backup": result["backup"],
    }


# ---- Named modpacks ----


def _modpack_path(name: str) -> Path:
    if not _MODPACK_NAME_RE.match(name):
        raise ToolError(
            "Invalid modpack name: use letters, digits, spaces, '-' or '.' "
            "(max 64 chars)"
        )
    return AppInfo().saved_modlists_folder / f"{name}.json"


def save_modpack(ctx: MCPContext, name: str) -> dict[str, Any]:
    """Save the current active list under ``name`` (JSON, overwrites)."""
    config = _require_config(ctx)
    _, missing = ctx.resolve_active_paths(config)
    ids = [str(pid) for pid in config.activeMods]
    payload = {
        "name": name,
        "package_ids": ids,
        "count": len(ids),
        "game_version": ctx.game_version,
        "instance": ctx.instance_name,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "missing_at_save": missing,
    }
    target = _modpack_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    logger.info(f"Saved modpack {name!r} ({len(ids)} mods) to {target}")
    return {
        "name": name,
        "count": len(ids),
        "path": str(target),
        "missing": missing,
    }


def load_modpack(ctx: MCPContext, name: str) -> dict[str, Any]:
    """Read a saved modpack (does not modify the active list)."""
    target = _modpack_path(name)
    if not target.is_file():
        raise ToolError(f"No saved modpack named {name!r} in {target.parent}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"Failed to read modpack {name!r}: {exc}") from exc
    ids = payload.get("package_ids")
    if not isinstance(ids, list):
        raise ToolError(f"Modpack {name!r} is malformed (no package_ids)")
    return {
        "name": name,
        "package_ids": [str(pid) for pid in ids],
        "count": len(ids),
        "game_version": str(payload.get("game_version", "unknown")),
        "saved_at": str(payload.get("saved_at", "unknown")),
    }


def apply_modpack(
    ctx: MCPContext,
    name: str,
    allow_missing: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    """Load a modpack into ModsConfig.xml."""
    pack = load_modpack(ctx, name)
    result = set_active_list(
        ctx,
        pack["package_ids"],
        allow_missing=allow_missing,
        backup=backup,
    )
    return {"name": name, "count": pack["count"], **result}


def list_modpacks() -> dict[str, Any]:
    """List saved modpacks in the RimSort modlists folder."""
    folder = AppInfo().saved_modlists_folder
    packs: list[dict[str, Any]] = []
    if folder.is_dir():
        for target in sorted(folder.glob("*.json")):
            entry: dict[str, Any] = {"name": target.stem, "path": str(target)}
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
                entry["count"] = int(payload.get("count", 0))
                entry["saved_at"] = str(payload.get("saved_at", "unknown"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                entry["count"] = -1
                entry["saved_at"] = "unreadable"
            packs.append(entry)
    return {"modpacks": packs, "count": len(packs), "folder": str(folder)}


def installed_mod_summary(ctx: MCPContext) -> dict[str, Any]:
    """Counts of installed mods by source (used by status reporting)."""
    metadata = ctx.mods_metadata
    by_source: Counter[str] = Counter()
    about_count = 0
    for mod in metadata.values():
        if isinstance(mod, AboutXmlMod):
            about_count += 1
            by_source[mod.mod_type.value] += 1
    return {
        "installed": len(metadata),
        "listed": about_count,
        "by_source": dict(sorted(by_source.items())),
    }
