"""Main-thread GUI action bridge for the agent (Stage C).

The agent runs off the GUI thread, so it cannot touch widgets directly.
It submits a *semantic* command to :class:`GuiActionBridge`, which lives in
the Qt main thread and executes it against registered surfaces, returning an
acknowledgement with the real resulting state.

Waiting uses a queued signal plus a ``threading.Event`` (never
``BlockingQueuedConnection``, which deadlocks on shutdown). When ``submit``
is called from the GUI thread itself it dispatches inline so tests and
in-thread callers do not deadlock.

Real widget executors are registered by the main window against stable
:class:`SurfaceId` keys; this module stays free of concrete widget knowledge
so it is unit-testable with fake executors. Headless callers get
``gui_unavailable``.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

#: Default acknowledgement wait, seconds.
DEFAULT_GUI_TIMEOUT = 5.0

#: Only validated Steam HTTPS Workshop URLs are navigable.
_WORKSHOP_URL_RE = re.compile(
    r"^https://(steamcommunity\.com/sharedfiles/filedetails/\?id=\d+"
    r"|steamcommunity\.com/workshop/filedetails/\?id=\d+)$"
)


class SurfaceId(str, Enum):
    """Stable identifiers for real, existing surfaces (no invented ones)."""

    MAIN_WINDOW = "main_window"
    MODS_TAB = "mods_tab"
    PLAYER_LOG_TAB = "player_log_tab"
    ACF_LOG_TAB = "acf_log_tab"
    FILE_SEARCH_TAB = "file_search_tab"
    TROUBLESHOOTING_TAB = "troubleshooting_tab"
    SETTINGS_DIALOG = "settings_dialog"
    ACTIVE_MODS_PANEL = "active_mods_panel"
    INACTIVE_MODS_PANEL = "inactive_mods_panel"
    WORKSHOP_BROWSER = "workshop_browser"


#: Allowlist for ``gui_invoke_action``: safe, reversible UI actions only.
#: Every id must map to a real, existing action (no invented surfaces).
INVOKE_ALLOWLIST = frozenset(
    {
        "open_settings",
        "refresh_mods",
        "clear_chat",
        "toggle_ai_chat",
    }
)

#: Actions the bridge can dispatch (mirrors the documented tool set).
SUPPORTED_ACTIONS = frozenset(
    {
        "get_state",
        "navigate",
        "select_mod",
        "set_filter",
        "open_workshop_item",
        "invoke_action",
        "get_modlist",
        "apply_modlist",
    }
)


def validate_workshop_url(publishedfileid: str) -> str | None:
    """Return the canonical Workshop URL for a valid numeric id, else None."""
    if not publishedfileid.isdigit():
        return None
    url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={publishedfileid}"
    return url if _WORKSHOP_URL_RE.match(url) else None


@dataclass
class GuiRequest:
    """One pending GUI command the agent waits on."""

    action: str
    params: dict[str, Any]
    action_id: str = ""
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None


class GuiActionBridge(QObject):
    """Executes semantic GUI commands in the main thread (Stage C).

    Executors are ``(params) -> dict`` callables registered per action. The
    main window registers real ones; tests register fakes. With no executor
    for an action the bridge returns a structured ``gui_unavailable``.
    """

    #: Queued to the main thread so the GUI thread performs the mutation.
    _dispatch_signal = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._executors: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self._targets: list[str] = []
        self._actions: list[str] = []
        self._dispatch_signal.connect(self._handle, Qt.ConnectionType.QueuedConnection)

    # -- registration ----------------------------------------------------
    def register_executor(
        self, action: str, executor: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> None:
        """Attach the main-thread implementation for one action."""
        self._executors[action] = executor

    def set_capabilities(self, targets: list[str], actions: list[str]) -> None:
        """Publish the bounded target/action lists reported by get_state."""
        self._targets = list(targets)
        self._actions = list(actions)

    def has_executor(self, action: str) -> bool:
        return action in self._executors

    # -- submission ------------------------------------------------------
    def submit(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        action_id: str = "",
        timeout: float = DEFAULT_GUI_TIMEOUT,
    ) -> dict[str, Any]:
        """Run one action and return its acknowledgement (thread-safe).

        From a worker thread this queues to the GUI thread and waits up to
        ``timeout``. From the GUI thread it dispatches inline.
        """
        params = params or {}
        if action not in SUPPORTED_ACTIONS:
            return _err("gui_unknown_action", f"Unsupported action {action!r}")
        if action not in self._executors:
            return _err(
                "gui_unavailable",
                f"Action {action!r} is unavailable in this mode (headless?).",
                allowed=sorted(SUPPORTED_ACTIONS),
            )
        request = GuiRequest(action=action, params=params, action_id=action_id)
        if QThread.currentThread() is self.thread():
            self._handle(request)
        else:
            self._dispatch_signal.emit(request)
            if not request.done.wait(timeout):
                request.done.set()  # Skip queued requests that have not started.
                return _err(
                    "gui_timeout",
                    f"GUI action {action!r} did not acknowledge in {timeout:.1f}s.",
                )
        return request.result or _err("gui_error", "No result produced.")

    # -- execution (main thread) -----------------------------------------
    @Slot(object)
    def _handle(self, request: GuiRequest) -> None:
        if request.done.is_set():
            return
        try:
            request.result = self._execute(request.action, request.params)
        except Exception as exc:
            request.result = _err("gui_error", str(exc))
        finally:
            request.done.set()

    def _execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        error = _validate(action, params)
        if error is not None:
            return error
        executor = self._executors.get(action)
        if executor is None:
            return _err("gui_unavailable", f"Action {action!r} not available.")
        result = executor(params)
        # Always stamp the acknowledged action + a state revision when given.
        out = dict(result)
        out.setdefault("ok", True)
        out.setdefault("action", action)
        if action == "get_state":
            out.setdefault("targets", list(self._targets))
            out.setdefault("actions", list(self._actions))
            out.setdefault(
                "available_actions", sorted(SUPPORTED_ACTIONS & set(self._executors))
            )
        return out

    # -- state -----------------------------------------------------------
    def get_state(self, detail: str = "summary") -> dict[str, Any]:
        """Bounded GUI state: surfaces, targets, actions, no widget tree."""
        state: dict[str, Any] = {
            "ok": True,
            "detail": detail,
            "targets": list(self._targets),
            "actions": list(self._actions),
            "available_actions": sorted(SUPPORTED_ACTIONS & set(self._executors)),
        }
        state.update(self.submit("get_state", {"detail": detail}))
        return state


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "code": code, "error": message}
    payload.update(extra)
    return payload


def _validate(action: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Per-action parameter validation; returns an error dict or None."""
    if action == "open_workshop_item":
        url = validate_workshop_url(str(params.get("publishedfileid", "")))
        if url is None:
            return _err(
                "gui_invalid_url",
                "publishedfileid must be a positive numeric Steam Workshop id.",
            )
        params["url"] = url
    elif action == "invoke_action":
        action_id = str(params.get("action_id", ""))
        if action_id not in INVOKE_ALLOWLIST:
            return _err(
                "gui_unknown_action",
                f"Action id {action_id!r} is not in the allowlist.",
                allowed=sorted(INVOKE_ALLOWLIST),
            )
    elif action == "set_filter":
        list_name = str(params.get("list", "inactive"))
        if list_name not in ("active", "inactive"):
            return _err(
                "gui_invalid_list",
                "list must be 'active' or 'inactive'.",
                allowed=["active", "inactive"],
            )
    elif action == "select_mod":
        if not str(params.get("package_id", "")).strip():
            return _err("gui_invalid_package", "package_id must be non-empty.")
    return None
