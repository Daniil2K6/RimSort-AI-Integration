"""OpenAI-compatible chat completions client.

Two paths share one payload builder:

* **Non-streaming** (no ``on_text``/``on_event``) uses ``requests`` — a
  single blocking JSON POST. The POST function is injectable so tests can
  stub the network without monkeypatching ``requests``.
* **Streaming** uses ``aiohttp`` on an event loop owned by this client so
  a cancel from another thread can abort a *blocked* network read within a
  bounded time (Stage A). The stream transport is injectable as an async
  generator function for tests.

Streaming parses SSE through :mod:`app.ai.sse`, which tolerates split
UTF-8, CRLF, heartbeat comments, ``[DONE]``, usage-only chunks and
fragmented tool calls, and never silently degrades malformed tool-call
arguments to ``{}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, NamedTuple, Protocol

import aiohttp
import requests

from app.ai.errors import AIError, http_error_report, network_error_report
from app.ai.sse import SSEParser, StreamAccumulator, _load_arguments
from app.ai.types import AssistantReply, ToolCall

__all__ = ["AIClient", "AIError", "AssistantReply", "ChatCancelled", "ToolCall"]

DEFAULT_TIMEOUT: tuple[float, float] = (15.0, 180.0)

PostFn = Callable[..., Any]

#: Async generator function: (url, headers, payload, timeout) -> byte chunks.
StreamFn = Callable[
    [str, dict[str, str], dict[str, Any], tuple[float, float]],
    AsyncIterator[bytes],
]
#: Factory returning a StreamFn bound to a should_stop predicate.
StreamFactory = Callable[[Callable[[], bool]], StreamFn]


class _StreamCallbacks(NamedTuple):
    """Bundle of per-stream callbacks (keeps ``_stream_coro`` signature small)."""

    on_text: Callable[[str], None] | None
    should_stop: Callable[[], bool] | None
    on_event: Callable[[str, dict[str, Any]], None] | None


class ChatCancelled(AIError):
    """Raised when the caller cancels a streaming request mid-flight."""


class ChatClient(Protocol):
    """Structural interface for sending one chat completion."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        on_text: Callable[[str], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> AssistantReply: ...


class AIClient:
    """Chat client with blocking JSON and cancellable streaming paths."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        post: PostFn | None = None,
        stream_factory: StreamFactory | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._post: PostFn = post or requests.post
        self._stream_factory: StreamFactory = stream_factory or aiohttp_stream_factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[AssistantReply] | None = None
        self._partial: AssistantReply | None = None

    # -- cancellation -----------------------------------------------------
    def cancel(self) -> None:
        """Abort an in-flight streaming call from another thread.

        Cancels the asyncio task that owns the network read so a blocked
        ``aiohttp`` read wakes promptly (well under the 2s budget). Safe to
        call when idle.
        """
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not task.done():
            loop.call_soon_threadsafe(task.cancel)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> AssistantReply:
        """Send one chat completion and return the parsed message.

        With ``on_text``/``on_event`` the request streams; otherwise it is a
        single JSON POST.
        """
        payload: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            payload["tools"] = tools
        streaming = on_text is not None or on_event is not None
        if streaming:
            payload["stream"] = True
            payload.setdefault("stream_options", {"include_usage": True})
        headers = self._build_headers()

        if not streaming:
            return self._chat_json(payload, headers)
        return self._run_stream_loop(payload, headers, on_text, should_stop, on_event)

    def _run_stream_loop(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_text: Callable[[str], None] | None,
        should_stop: Callable[[], bool] | None,
        on_event: Callable[[str, dict[str, Any]], None] | None,
    ) -> AssistantReply:
        """Drive the streaming coroutine on a private, cancellable loop."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            task = loop.create_task(
                self._stream_coro(
                    payload, headers, _StreamCallbacks(on_text, should_stop, on_event)
                )
            )
            self._task = task
            try:
                return loop.run_until_complete(task)
            except asyncio.CancelledError:
                return self._pop_partial()
        finally:
            self._task = None
            self._loop = None
            with contextlib.suppress(Exception):
                # Best-effort cleanup so the loop can close cleanly.
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    # -- helpers ----------------------------------------------------------
    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _chat_json(
        self, payload: dict[str, Any], headers: dict[str, str]
    ) -> AssistantReply:
        try:
            response = self._post(
                self._url, json=payload, headers=headers, timeout=self._timeout
            )
        except (requests.RequestException, OSError) as exc:
            raise AIError(
                network_error_report(
                    exc, endpoint=self._url, model=self._model, secrets=(self._api_key,)
                )
            ) from exc
        status = getattr(response, "status_code", 0)
        if status != 200:
            raise AIError(
                http_error_report(
                    status,
                    str(getattr(response, "text", "") or ""),
                    endpoint=self._url,
                    model=self._model,
                    secrets=(self._api_key,),
                    headers=getattr(response, "headers", None),
                )
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise AIError(
                "Сервер ИИ вернул ответ не в формате JSON. Проверьте базовый URL API: он должен вести к API, а не к странице сайта."
            ) from exc
        if not isinstance(data, dict):
            raise AIError(
                "Сервер ИИ вернул неожиданный формат JSON. Проверьте совместимость сервера с Chat Completions API."
            )
        if data.get("error"):
            raise AIError(
                http_error_report(
                    200,
                    json.dumps(data),
                    endpoint=self._url,
                    model=self._model,
                    secrets=(self._api_key,),
                )
            )
        return _parse_reply(data)

    async def _stream_coro(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        cbs: _StreamCallbacks,
    ) -> AssistantReply:
        stop = cbs.should_stop or (lambda: False)
        parser = SSEParser()

        def _on_content(text: str) -> None:
            if cbs.on_text is not None:
                cbs.on_text(text)
            if cbs.on_event is not None:
                cbs.on_event("text_delta", {"content": text})

        acc = StreamAccumulator(on_content=_on_content, should_stop=stop)
        stream = self._stream_factory(stop)
        try:
            async for chunk in stream(self._url, headers, payload, self._timeout):
                for payload_str in parser.feed(chunk):
                    self._check_stream_error(payload_str)
                    acc.feed_payload(payload_str)
                if acc.stopped:
                    break
        except asyncio.CancelledError:
            # Persist partial content so cancel can show a partial answer.
            acc.finalize()
            self._stash_partial(acc)
            raise
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise AIError(
                network_error_report(
                    exc, endpoint=self._url, model=self._model, secrets=(self._api_key,)
                )
            ) from exc
        for payload_str in parser.flush():
            self._check_stream_error(payload_str)
            acc.feed_payload(payload_str)
        if not acc.done and not acc.finish_reason and not stop():
            raise AIError(
                "Сервер ИИ прервал поток до завершения ответа. "
                "Частичный текст сохранён на экране; инструменты из неполного ответа "
                "не выполнены. Проверьте соединение и состояние сервиса."
            )
        acc.finalize()
        if cbs.on_event is not None:
            cbs.on_event(
                "message_completed",
                {
                    "finish_reason": acc.finish_reason,
                    "usage": acc.usage,
                    "incomplete_calls": len(acc.incomplete_calls),
                },
            )
        return AssistantReply(
            content=acc.content,
            tool_calls=acc.tool_calls,
            usage=acc.usage,
            incomplete_calls=acc.incomplete_calls,
        )

    def _check_stream_error(self, payload: str) -> None:
        """Providers can send error events even after HTTP 200 headers."""
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return
        if isinstance(data, dict) and data.get("error"):
            raise AIError(
                http_error_report(
                    200,
                    payload,
                    endpoint=self._url,
                    model=self._model,
                    secrets=(self._api_key,),
                )
            )

    def _stash_partial(self, acc: StreamAccumulator) -> None:
        """Remember partial content so a cancel can show a partial answer."""
        self._partial = AssistantReply(content=acc.content, usage=acc.usage)

    def _pop_partial(self) -> AssistantReply:
        """Return and clear any stashed partial reply (empty when none)."""
        partial = self._partial
        self._partial = None
        return partial if partial is not None else AssistantReply(content="")


# -- default aiohttp streaming transport ----------------------------------


def aiohttp_stream_factory(should_stop: Callable[[], bool]) -> StreamFn:
    """Build an aiohttp-backed stream function honouring ``should_stop``."""

    async def _stream(
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: tuple[float, float],
    ) -> AsyncIterator[bytes]:
        connect_t, read_t = timeout
        client_timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=connect_t, sock_read=read_t
        )
        async with (
            aiohttp.ClientSession(timeout=client_timeout) as session,
            session.post(url, json=payload, headers=headers) as resp,
        ):
            if resp.status != 200:
                chunks: list[bytes] = []
                remaining = 16_384
                async for piece in resp.content.iter_chunked(4096):
                    chunks.append(piece[:remaining])
                    remaining -= len(piece)
                    if remaining <= 0:
                        break
                body = b"".join(chunks).decode("utf-8", "replace")
                token = headers.get("Authorization", "").removeprefix("Bearer ")
                raise AIError(
                    http_error_report(
                        resp.status,
                        body,
                        endpoint=url,
                        model=str(payload.get("model", "")),
                        secrets=(token,),
                        headers=resp.headers,
                    )
                )
            async for chunk in resp.content.iter_any():
                if should_stop():
                    break
                if chunk:
                    yield chunk

    return _stream


# -- non-streaming parsing -------------------------------------------------


def _format_http_error(status: int, response: Any) -> str:
    try:
        body = str(getattr(response, "text", "") or "")
    except Exception:
        body = ""
    return http_error_report(
        status, body, headers=getattr(response, "headers", None)
    ).render()


def _parse_reply(data: dict[str, Any]) -> AssistantReply:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AIError(
            "Ответ ИИ не содержит choices с сообщением. Проверьте совместимость выбранной модели и API с Chat Completions."
        )
    first = choices[0]
    if not isinstance(first, dict):
        raise AIError(
            "Сервер ИИ вернул некорректный элемент choices. Проверьте совместимость API провайдера."
        )
    message = first.get("message")
    if not isinstance(message, dict):
        raise AIError(
            "Сервер ИИ вернул некорректное сообщение. Проверьте совместимость API провайдера."
        )

    raw_content = message.get("content")
    content = raw_content if isinstance(raw_content, str) else ""
    if raw_content is not None and not isinstance(raw_content, str):
        content = str(raw_content)

    tool_calls: list[ToolCall] = []
    incomplete: list[ToolCall] = []
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            function = raw.get("function")
            if not isinstance(function, dict):
                function = {}
            call_id = str(raw.get("id") or f"call_{len(tool_calls)}")
            name = str(function.get("name") or "")
            parsed = _load_arguments(function.get("arguments"))
            if parsed is None:
                incomplete.append(ToolCall(call_id, name, {}))
            else:
                tool_calls.append(ToolCall(call_id, name, parsed))
    raw_usage = data.get("usage")
    usage: dict[str, Any] = dict(raw_usage) if isinstance(raw_usage, dict) else {}
    return AssistantReply(
        content=content, tool_calls=tool_calls, usage=usage, incomplete_calls=incomplete
    )


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Back-compat helper: return args or ``{}`` for degenerate shapes."""
    parsed = _load_arguments(raw)
    if parsed is not None:
        return parsed
    return dict(raw) if isinstance(raw, dict) else {}
