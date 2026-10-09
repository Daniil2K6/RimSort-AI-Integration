"""Tests for the OpenAI-compatible chat client."""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from app.ai.client import AIClient, AIError, ToolCall

GOOD_BODY = {
    "choices": [
        {"message": {"content": "hello", "tool_calls": []}},
    ]
}


class FakeResponse:
    """Stand-in exposing the bits of requests.Response the client reads."""

    def __init__(
        self,
        status_code: int = 200,
        data: Any = None,
        text: str = "",
        bad_json: bool = False,
        lines: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.data = data
        self.text = text
        self.bad_json = bad_json
        self.lines = lines or []

    def json(self) -> Any:
        if self.bad_json:
            raise ValueError("not json")
        return self.data

    def iter_lines(self) -> list[bytes]:
        return self.lines


def make_recorder(
    response: FakeResponse | None = None, error: Exception | None = None
) -> tuple[Any, list[dict[str, Any]]]:
    """Return a `post` callable that records calls and replays a response."""
    calls: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        if error is not None:
            raise error
        return response if response is not None else FakeResponse(data=GOOD_BODY)

    return post, calls


def make_client(post: Any) -> AIClient:
    return AIClient(
        base_url="https://api.example.test/v1/",
        api_key="sk-unit-test",
        model="test-model",
        post=post,
    )


def test_chat_builds_url_auth_and_payload() -> None:
    post, calls = make_recorder(FakeResponse(data=GOOD_BODY))
    client = make_client(post)

    reply = client.chat([{"role": "user", "content": "hi"}])

    assert reply.content == "hello"
    assert reply.tool_calls == []
    request = calls[0]
    assert request["url"] == "https://api.example.test/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer sk-unit-test"
    assert request["json"]["model"] == "test-model"
    assert request["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert "tools" not in request["json"]
    assert isinstance(request["timeout"], tuple)


def test_chat_omits_auth_header_without_key() -> None:
    post, calls = make_recorder(FakeResponse(data=GOOD_BODY))
    client = AIClient(
        base_url="http://localhost:11434/v1",
        api_key="",
        model="llama",
        post=post,
    )

    client.chat([{"role": "user", "content": "hi"}])

    assert "Authorization" not in calls[0]["headers"]
    assert calls[0]["url"] == "http://localhost:11434/v1/chat/completions"


def test_chat_parses_tool_calls_with_json_arguments() -> None:
    body = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "get_status",
                                "arguments": '{"refresh": true}',
                            },
                        },
                        {
                            "id": "call_2",
                            "function": {
                                "name": "list_mods",
                                "arguments": "not json at all",
                            },
                        },
                        {
                            "id": "call_3",
                            "function": {
                                "name": "validate_modlist",
                                "arguments": {"severity": "warning"},
                            },
                        },
                    ],
                }
            }
        ]
    }
    post, _ = make_recorder(FakeResponse(data=body))
    client = make_client(post)

    reply = client.chat([{"role": "user", "content": "go"}])

    assert reply.content == ""
    assert reply.tool_calls == [
        ToolCall("call_1", "get_status", {"refresh": True}),
        ToolCall("call_2", "list_mods", {}),
        ToolCall("call_3", "validate_modlist", {"severity": "warning"}),
    ]


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, "credentials"),
        (404, "base URL"),
        (429, "rate limit"),
        (500, "HTTP 500"),
    ],
)
def test_chat_maps_http_errors_to_ai_error(status_code: int, expected: str) -> None:
    post, _ = make_recorder(
        FakeResponse(status_code=status_code, text="upstream says no")
    )
    client = make_client(post)

    with pytest.raises(AIError, match=expected) as excinfo:
        client.chat([{"role": "user", "content": "hi"}])

    assert "upstream says no" in str(excinfo.value)


def test_chat_reports_network_failure() -> None:
    post, _ = make_recorder(error=requests.ConnectionError("refused"))
    client = make_client(post)

    with pytest.raises(AIError, match="Could not reach"):
        client.chat([{"role": "user", "content": "hi"}])


def test_chat_rejects_non_json_and_missing_choices() -> None:
    post, _ = make_recorder(FakeResponse(bad_json=True, text="<html>"))
    client = make_client(post)
    with pytest.raises(AIError, match="non-JSON"):
        client.chat([{"role": "user", "content": "hi"}])

    post, _ = make_recorder(FakeResponse(data={"oops": 1}))
    client = make_client(post)
    with pytest.raises(AIError, match="no choices"):
        client.chat([{"role": "user", "content": "hi"}])

    post, _ = make_recorder(FakeResponse(data=[1, 2, 3]))
    client = make_client(post)
    with pytest.raises(AIError, match="unexpected JSON"):
        client.chat([{"role": "user", "content": "hi"}])


def test_tool_call_arguments_accept_dict_or_string() -> None:
    """Guard the parsing helpers against degenerate argument shapes."""
    from app.ai.client import _parse_arguments

    assert _parse_arguments({"a": 1}) == {"a": 1}
    assert _parse_arguments('{"a": 1}') == {"a": 1}
    assert _parse_arguments("") == {}
    assert _parse_arguments("[1]") == {}
    assert _parse_arguments("[oops") == {}
    assert _parse_arguments(None) == {}
    assert json.dumps(_parse_arguments('{"a": "б"}')) == '{"a": "\\u0431"}'


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}".encode()


def _run_stream(
    lines: list[bytes], should_stop: Any = None
) -> tuple[list[str], Any, list[dict[str, Any]]]:
    """Stream fake SSE lines through a real client; return seen/reply/calls."""
    post, calls = make_recorder(FakeResponse(lines=lines))
    client = make_client(post)
    seen: list[str] = []
    reply = client.chat(
        [{"role": "user", "content": "hi"}],
        on_text=seen.append,
        should_stop=should_stop or (lambda: False),
    )
    return seen, reply, calls


def test_streaming_forwards_deltas_and_sets_stream_flag() -> None:
    """With on_text the request streams and deltas reach the callback."""
    lines = [
        _sse({"choices": [{"delta": {"content": "he"}}]}),
        _sse({"choices": [{"delta": {"content": "llo"}}]}),
        b"data: [DONE]",
    ]

    seen, reply, calls = _run_stream(lines)

    assert calls[0]["json"]["stream"] is True
    assert seen == ["he", "llo"]
    assert reply.content == "hello"
    assert reply.tool_calls == []


def test_streaming_assembles_fragmented_tool_calls() -> None:
    """Tool-call argument fragments are concatenated across SSE chunks."""
    lines = [
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {
                                        "name": "list_mods",
                                        "arguments": '{"query":',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"ce"}'},
                                }
                            ]
                        }
                    }
                ]
            }
        ),
        b"data: [DONE]",
    ]

    _, reply, _ = _run_stream(lines)

    assert reply.content == ""
    assert len(reply.tool_calls) == 1
    call = reply.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "list_mods"
    assert call.arguments == {"query": "ce"}


def test_streaming_should_stop_breaks_early() -> None:
    """should_stop aborts the stream between chunks."""
    lines = [
        _sse({"choices": [{"delta": {"content": "first"}}]}),
        _sse({"choices": [{"delta": {"content": "second"}}]}),
        b"data: [DONE]",
    ]
    budget = [1]

    def _stop_after_first() -> bool:
        budget[0] -= 1
        return budget[0] < 0

    seen, reply, _ = _run_stream(lines, should_stop=_stop_after_first)

    assert seen == ["first"]
    assert reply.content == "first"


def test_streaming_skips_malformed_chunks() -> None:
    """Malformed SSE lines never break the stream."""
    lines = [
        b"data: {not json",
        b"event: ping",
        _sse({"choices": []}),
        _sse({"choices": [{"delta": {"content": "ok"}}]}),
        b"data: [DONE]",
    ]

    seen, reply, _ = _run_stream(lines)

    assert seen == ["ok"]
    assert reply.content == "ok"
