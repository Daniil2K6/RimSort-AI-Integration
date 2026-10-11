"""Headless dependency / conflict / log analysis for the MCP server (Stage E).

Pure functions over the scanned mod metadata and the compiled dependency
graphs — no Qt, no disk writes. Expected failures raise ``ToolError``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.context import MCPContext
from app.models.metadata.metadata_structure import AboutXmlMod

#: Cap on reported error blocks from Player.log.
_LOG_ERROR_CAP = 25

#: Lines that start a Unity/RimWorld exception block.
_EXCEPTION_START = re.compile(
    r"^(Exception|Error|ArgumentNullException|NullReferenceException|"
    r"MissingMethodException|TypeLoadException|UnityException|"
    r"ArgumentOutOfRangeException|KeyNotFoundException|"
    r"InvalidOperationException)\b"
)

#: A mod-folder hint inside a stack frame (…/Mods/<Folder>/...).
_MOD_FRAME = re.compile(r"[/\\]Mods[/\\]([^/\\]+)[/\\]")


def _mod_by_pid(ctx: MCPContext, package_id: str) -> AboutXmlMod | None:
    for path in ctx.paths_for_pid(package_id):
        mod = ctx.mods_metadata.get(path)
        if isinstance(mod, AboutXmlMod):
            return mod
    return None


def resolve_dependencies(
    ctx: MCPContext,
    package_ids: list[str],
    include_load_after: bool = False,
) -> dict[str, Any]:
    """Required dependencies for a set of mods, and which are not yet active.

    ``include_load_after`` also pulls in soft load-order predecessors.
    """
    if not package_ids:
        raise ToolError("package_ids must not be empty")
    working = {pid.lower() for pid in ctx.working_ids()}

    needed: dict[str, set[str]] = {}
    for pid in package_ids:
        mod = _mod_by_pid(ctx, pid)
        if mod is None:
            continue
        rules = mod.overall_rules
        deps = {str(dep) for dep in rules.dependencies}
        if include_load_after:
            deps |= {str(dep) for dep in rules.load_after}
        for dep in deps:
            needed.setdefault(dep, set()).add(pid)

    entries: list[dict[str, Any]] = []
    missing_active: list[str] = []
    not_installed: list[str] = []
    for dep in sorted(needed):
        installed = bool(ctx.paths_for_pid(dep))
        active = dep.lower() in working
        entries.append(
            {
                "dependency": dep,
                "required_by": sorted(needed[dep]),
                "installed": installed,
                "active": active,
            }
        )
        if not active:
            missing_active.append(dep)
        if not installed:
            not_installed.append(dep)
    return {
        "dependencies": entries,
        "count": len(entries),
        "missing_from_active": missing_active,
        "not_installed": not_installed,
        "include_load_after": include_load_after,
    }


def find_conflicts(
    ctx: MCPContext, package_ids: list[str] | None = None
) -> dict[str, Any]:
    """Incompatible pairs among the given set (default: the working list)."""
    ids = package_ids if package_ids is not None else ctx.working_ids()
    active_set = {pid.lower() for pid in ids}
    incompat = ctx.compiled_data.incompatibilities

    pairs: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for pid, targets in incompat.items():
        if pid.lower() not in active_set:
            continue
        for target in targets:
            if target.lower() not in active_set:
                continue
            key = tuple(sorted((pid.lower(), target.lower())))
            if key in seen:
                continue
            seen.add(key)
            pairs.append([key[0], key[1]])
    pairs.sort()
    return {
        "conflicting_pairs": pairs[:_LOG_ERROR_CAP],
        "count": len(pairs),
        "truncated": len(pairs) > _LOG_ERROR_CAP,
        "checked": len(ids),
    }


def _player_log_path(ctx: MCPContext) -> Path:
    config_folder = ctx.instance.config_folder
    if not config_folder:
        raise ToolError("Instance has no config folder; cannot locate Player.log")
    return Path(config_folder).parent / "Player.log"


def analyze_player_log(
    ctx: MCPContext, max_errors: int = _LOG_ERROR_CAP
) -> dict[str, Any]:
    """Scan Player.log for exception blocks and implicated mod folders."""
    path = _player_log_path(ctx)
    if not path.is_file():
        return {"ok": False, "code": "log_not_found", "path": str(path)}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(f"Unable to read Player.log: {exc}") from exc

    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []
    current: list[str] | None = None
    for line in lines:
        stripped = line.strip()
        if _EXCEPTION_START.match(stripped):
            if current is not None:
                blocks.append(_finish_block(current))
            current = [stripped]
        elif current is not None:
            # Continuation: stack frames and inner-exception lines.
            if stripped.startswith(("at ", "--- ", "UnityEngine", "Inner")) or (
                stripped and not stripped.startswith("[")
            ):
                current.append(stripped)
            else:
                blocks.append(_finish_block(current))
                current = None
    if current is not None:
        blocks.append(_finish_block(current))

    capped = blocks[: max(1, min(max_errors, _LOG_ERROR_CAP))]
    return {
        "ok": True,
        "path": str(path),
        "error_count": len(blocks),
        "errors": capped,
        "truncated": len(blocks) > len(capped),
    }


def _finish_block(lines: list[str]) -> dict[str, Any]:
    mod_folders: list[str] = []
    for frame in lines:
        for match in _MOD_FRAME.finditer(frame):
            folder = match.group(1)
            if folder not in mod_folders:
                mod_folders.append(folder)
    return {
        "message": lines[0] if lines else "",
        "mod_folders": mod_folders,
        "depth": len(lines),
    }


__all__ = ["analyze_player_log", "find_conflicts", "resolve_dependencies"]
