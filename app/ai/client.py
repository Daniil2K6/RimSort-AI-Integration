"""Minimal OpenAI-compatible chat completions client.

Uses `requests` (already a RimSort dependency) against
`{base_url}/chat/completions`. The POST function is injectable so tests
can stub the network without monkeypatching `requests`. When an
`on_text` callback is supplied the request is made with `stream: true`
and content deltas are forwarded to the callback as they arrive (SSE).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

DEFAULT_TIMEOUT: tuple[float, float] = (15.0, 180.0)

PostFn = Callable[..., Any]


class AIError(Exception):
    """A user-facing AI API failure (network, HTTP status, malformed body)."""


@dataclass
class ToolCall:
    """One function call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistantReply:
    """Parsed `choices[0].message` from a chat completion."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class ChatClient(Protocol):
    """Structural interface for sending one chat completion.

    Parameters carry no defaults so the signature does not mirror
    :meth:`AIClient.chat` line-for-line; every call site passes
    ``tools``/``on_text``/``should_stop`` explicitly.
    """

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        on_text: Callable[[str], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> AssistantReply: ...


class AIClient:
    """Synchronous OpenAI-compatible chat client."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        post: PostFn | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._post: PostFn = post or requests.post

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> AssistantReply:
        """Send one chat completion request and return the parsed message.

        With `on_text` the request streams: each content delta is passed to
        the callback immediately and the full reply is still returned.
        """
        payload: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            payload["tools"] = tools
        if on_text is not None:
            payload["stream"] = True
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = self._post(
                self._url, json=payload, headers=headers, timeout=self._timeout
            )
        except (requests.RequestException, OSError) as exc:
            raise AIError(f"Could not reach the AI endpoint: {exc}") from exc

        status = getattr(response, "status_code", 0)
        if status != 200:
            raise AIError(_format_http_error(status, response))
        if on_text is not None:
            return _parse_stream(response, on_text, should_stop)
        try:
            data = response.json()
        except ValueError as exc:
            raise AIError("The AI endpoint returned a non-JSON response") from exc
        if not isinstance(data, dict):
            raise AIError("The AI endpoint returned unexpected JSON")
        return _parse_reply(data)


def _format_http_error(status: int, response: Any) -> str:
    try:
        body = str(response.text or "")
    except (AttributeError, requests.RequestException):
        body = ""
    detail = f" Response: {body[:300]}" if body else ""
    if status in (401, 403):
        return f"AI endpoint rejected the credentials (HTTP {status}).{detail}"
    if status == 404:
        return f"AI endpoint not found (HTTP {status}); check the base URL.{detail}"
    if status == 429:
        return f"AI endpoint rate limit hit (HTTP {status}).{detail}"
    return f"AI endpoint error (HTTP {status}).{detail}"


def _parse_stream(
    response: Any,
    on_text: Callable[[str], None],
    should_stop: Callable[[], bool] | None,
) -> AssistantReply:
    """Consume an SSE stream, forwarding content deltas to `on_text`."""
    parts: list[str] = []
    tool_acc: dict[int, dict[str, str]] = {}
    try:
        for raw in response.iter_lines():
            if should_stop is not None and should_stop():
                break
            if not raw:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            first = choices[0]
            if not isinstance(first, dict):
                continue
            delta = first.get("delta")
            if not isinstance(delta, dict):
                continue
            text = delta.get("content")
            if isinstance(text, str) and text:
                parts.append(text)
                on_text(text)
            _accumulate_tool_calls(tool_acc, delta.get("tool_calls"))
    except (requests.RequestException, OSError) as exc:
        raise AIError(f"AI stream interrupted: {exc}") from exc
    return AssistantReply(
        content="".join(parts), tool_calls=_finish_tool_calls(tool_acc)
    )


def _accumulate_tool_calls(acc: dict[int, dict[str, str]], raw_calls: Any) -> None:
    """Merge one SSE delta's partial tool-call fragments into `acc`."""
    if not isinstance(raw_calls, list):
        return
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        index = raw.get("index")
        slot = acc.setdefault(
            int(index) if isinstance(index, int) else 0,
            {"id": "", "name": "", "arguments": ""},
        )
        call_id = raw.get("id")
        if isinstance(call_id, str) and call_id:
            slot["id"] = call_id
        function = raw.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name:
                slot["name"] = name
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                slot["arguments"] += arguments


def _finish_tool_calls(acc: dict[int, dict[str, str]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index in sorted(acc):
        slot = acc[index]
        if not slot["name"]:
            continue
        calls.append(
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=_parse_arguments(slot["arguments"]),
            )
        )
    return calls


def _parse_reply(data: dict[str, Any]) -> AssistantReply:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AIError(f"AI response has no choices: {str(data)[:300]}")
    first = choices[0]
    if not isinstance(first, dict):
        raise AIError("AI response choice is malformed")
    message = first.get("message")
    if not isinstance(message, dict):
        raise AIError("AI response message is malformed")

    raw_content = message.get("content")
    content = raw_content if isinstance(raw_content, str) else ""
    if raw_content is not None and not isinstance(raw_content, str):
        content = str(raw_content)

    tool_calls: list[ToolCall] = []
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            function = raw.get("function")
            if not isinstance(function, dict):
                function = {}
            tool_calls.append(
                ToolCall(
                    id=str(raw.get("id") or f"call_{len(tool_calls)}"),
                    name=str(function.get("name") or ""),
                    arguments=_parse_arguments(function.get("arguments")),
                )
            )
    return AssistantReply(content=content, tool_calls=tool_calls)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
