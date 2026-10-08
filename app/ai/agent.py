"""Agentic loop that drives MCP tools from an AI chat request.

`run_agent` is a plain (non-Qt) function so it can be unit-tested; the
`AgentWorker` QThread wraps it for the GUI, including confirmation
handshakes that block the worker thread until the main thread answers.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from loguru import logger
from PySide6.QtCore import QThread, Signal, Slot

from app.ai.client import AIError, AssistantReply, ChatClient, ToolCall

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

MAX_ROUNDS = 8
CONFIRM_TIMEOUT_S = 1800.0
CANCELLED_TEXT = "Generation stopped."
MAX_ROUNDS_TEXT = (
    "Reached the tool-call limit for this message; summarise what you have so far."
)

#: Tools that write data or launch the game: they ask the user first.
WRITE_TOOLS = frozenset(
    {
        "set_active_modlist",
        "update_modlist",
        "sort_modlist",
        "apply_modpack",
        "save_modpack",
        "launch_game",
        "update_mods",
    }
)

#: Write tools that only need confirmation when not a dry run, with the
#: tool's own default for a missing `dry_run` argument.
DRY_RUN_TOOLS: dict[str, bool] = {
    "sort_modlist": True,
    "launch_game": False,
    "update_mods": False,
}

#: Hidden from the in-app chat (would desync the GUI-selected instance).
CHAT_EXCLUDED_TOOLS = frozenset({"set_instance"})


def requires_confirmation(name: str, arguments: dict[str, Any]) -> bool:
    """Return True when running `name` must be confirmed by the user."""
    if name == "import_modlist":
        # Read-only parse by default; apply=true rewrites ModsConfig.xml.
        return bool(arguments.get("apply"))
    if name not in WRITE_TOOLS:
        return False
    if name in DRY_RUN_TOOLS:
        dry_run = bool(arguments.get("dry_run", DRY_RUN_TOOLS[name]))
        return not dry_run
    return True


def build_agent_tools(server: MCPServer) -> list[dict[str, Any]]:
    """Convert `server.list_tools()` into OpenAI function-calling schemas."""
    openai_tools: list[dict[str, Any]] = []
    for tool in asyncio.run(server.list_tools()):
        if tool.name in CHAT_EXCLUDED_TOOLS:
            continue
        schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": schema,
                },
            }
        )
    return openai_tools


def summarize_args(name: str, arguments: dict[str, Any]) -> str:
    """One-line, length-capped rendering of a tool call for the chat log."""
    rendered = json.dumps(arguments, ensure_ascii=False) if arguments else ""
    if len(rendered) > 120:
        rendered = rendered[:117] + "..."
    return f"{name}({rendered})"


def call_tool_payload(
    server: MCPServer, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Run one tool and normalise its result into a JSON-able dict."""
    try:
        result = asyncio.run(server.call_tool(name, arguments))
    except Exception as exc:
        return {"error": str(exc) or exc.__class__.__name__}
    if getattr(result, "is_error", False):
        return {"error": _result_text(result)}
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    if structured is not None:
        return {"result": structured}
    text = _result_text(result)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"result": text}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _result_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def run_agent(
    *,
    client: ChatClient,
    server: MCPServer,
    messages: list[dict[str, Any]],
    confirm: Callable[[str], bool],
    emit: Callable[[str], None],
    is_cancelled: Callable[[], bool],
    max_rounds: int = MAX_ROUNDS,
) -> str:
    """Run the chat loop until the model stops asking for tools.

    Returns the final assistant text. `messages` is extended in place so
    the chat panel can reuse the full transcript on the next turn.
    """
    tools = build_agent_tools(server)
    for _ in range(max_rounds):
        if is_cancelled():
            return CANCELLED_TEXT
        reply = client.chat(messages, tools=tools)
        if not reply.tool_calls:
            text = reply.content
            messages.append({"role": "assistant", "content": text})
            return text
        messages.append(_assistant_tool_message(reply))
        for call in reply.tool_calls:
            payload = _resolve_call(
                server=server,
                call=call,
                confirm=confirm,
                emit=emit,
                is_cancelled=is_cancelled,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(payload, ensure_ascii=False),
                }
            )
    # Out of rounds: ask once more without tools so the model can summarise.
    reply = client.chat(messages, tools=None)
    text = reply.content
    messages.append({"role": "assistant", "content": text})
    return text


def _assistant_tool_message(reply: AssistantReply) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": reply.content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in reply.tool_calls
        ],
    }


def _resolve_call(
    *,
    server: MCPServer,
    call: ToolCall,
    confirm: Callable[[str], bool],
    emit: Callable[[str], None],
    is_cancelled: Callable[[], bool],
) -> dict[str, Any]:
    pretty = summarize_args(call.name, call.arguments)
    if is_cancelled():
        return {"error": "cancelled"}
    if requires_confirmation(call.name, call.arguments):
        emit(f"… {pretty}")
        if not confirm(pretty):
            emit(f"✗ {pretty} — declined")
            return {"error": "Declined by user"}
        emit(f"→ {pretty}")
        return call_tool_payload(server, call.name, call.arguments)
    emit(f"→ {pretty}")
    return call_tool_payload(server, call.name, call.arguments)


class AgentWorker(QThread):
    """Runs `run_agent` off the GUI thread.

    Confirmation requests block this thread until `resolve_confirmation`
    is called from the GUI thread (or the timeout expires, which counts
    as a decline).
    """

    activity = Signal(str)
    confirm_requested = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        client: ChatClient,
        server_provider: Callable[[], MCPServer],
        messages: list[dict[str, Any]],
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._server_provider = server_provider
        self._messages = messages
        self._cancel = threading.Event()
        self._confirm_event = threading.Event()
        self._confirm_answer = False

    def request_cancel(self) -> None:
        """Ask the loop to stop at the next safe point."""
        self._cancel.set()

    @Slot(bool)
    def resolve_confirmation(self, approved: bool) -> None:
        """Unblock the worker thread waiting in `_confirm`."""
        self._confirm_answer = approved
        self._confirm_event.set()

    def run(self) -> None:
        try:
            server = self._server_provider()
            text = run_agent(
                client=self._client,
                server=server,
                messages=self._messages,
                confirm=self._confirm,
                emit=self.activity.emit,
                is_cancelled=self._cancel.is_set,
            )
        except AIError as exc:
            logger.warning("AI chat request failed: {}", exc)
            self.failed.emit(str(exc))
        except Exception as exc:
            logger.exception("AI chat worker crashed")
            self.failed.emit(f"Unexpected error: {exc}")
        else:
            self.finished_ok.emit(text)

    def _confirm(self, pretty: str) -> bool:
        self._confirm_event.clear()
        self.confirm_requested.emit(pretty)
        if not self._confirm_event.wait(timeout=CONFIRM_TIMEOUT_S):
            return False
        return self._confirm_answer
