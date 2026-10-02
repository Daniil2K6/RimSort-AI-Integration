"""Headless RimWorld launcher for the MCP server.

Mirrors the GUI's launch workflow (steam_appid.txt management, Steam
protocol vs direct executable) without ever showing a dialog.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from loguru import logger
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.context import MCPContext
from app.utils.generic import (
    get_executable_path,
    launch_process,
    parse_launch_command,
    platform_specific_open,
)

RIMWORLD_APP_ID = "294100"


def _steam_appid_path(game_folder: Path) -> Path:
    # On macOS, steam_appid.txt must live outside the .app bundle.
    if sys.platform == "darwin":
        return game_folder.parent / "steam_appid.txt"
    return game_folder / "steam_appid.txt"


def launch_game(ctx: MCPContext, dry_run: bool = False) -> dict[str, Any]:
    """Launch RimWorld (or describe how it would be launched).

    :param dry_run: when True nothing is executed or written; the
        returned plan describes what would happen.
    """
    instance = ctx.instance
    game_folder = ctx.game_folder
    if game_folder is None:
        raise ToolError(f"Instance {ctx.instance_name!r} has no game folder configured")
    if not game_folder.is_dir():
        raise ToolError(f"Game folder does not exist: {game_folder}")

    steam_integration = instance.steam_client_integration
    via_steam = instance.launch_via_steam_protocol

    appid_path = _steam_appid_path(game_folder)
    appid_action = "keep"
    if steam_integration and not appid_path.exists():
        appid_action = "create"
    elif not steam_integration and appid_path.exists():
        appid_action = "remove"

    if via_steam:
        if not steam_integration:
            raise ToolError(
                "Steam protocol launch requires Steam Client Integration "
                "to be enabled in RimSort settings (Settings > Steam)."
            )
        uri = f"steam://rungameid/{RIMWORLD_APP_ID}"
        plan: dict[str, Any] = {
            "method": "steam_protocol",
            "uri": uri,
            "steam_appid_action": appid_action,
            "steam_appid_path": str(appid_path),
            "note": "Steam overlay enabled; run_args are ignored in this mode",
        }
        if dry_run:
            return {"dry_run": True, **plan}
        _apply_appid(appid_action, appid_path)
        platform_specific_open(uri)
        logger.info(f"Launched RimWorld via {uri}")
        return {"launched": True, **plan}

    executable = get_executable_path(game_folder)
    if not executable:
        raise ToolError(
            f"No RimWorld executable found in {game_folder}. "
            "Check the game folder in RimSort instance settings."
        )

    run_args = instance.run_args
    if run_args:
        parsed = parse_launch_command(run_args)
        env_vars = parsed.env_vars
        wrappers = parsed.wrapper_commands
        game_args = parsed.game_args
    else:
        env_vars = {}
        wrappers = []
        game_args = []

    plan = {
        "method": "direct",
        "executable": executable,
        "args": list(game_args),
        "env_vars": sorted(env_vars),
        "wrappers": list(wrappers),
        "cwd": str(game_folder),
        "steam_appid_action": appid_action,
        "steam_appid_path": str(appid_path),
    }
    if dry_run:
        return {"dry_run": True, **plan}

    _apply_appid(appid_action, appid_path)
    pid, popen_args = launch_process(
        executable,
        game_args,
        str(game_folder),
        env_vars=env_vars,
        wrapper_commands=wrappers,
    )
    logger.info(f"Launched RimWorld directly with PID {pid}")
    return {"launched": True, "pid": pid, "popen_args": popen_args, **plan}


def _apply_appid(action: str, path: Path) -> None:
    if action == "create":
        try:
            path.write_text(RIMWORLD_APP_ID, encoding="utf-8")
        except OSError as exc:
            logger.warning(f"Unable to create {path}: {exc}")
    elif action == "remove":
        try:
            path.unlink()
        except OSError as exc:
            logger.warning(f"Unable to remove {path}: {exc}")
