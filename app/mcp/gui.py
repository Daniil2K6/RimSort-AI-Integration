"""MCP GUI tools routed through the main-thread :class:`GuiActionBridge`.

These give the agent a *bounded*, validated way to drive the real RimSort UI
(Stage C). When no bridge is attached (headless / external stdio server) every
tool answers ``gui_unavailable`` so domain tools keep working; nothing is
faked. Execution happens in the Qt main thread via the bridge, and the tool
returns the real acknowledgement.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from app.ai.gui_bridge import DEFAULT_GUI_TIMEOUT, GuiActionBridge

#: Headless reply for every GUI tool when no bridge is registered.
_UNAVAILABLE: dict[str, Any] = {
    "ok": False,
    "code": "gui_unavailable",
    "error": (
        "GUI tools need the RimSort desktop app (a bridge is attached). "
        "They are unavailable in headless mode; domain tools still work."
    ),
}


def register_gui_tools(mcp: MCPServer, bridge: GuiActionBridge | None) -> None:
    """Register the GUI action tools, bound to ``bridge`` when present."""

    def _run(action: str, params: dict[str, Any]) -> dict[str, Any]:
        if bridge is None:
            return dict(_UNAVAILABLE)
        return bridge.submit(action, params, timeout=DEFAULT_GUI_TIMEOUT)

    @mcp.tool()
    def get_gui_state(detail: str = "summary") -> dict[str, Any]:
        """Bounded GUI state: visible surfaces, available targets/actions.

        detail: "summary" (fast, current tab + counts) or "full" (adds filter
        text and selection). Never returns a widget tree or secrets. Call this
        before other gui_* tools to learn valid targets.
        """
        if bridge is None:
            return dict(_UNAVAILABLE)
        return bridge.get_state(detail=detail)

    @mcp.tool()
    def gui_navigate(target: str, tab_id: str = "") -> dict[str, Any]:
        """Switch the visible surface/tab by stable id (see get_gui_state).

        target: a SurfaceId value such as "mods_tab" or "log_tab".
        tab_id: optional explicit tab objectName for future-proofing.
        """
        return _run("navigate", {"target": target, "tab_id": tab_id})

    @mcp.tool()
    def gui_select_mod(package_id: str, reveal: bool = True) -> dict[str, Any]:
        """Select a mod in the active or inactive list and scroll to it.

        package_id: the mod's packageId (from list_mods).
        reveal: also switch to the mods tab so the selection is visible.
        """
        return _run("select_mod", {"package_id": package_id, "reveal": reveal})

    @mcp.tool()
    def gui_set_mod_filter(query: str, list: str = "inactive") -> dict[str, Any]:
        """Set the search box on a mod list (reuses the existing filter UI).

        query: the search text.
        list: "active" or "inactive".
        """
        return _run("set_filter", {"query": query, "list": list})

    @mcp.tool()
    def gui_open_workshop_item(publishedfileid: str) -> dict[str, Any]:
        """Open a Steam Workshop item in the built-in browser.

        publishedfileid: numeric Steam Workshop file id. Only validated HTTPS
        steamcommunity.com URLs are opened.
        """
        return _run("open_workshop_item", {"publishedfileid": publishedfileid})

    @mcp.tool()
    def gui_invoke_action(action_id: str) -> dict[str, Any]:
        """Trigger one allowlisted UI action (e.g. "open_settings").

        Only reversible, safe actions are permitted; the allowlist is enforced
        by the bridge. Anything else returns gui_unknown_action.
        """
        return _run("invoke_action", {"action_id": action_id})


__all__ = ["register_gui_tools"]
