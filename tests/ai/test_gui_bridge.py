"""Tests for the main-thread GUI action bridge (Stage C)."""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import QCoreApplication

from app.ai.gui_bridge import (
    INVOKE_ALLOWLIST,
    SUPPORTED_ACTIONS,
    GuiActionBridge,
    SurfaceId,
    validate_workshop_url,
)


def _bridge_with(
    **executors: Any,
) -> tuple[GuiActionBridge, dict[str, list[dict[str, Any]]]]:
    bridge = GuiActionBridge()
    seen: dict[str, list[dict[str, Any]]] = {}
    for action, fn in executors.items():
        seen[action] = []

        def wrapper(
            params: dict[str, Any],
            _a: str = action,
            _fn: Any = fn,
            _s: dict[str, list[dict[str, Any]]] = seen,
        ) -> dict[str, Any]:
            _s[_a].append(params)
            return _fn(params)

        bridge.register_executor(action, wrapper)
    return bridge, seen


def test_unknown_action_is_rejected() -> None:
    bridge = GuiActionBridge()
    result = bridge.submit("launch_missiles", {})
    assert result["ok"] is False
    assert result["code"] == "gui_unknown_action"


def test_unregistered_action_returns_gui_unavailable() -> None:
    bridge = GuiActionBridge()
    result = bridge.submit("navigate", {"target": "mods_tab"})
    assert result["ok"] is False
    assert result["code"] == "gui_unavailable"
    assert "navigate" in result["allowed"]


def test_executor_runs_and_stamps_ack() -> None:
    bridge, _ = _bridge_with(navigate=lambda p: {"target": p["target"]})
    result = bridge.submit("navigate", {"target": SurfaceId.MODS_TAB.value})
    assert result["ok"] is True
    assert result["action"] == "navigate"
    assert result["target"] == "mods_tab"


def test_workshop_url_validation() -> None:
    assert validate_workshop_url("123456") is not None
    assert validate_workshop_url("abc") is None
    assert validate_workshop_url("-5") is None
    assert validate_workshop_url("") is None

    bridge, seen = _bridge_with(open_workshop_item=lambda p: {"url": p["url"]})
    ok = bridge.submit("open_workshop_item", {"publishedfileid": "123456"})
    assert ok["ok"] is True
    assert "id=123456" in seen["open_workshop_item"][0]["url"]

    bad = bridge.submit("open_workshop_item", {"publishedfileid": "not-a-number"})
    assert bad["ok"] is False
    assert bad["code"] == "gui_invalid_url"


def test_invoke_action_allowlist() -> None:
    bridge, seen = _bridge_with(invoke_action=lambda p: {"acted": p["action_id"]})
    ok = bridge.submit("invoke_action", {"action_id": "open_settings"})
    assert ok["ok"] is True
    assert ok["acted"] == "open_settings"

    bad = bridge.submit("invoke_action", {"action_id": "delete_everything"})
    assert bad["ok"] is False
    assert bad["code"] == "gui_unknown_action"
    assert "delete_everything" not in INVOKE_ALLOWLIST
    assert seen["invoke_action"] == [{"action_id": "open_settings"}]


def test_set_filter_validates_list_name() -> None:
    bridge, _ = _bridge_with(set_filter=lambda p: {"count": 3, "list": p["list"]})
    ok = bridge.submit("set_filter", {"query": "ce", "list": "inactive"})
    assert ok["ok"] is True
    bad = bridge.submit("set_filter", {"query": "x", "list": "bogus"})
    assert bad["code"] == "gui_invalid_list"


def test_select_mod_requires_package_id() -> None:
    bridge, _ = _bridge_with(select_mod=lambda p: {"selected": p["package_id"]})
    ok = bridge.submit("select_mod", {"package_id": "brrainz.hugslib"})
    assert ok["ok"] is True
    bad = bridge.submit("select_mod", {"package_id": "   "})
    assert bad["code"] == "gui_invalid_package"


def test_get_state_reports_bounded_capabilities() -> None:
    bridge, _ = _bridge_with(get_state=lambda p: {"active_tab": "mods"})
    bridge.set_capabilities(
        targets=[SurfaceId.MODS_TAB.value, SurfaceId.PLAYER_LOG_TAB.value],
        actions=["open_settings"],
    )
    state = bridge.get_state()
    assert state["ok"] is True
    assert state["targets"] == ["mods_tab", "player_log_tab"]
    assert state["actions"] == ["open_settings"]
    assert state["active_tab"] == "mods"
    # No full widget tree / secrets leaked.
    assert "widgets" not in state


def test_supported_actions_cover_documented_tools() -> None:
    assert {
        "get_state",
        "navigate",
        "select_mod",
        "set_filter",
        "open_workshop_item",
        "invoke_action",
    } <= SUPPORTED_ACTIONS


def test_executor_exception_becomes_gui_error() -> None:
    def boom(_params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    bridge = GuiActionBridge()
    bridge.register_executor("navigate", boom)
    result = bridge.submit("navigate", {"target": "mods_tab"})
    assert result["ok"] is False
    assert result["code"] == "gui_error"
    assert "kaboom" in result["error"]


def test_submit_from_worker_thread_times_out_when_gui_stalled() -> None:
    """A worker-thread submit with a stalled GUI returns gui_timeout."""
    bridge, _ = _bridge_with(navigate=lambda p: {"target": p["target"]})
    # Disconnect the main-thread handler so the request never completes.
    bridge._dispatch_signal.disconnect(bridge._handle)
    box: dict[str, Any] = {}

    def worker() -> None:
        box["result"] = bridge.submit("navigate", {"target": "mods"}, timeout=0.0)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert box["result"]["code"] == "gui_timeout"


def test_submit_from_worker_thread_succeeds_when_gui_pumps(
    qtbot: Any, qapp: QCoreApplication
) -> None:
    """A worker-thread submit completes once the GUI thread processes events."""
    bridge, _ = _bridge_with(navigate=lambda p: {"target": p["target"]})
    box: dict[str, Any] = {}

    def worker() -> None:
        box["result"] = bridge.submit("navigate", {"target": "log_tab"}, timeout=5.0)

    thread = threading.Thread(target=worker)
    thread.start()
    # Pump the Qt event loop so the queued dispatch runs in the main thread.
    qtbot.waitUntil(lambda: thread.is_alive() is False, timeout=3000)
    thread.join(timeout=1.0)
    assert box["result"]["ok"] is True
    assert box["result"]["target"] == "log_tab"
