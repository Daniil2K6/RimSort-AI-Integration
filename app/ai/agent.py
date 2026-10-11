"""Agentic loop that drives MCP tools from an AI chat request.

`run_agent` is a plain (non-Qt) function so it can be unit-tested; the
`AgentWorker` QThread wraps it for the GUI, including confirmation
handshakes that block the worker thread until the main thread answers.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from loguru import logger
from PySide6.QtCore import QThread, Signal, Slot

from app.ai.client import AIError, AssistantReply, ChatClient, ToolCall
from app.ai.events import AgentEvent, EventEmitter, EventKind, EventStatus

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
        "delete_mod",
    }
)

#: Write tools that only need confirmation when not a dry run, with the
#: tool's own default for a missing `dry_run` argument.
DRY_RUN_TOOLS: dict[str, bool] = {
    "sort_modlist": True,
    "launch_game": False,
    "update_mods": False,
    "delete_mod": False,
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
    on_text: Callable[[str], None] | None = None,
    on_event: Callable[[AgentEvent], None] | None = None,
    max_rounds: int = MAX_ROUNDS,
) -> str:
    """Run the chat loop until the model stops asking for tools.

    Returns the final assistant text. `messages` is extended in place so
    the chat panel can reuse the full transcript on the next turn. When
    `on_text` is given, the client streams and every content delta is
    forwarded to it as it arrives.

    Each round is one assistant *message* with a stable ``message_id`` and
    an incrementing ``round_id``. A ``MESSAGE_COMPLETED`` event carries the
    round's text plus ``is_final`` so the GUI can render intermediate
    (tool-calling) turns separately from the final answer (Stage A.4).
    """
    emitter = EventEmitter(uuid.uuid4().hex, sink=on_event)
    emitter.emit(EventKind.RUN_STARTED, status=EventStatus.RUNNING)
    tools = build_agent_tools(server)
    for round_index in range(max_rounds):
        if is_cancelled():
            emitter.emit(EventKind.RUN_CANCELLED, status=EventStatus.CANCELLED)
            return CANCELLED_TEXT
        reply = _stream_round(
            client=client,
            messages=messages,
            tools=tools,
            is_cancelled=is_cancelled,
            on_text=on_text,
            emitter=emitter,
            round_index=round_index,
        )
        if is_cancelled():
            emitter.emit(EventKind.RUN_CANCELLED, status=EventStatus.CANCELLED)
            return CANCELLED_TEXT
        if not reply.tool_calls:
            _finish_message(emitter, reply.content, is_final=True)
            emitter.emit(
                EventKind.RUN_COMPLETED,
                reply.content,
                status=EventStatus.SUCCEEDED,
            )
            messages.append({"role": "assistant", "content": reply.content})
            return reply.content
        _finish_message(emitter, reply.content, is_final=False)
        messages.append(_assistant_tool_message(reply))
        for call in reply.tool_calls:
            payload = _resolve_call(
                server=server,
                call=call,
                confirm=confirm,
                emit=emit,
                is_cancelled=is_cancelled,
                emitter=emitter,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(payload, ensure_ascii=False),
                }
            )
    # Out of rounds: ask once more without tools so the model can summarise.
    if is_cancelled():
        emitter.emit(EventKind.RUN_CANCELLED, status=EventStatus.CANCELLED)
        return CANCELLED_TEXT
    reply = _stream_round(
        client=client,
        messages=messages,
        tools=None,
        is_cancelled=is_cancelled,
        on_text=on_text,
        emitter=emitter,
        round_index=max_rounds,
    )
    text = reply.content
    _finish_message(emitter, text, is_final=True)
    emitter.emit(EventKind.RUN_COMPLETED, text, status=EventStatus.SUCCEEDED)
    messages.append({"role": "assistant", "content": text})
    return text


def _stream_round(
    *,
    client: ChatClient,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    is_cancelled: Callable[[], bool],
    on_text: Callable[[str], None] | None,
    emitter: EventEmitter,
    round_index: int,
) -> AssistantReply:
    """Run one chat completion, tagging it with a round/message id."""
    emitter.round_id = round_index
    emitter.message_id = uuid.uuid4().hex[:12]
    emitter.emit(
        EventKind.MESSAGE_STARTED,
        round_id=round_index,
        message_id=emitter.message_id,
        status=EventStatus.RUNNING,
    )
    return client.chat(messages, tools=tools, on_text=on_text, should_stop=is_cancelled)


def _finish_message(emitter: EventEmitter, text: str, *, is_final: bool) -> None:
    """Emit MESSAGE_COMPLETED so the GUI can split intermediate vs final."""
    emitter.emit(
        EventKind.MESSAGE_COMPLETED,
        text,
        round_id=emitter.round_id,
        message_id=emitter.message_id,
        status=EventStatus.SUCCEEDED,
        payload={"is_final": is_final, "chars": len(text)},
    )


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
    emitter: EventEmitter | None = None,
) -> dict[str, Any]:
    pretty = summarize_args(call.name, call.arguments)
    if is_cancelled():
        return {"error": "cancelled"}
    if requires_confirmation(call.name, call.arguments):
        emit(f"… {pretty}")
        _emit_tool(
            emitter, EventKind.APPROVAL_REQUESTED, call, pretty, EventStatus.REQUESTED
        )
        if not confirm(pretty):
            if is_cancelled():
                return {"error": "cancelled"}
            emit(f"✗ {pretty} — declined")
            _emit_tool(
                emitter, EventKind.TOOL_FAILED, call, pretty, EventStatus.DECLINED
            )
            return {"error": "Declined by user"}
        emit(f"→ {pretty}")
        _emit_tool(emitter, EventKind.TOOL_STARTED, call, pretty, EventStatus.RUNNING)
        return _run_and_report(server, call, pretty, emitter)
    emit(f"→ {pretty}")
    _emit_tool(emitter, EventKind.TOOL_STARTED, call, pretty, EventStatus.RUNNING)
    return _run_and_report(server, call, pretty, emitter)


def _run_and_report(
    server: MCPServer,
    call: ToolCall,
    pretty: str,
    emitter: EventEmitter | None,
) -> dict[str, Any]:
    """Execute one tool and emit its completion event (succeeded/failed)."""
    result = call_tool_payload(server, call.name, call.arguments)
    status = EventStatus.FAILED if "error" in result else EventStatus.SUCCEEDED
    _emit_tool(
        emitter,
        EventKind.TOOL_COMPLETED
        if status is EventStatus.SUCCEEDED
        else EventKind.TOOL_FAILED,
        call,
        pretty,
        status,
    )
    return result


def _emit_tool(
    emitter: EventEmitter | None,
    kind: EventKind,
    call: ToolCall,
    pretty: str,
    status: EventStatus,
) -> None:
    if emitter is None:
        return
    emitter.emit(
        kind,
        pretty,
        status=status,
        tool_call_id=call.id,
        payload={"tool": call.name},
    )


class AgentWorker(QThread):
    """Runs `run_agent` off the GUI thread.

    Confirmation requests block this thread until `resolve_confirmation`
    is called from the GUI thread (or the timeout expires, which counts
    as a decline).
    """

    activity = Signal(str)
    #: Structured per-run events (AgentEvent) delivered over a queued
    #: connection so the GUI can render a live action journal (Stage B).
    agent_event = Signal(object)
    stream_text = Signal(str)
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
        """Ask the loop to stop now, not at the next safe point only.

        Unblocks a worker waiting in ``_confirm`` and aborts any in-flight
        network read via the client, so cancel lands within ~2s (Stage A.5).
        """
        self._cancel.set()
        # Unblock a worker parked in _confirm (treat as declined for now;
        # the is_cancelled check reclassifies it as cancelled).
        self._confirm_answer = False
        self._confirm_event.set()
        # Abort a blocked streaming read if the client supports it.
        cancel = getattr(self._client, "cancel", None)
        if callable(cancel):
            cancel()

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
                on_text=self.stream_text.emit,
                on_event=self._emit_event,
            )
        except AIError as exc:
            logger.warning("AI chat request failed: {}", exc)
            self.failed.emit(str(exc))
        except Exception as exc:
            logger.exception("AI chat worker crashed")
            self.failed.emit(f"Unexpected error: {exc}")
        else:
            self.finished_ok.emit(text)

    def _emit_event(self, event: AgentEvent) -> None:
        """Forward one structured event to the GUI (queued cross-thread)."""
        self.agent_event.emit(event)

    def _confirm(self, pretty: str) -> bool:
        if self._cancel.is_set():
            return False
        self._confirm_event.clear()
        self.confirm_requested.emit(pretty)
        if not self._confirm_event.wait(timeout=CONFIRM_TIMEOUT_S):
            return False
        return self._confirm_answer
