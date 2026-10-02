"""MCP server assembly and stdio entry point.

stdout is reserved for the MCP protocol; all logging goes to stderr.
"""

from __future__ import annotations

import sys

from loguru import logger
from mcp.server.mcpserver import MCPServer

from app.mcp.context import MCPContext, MCPContextError
from app.mcp.tools import register_tools
from app.utils.app_info import AppInfo

INSTRUCTIONS = (
    "RimSort manages RimWorld mod lists for the current instance. "
    "Start with get_status; use list_mods/get_mod_details to discover mods "
    "and their load-order rules; build lists with set_active_modlist or "
    "update_modlist; order them with sort_modlist (dry_run=true first); "
    "always finish with validate_modlist (ok=true) and persist via "
    "save_modpack. Every write backs up ModsConfig.xml; launch_game "
    "supports dry_run=true to preview the launch."
)


def build_server(ctx: MCPContext) -> MCPServer:
    """Create the MCPServer with all RimSort tools registered."""
    server = MCPServer(
        name="rimsort",
        instructions=INSTRUCTIONS,
        version=AppInfo().app_version,
    )
    register_tools(server, ctx)
    return server


def run_stdio(log_level: str = "INFO") -> None:
    """Run the MCP server over stdio (blocks until the client exits)."""
    # stdout belongs to the MCP protocol; log to stderr only.
    logger.remove()
    logger.add(sys.stderr, level=log_level.upper(), colorize=False)

    ctx = MCPContext()
    try:
        count = ctx.refresh()
        logger.info(
            f"MCP server starting: instance={ctx.instance_name!r}, "
            f"mods={count}, game_version={ctx.game_version}"
        )
    except MCPContextError as exc:
        # Stay alive: get_status(refresh=true) can retry after the user
        # fixes settings.json.
        logger.warning(f"Initial scan failed, tools will retry lazily: {exc}")

    server = build_server(ctx)
    server.run(transport="stdio")
