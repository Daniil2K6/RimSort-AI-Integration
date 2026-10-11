"""Transport-agnostic SSE parsing and streaming tool-call assembly.

Free of ``requests``/``aiohttp`` so the wire behaviour is unit-testable with
plain bytes. Two small push-based pieces compose into either a sync or an
async consumer:

* :class:`SSEParser` — incremental: feed it raw byte chunks, it returns
  completed ``data:`` payloads. Tolerates UTF-8 split across chunks, CRLF,
  ``:`` heartbeat comments, blank-line event termination and multi-line
  ``data:`` fields joined by ``\\n``.
* :class:`StreamAccumulator` — folds payloads into content deltas, usage,
  finish reason and tool calls. Tool-call argument fragments concatenate by
  ``index``; a call is only surfaced once its arguments parse as a JSON
  *object*. Malformed/incomplete arguments are reported via
  :attr:`StreamAccumulator.incomplete_calls` instead of degrading to ``{}``.
"""

from __future__ import annotations

import codecs
import json
from collections.abc import Iterable, Mapping
from typing import Any

from app.ai.types import ToolCall

#: Text emitted by providers to terminate an SSE stream.
DONE_SENTINEL = "[DONE]"


class SSEParser:
    """Incremental SSE decoder: feed bytes, get completed data payloads."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._text = ""
        self._data_lines: list[str] = []

    def feed(self, chunk: bytes | str) -> list[str]:
        """Feed one network chunk; return payloads of any events it completed."""
        if isinstance(chunk, bytes):
            self._text += self._decoder.decode(chunk)
        else:
            self._text += chunk
        payloads: list[str] = []
        while True:
            newline = self._text.find("\n")
            if newline == -1:
                break
            line = self._text[:newline]
            self._text = self._text[newline + 1 :]
            payload = self._consume_line(line)
            if payload is not None:
                payloads.append(payload)
        return payloads

    def flush(self) -> list[str]:
        """Process any trailing text without a final newline."""
        payloads: list[str] = []
        if self._text:
            payload = self._consume_line(self._text)
            self._text = ""
            if payload is not None:
                payloads.append(payload)
        if self._data_lines:
            payloads.append("\n".join(self._data_lines))
            self._data_lines = []
        return payloads

    def _consume_line(self, raw: str) -> str | None:
        line = raw.rstrip("\r")
        if not line:
            # Blank line terminates the current event.
            if not self._data_lines:
                return None
            payload = "\n".join(self._data_lines)
            self._data_lines = []
            return payload
        if line.startswith(":"):
            return None  # heartbeat/comment
        if line.startswith("data:"):
            self._data_lines.append(line[5:].lstrip(" "))
        # Ignore other fields (event:, id:, retry:, unknown).
        return None


class StreamAccumulator:
    """Folds SSE data payloads into a completed streaming reply."""

    def __init__(
        self,
        on_content: Any = None,
        should_stop: Any = None,
    ) -> None:
        self._on_content = on_content
        self._should_stop = should_stop
        self.content = ""
        self.usage: dict[str, Any] = {}
        self.finish_reason = ""
        self.tool_calls: list[ToolCall] = []
        self.incomplete_calls: list[ToolCall] = []
        self.stopped = False
        self.done = False
        self.aborted = False
        self._builders: dict[int, _ToolCallBuilder] = {}

    def feed_payload(self, payload: str) -> None:
        """Fold one completed ``data:`` payload (may be ``[DONE]``)."""
        if self._should_stop is not None and self._should_stop():
            self.aborted = True
            self.stopped = True
            return
        text = payload.strip()
        if not text:
            return
        if text == DONE_SENTINEL:
            # Normal termination: stop reading but keep what we assembled.
            self.done = True
            self.stopped = True
            return
        try:
            chunk = json.loads(text)
        except (ValueError, TypeError):
            return  # malformed chunk: skip, keep the stream alive
        if not isinstance(chunk, Mapping):
            return
        _absorb_usage(self, chunk)
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, Mapping):
                continue
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta")
            if not isinstance(delta, Mapping):
                delta = choice.get("message")
                if not isinstance(delta, Mapping):
                    continue
            text_piece = delta.get("content")
            if text_piece:
                self.content += str(text_piece)
                if self._on_content is not None:
                    self._on_content(str(text_piece))
            self._absorb_tool_fragments(delta.get("tool_calls"))

    def _absorb_tool_fragments(self, raw_calls: Any) -> None:
        if not isinstance(raw_calls, list):
            return
        for fragment in raw_calls:
            if not isinstance(fragment, Mapping):
                continue
            index = fragment.get("index", 0)
            try:
                index = int(index)
            except (TypeError, ValueError):
                index = 0
            builder = self._builders.get(index)
            if builder is None:
                builder = _ToolCallBuilder()
                self._builders[index] = builder
            builder.apply_fragment(fragment)

    def finalize(self) -> None:
        """Resolve tool calls once the stream ends; honour an abort.

        ``[DONE]`` is a normal end and keeps assembled calls. Only a caller
        abort (``should_stop``) discards tool calls, since a cancelled turn
        must not execute a partial call.
        """
        if self.aborted:
            self.tool_calls = []
            self.incomplete_calls = []
            return
        # Emit in index order regardless of fragment arrival order.
        for index in sorted(self._builders):
            builder = self._builders[index]
            parsed = _load_arguments(builder.arguments)
            if parsed is not None:
                self.tool_calls.append(ToolCall(builder.id, builder.name, parsed))
            elif builder.name or builder.id:
                # Had an identity but arguments never parsed: malformed.
                self.incomplete_calls.append(ToolCall(builder.id, builder.name, {}))


class _ToolCallBuilder:
    """Accumulates streamed tool-call fragments keyed by ``index``."""

    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments = ""

    def apply_fragment(self, fragment: Mapping[str, Any]) -> None:
        if fragment.get("id"):
            self.id = str(fragment["id"])
        function = fragment.get("function")
        if isinstance(function, Mapping):
            if function.get("name"):
                self.name = str(function["name"])
            args = function.get("arguments")
            if args:
                self.arguments += str(args)


def _load_arguments(raw: Any) -> dict[str, Any] | None:
    """Parse tool-call arguments; ``None`` when unusable.

    Only a JSON *object* qualifies. Scalars, arrays, empty and malformed
    input return ``None`` so the caller flags the call instead of silently
    substituting ``{}``.
    """
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _absorb_usage(acc: StreamAccumulator, chunk: Mapping[str, Any]) -> None:
    usage = chunk.get("usage")
    if isinstance(usage, Mapping):
        acc.usage = dict(usage)


# -- sync convenience wrappers (used by tests / non-aiohttp paths) --------


def parse_lines(lines: Iterable[str]) -> list[str]:
    """Group raw SSE text lines into completed data payloads."""
    parser = SSEParser()
    payloads: list[str] = []
    for line in lines:
        payloads.extend(parser.feed(line + "\n"))
    payloads.extend(parser.flush())
    return payloads


def consume_bytes(
    chunks: Iterable[bytes],
    *,
    on_content: Any = None,
    should_stop: Any = None,
) -> StreamAccumulator:
    """Fold a sync byte stream into a finalized accumulator."""
    parser = SSEParser()
    acc = StreamAccumulator(on_content=on_content, should_stop=should_stop)
    for chunk in chunks:
        if acc.stopped:
            break
        for payload in parser.feed(chunk):
            acc.feed_payload(payload)
    for payload in parser.flush():
        acc.feed_payload(payload)
    acc.finalize()
    return acc
