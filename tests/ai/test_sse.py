"""Unit tests for the transport-agnostic SSE parser and tool-call assembly.

These cover the wire-level edge cases Stage A required: split UTF-8, CRLF,
heartbeat comments, blank-line event termination, multi-line ``data:``
fields, ``[DONE]``, usage-only chunks, empty ``choices``, fragmented tool
calls, and malformed arguments that must never degrade to ``{}``.
"""

from __future__ import annotations

import json

from app.ai.sse import (
    DONE_SENTINEL,
    SSEParser,
    StreamAccumulator,
    _load_arguments,
    consume_bytes,
    parse_lines,
)


def _event(payload: object) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def test_parser_groups_blank_line_terminated_events() -> None:
    parser = SSEParser()
    assert parser.feed(b"data: A\n\n") == ["A"]
    assert parser.feed(b"data: B\n\n") == ["B"]
    assert parser.flush() == []


def test_parser_joins_multiline_data_with_newline() -> None:
    parser = SSEParser()
    # Two data: lines inside one event are joined by \n.
    assert parser.feed(b"data: part1\ndata: part2\n\n") == ["part1\npart2"]


def test_parser_ignores_comments_and_non_data_fields() -> None:
    parser = SSEParser()
    assert parser.feed(b": heartbeat\n\n") == []
    assert parser.feed(b"event: message\n\n") == []
    assert parser.feed(b"id: 42\nretry: 100\n\n") == []
    assert parser.feed(b"data: real\n\n") == ["real"]


def test_parser_handles_crlf_line_endings() -> None:
    parser = SSEParser()
    assert parser.feed(b"data: A\r\n\r\n") == ["A"]


def test_parser_decodes_utf8_split_across_chunks() -> None:
    raw = "data: б\n\n".encode()
    parser = SSEParser()
    # Feed one byte at a time; the multi-byte char is split mid-sequence.
    collected: list[str] = []
    for i in range(len(raw)):
        collected.extend(parser.feed(raw[i : i + 1]))
    assert collected == ["б"]


def test_parser_flush_emits_trailing_event_without_final_newline() -> None:
    parser = SSEParser()
    assert parser.feed(b"data: tail") == []
    assert parser.flush() == ["tail"]


def test_parser_replaces_invalid_utf8_instead_of_raising() -> None:
    parser = SSEParser()
    # 0xff/0xfe are never valid UTF-8; the decoder replaces them, not raise.
    payloads = parser.feed(b"data: \xff\xfe ok\n\n")
    assert len(payloads) == 1
    assert "ok" in payloads[0]
    assert "�" in payloads[0]


def test_accumulator_reassembles_content_and_tool_calls() -> None:
    acc = StreamAccumulator()
    acc.feed_payload(
        json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "content": "hi ",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {
                                        "name": "list_mods",
                                        "arguments": '{"q":',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )
    )
    acc.feed_payload(
        json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '"x"}'}}
                            ]
                        }
                    }
                ]
            }
        )
    )
    acc.feed_payload(DONE_SENTINEL)
    acc.finalize()

    assert acc.content == "hi "
    assert len(acc.tool_calls) == 1
    assert acc.tool_calls[0].arguments == {"q": "x"}
    assert acc.incomplete_calls == []


def test_accumulator_keeps_calls_after_done_but_clears_on_abort() -> None:
    # [DONE] keeps assembled calls.
    acc = StreamAccumulator()
    acc.feed_payload(
        json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c",
                                    "function": {"name": "f", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            }
        )
    )
    acc.feed_payload(DONE_SENTINEL)
    acc.finalize()
    assert len(acc.tool_calls) == 1

    # An abort discards tool calls (never execute a partial call).
    stop_flag = [False]
    acc2 = StreamAccumulator(should_stop=lambda: stop_flag[0])
    acc2.feed_payload(
        json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c",
                                    "function": {"name": "f", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            }
        )
    )
    stop_flag[0] = True  # caller cancels before the stream finishes
    acc2.feed_payload(json.dumps({"choices": [{"delta": {"content": "late"}}]}))
    acc2.finalize()
    assert acc2.tool_calls == []
    assert acc2.content == ""  # "late" was never folded in


def test_accumulator_reports_incomplete_tool_args_not_empty_object() -> None:
    acc = StreamAccumulator()
    acc.feed_payload(
        json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "bad",
                                    "function": {
                                        "name": "delete_mod",
                                        "arguments": "{oops",
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        )
    )
    acc.feed_payload(DONE_SENTINEL)
    acc.finalize()

    assert acc.tool_calls == []
    assert len(acc.incomplete_calls) == 1
    assert acc.incomplete_calls[0].name == "delete_mod"


def test_accumulator_multiple_tool_calls_keep_index_order() -> None:
    acc = StreamAccumulator()
    # Second call (index 1) arrives before the first finishes.
    acc.feed_payload(
        json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "b",
                                    "function": {"name": "b", "arguments": "{}"},
                                },
                                {
                                    "index": 0,
                                    "id": "a",
                                    "function": {"name": "a", "arguments": "{}"},
                                },
                            ]
                        }
                    }
                ]
            }
        )
    )
    acc.finalize()
    assert [c.name for c in acc.tool_calls] == ["a", "b"]


def test_accumulator_captures_usage_and_finish_reason() -> None:
    acc = StreamAccumulator()
    acc.feed_payload(
        json.dumps({"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]})
    )
    acc.feed_payload(
        json.dumps(
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
        )
    )
    assert acc.finish_reason == "stop"
    assert acc.usage == {"prompt_tokens": 3, "completion_tokens": 2}


def test_accumulator_on_content_receives_deltas() -> None:
    seen: list[str] = []
    acc = StreamAccumulator(on_content=seen.append)
    acc.feed_payload(json.dumps({"choices": [{"delta": {"content": "ab"}}]}))
    acc.feed_payload(json.dumps({"choices": [{"delta": {"content": "cd"}}]}))
    assert seen == ["ab", "cd"]


def test_accumulator_accepts_full_message_not_just_delta() -> None:
    acc = StreamAccumulator()
    acc.feed_payload(json.dumps({"choices": [{"message": {"content": "full"}}]}))
    assert acc.content == "full"


def test_consume_bytes_end_to_end_with_split_chunks() -> None:
    raw = (
        _event({"choices": [{"delta": {"content": "he"}}]})
        + _event({"choices": [{"delta": {"content": "llo"}}]})
        + b"data: [DONE]\n\n"
    )
    # Split into 7-byte pieces to stress the incremental decoder.
    chunks = [raw[i : i + 7] for i in range(0, len(raw), 7)]
    acc = consume_bytes(chunks)
    assert acc.content == "hello"


def test_load_arguments_rejects_non_object_shapes() -> None:
    assert _load_arguments('{"a": 1}') == {"a": 1}
    assert _load_arguments({"a": 1}) == {"a": 1}
    assert _load_arguments("[1]") is None
    assert _load_arguments("not json") is None
    assert _load_arguments("") is None
    assert _load_arguments(None) is None
    assert _load_arguments(42) is None


def test_parse_lines_wrapper_groups_payloads() -> None:
    payloads = parse_lines(["data: one", "", "data: two", "", ""])
    assert payloads == ["one", "two"]
