"""MCP server assembly and stdio entry point.

stdout is reserved for the MCP protocol; all logging goes to stderr.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from loguru import logger
from mcp.server.mcpserver import MCPServer

from app.mcp.context import MCPContext, MCPContextError
from app.mcp.tools import register_tools
from app.utils.app_info import AppInfo

if TYPE_CHECKING:
    from app.ai.gui_bridge import GuiActionBridge


INSTRUCTIONS = (
    "RimSort manages RimWorld mod lists for the current instance. "
    "Start with get_status; use list_mods/get_mod_details to discover mods "
    "and their load-order rules; build lists with set_active_modlist or "
    "update_modlist; order them with sort_modlist (dry_run=true first); "
    "validate changes with validate_modlist and report any remaining issues; "
    "save_modpack only when requested or needed. For revision-guarded edits "
    "use get_modlist_state + preview_modlist_changes + apply_modlist_plan "
    "with expected_revision. Do not retry writes blindly after an error. "
    "GUI tools require an attached bridge; file reads do not prove that "
    "the visible GUI list contains the same unsaved edits. "
    "import_modlist loads a mod list from a RimWorld save "
    "(list_game_saves finds them). check_workshop_updates finds outdated "
    "Workshop mods and update_mods refreshes them via SteamCMD. Mod-list "
    "writes back up ModsConfig.xml by default; launch_game supports dry_run=true to "
    "preview the launch."
)


def build_server(ctx: MCPContext, bridge: GuiActionBridge | None = None) -> MCPServer:
    """Create the MCPServer with all RimSort tools registered.

    ``bridge`` enables the gui_* tools that drive the live UI; without it they
    answer gui_unavailable (headless / external stdio server).
    """
    server = MCPServer(
        name="rimsort",
        instructions=INSTRUCTIONS,
        version=AppInfo().app_version,
    )
    ctx.gui_bridge = bridge
    register_tools(server, ctx, bridge)
    return server


def run_stdio(log_level: str = "INFO", headless: bool = False) -> None:
    """Run the MCP server over stdio (blocks until the client exits)."""
    # stdout belongs to the MCP protocol; log to stderr only.
    logger.remove()
    logger.add(sys.stderr, level=log_level.upper(), colorize=False)

    # A core application enables cross-platform Qt local sockets without
    # opening any widgets or requiring a display server.
    from PySide6.QtCore import QCoreApplication

    from app.utils.gui_ipc import RemoteGuiBridge

    core_app = QCoreApplication.instance() or QCoreApplication([])
    bridge = None
    if not headless:
        remote = RemoteGuiBridge()
        if remote.get_state().get("ok"):
            bridge = remote
            logger.info("MCP attached to running RimSort GUI")
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

    server = build_server(ctx, bridge)  # type: ignore[arg-type]
    server.run(transport="stdio")
    del core_app
