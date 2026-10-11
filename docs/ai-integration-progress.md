# AI Integration Progress

Per-stage progress for the AI chat / MCP / action-observation work.
Stages are implemented in order A→G. This file is updated as each stage
lands.

## Status

| Stage | Scope | Status |
|-------|-------|--------|
| A | Real streaming + correct cancellation | **Done** |
| B | Structured events + live action journal | Pending |
| C | GUI action bridge + stable ID registry | Pending |
| D | Working/saved mod-list state + preview/apply | Pending |
| E | Batch/dependency/conflict/log MCP tools | Pending |
| F | Token economy (prompt/session/context budget) | Pending |
| G | IPC bridge (`--attach-gui`) | Pending |

## Stage A — real streaming + correct cancellation

Confirmed defects fixed:

1. **HTTP streaming was never enabled.** The client set `payload["stream"]=True`
   but never passed `stream=True` to the POST, so no SSE stream was requested.
   Streaming now uses `aiohttp` on an event loop owned by the client; the
   response is opened/closed with context managers.
2. **Cancel could not interrupt a blocked read.** `AgentWorker.request_cancel`
   now (a) sets the cancel flag, (b) unblocks a worker parked in `_confirm`,
   and (c) calls `AIClient.cancel()`, which cancels the asyncio task owning
   the network read. A test asserts the worker returns within 2s with the
   partial answer. No `QThread.terminate()`, no deleting a running thread.
3. **Malformed tool-call arguments degraded to `{}`.** `app/ai/sse.py` only
   accepts a parsed JSON *object*; anything else is reported via
   `AssistantReply.incomplete_calls` so the agent surfaces an error instead of
   executing an empty call. Aborted/cancelled streams never execute a partial
   call.
4. **Intermediate and final text were mixed.** Each round is now one assistant
   message with a stable `message_id` and incrementing `round_id`; a
   `MESSAGE_COMPLETED` event carries the round text plus `is_final` so the GUI
   can render tool-calling turns separately from the final answer.
5. **Cancel was slow/incomplete during confirmation.** `_confirm` returns
   `False` immediately while cancelled; a cancel during confirmation is
   reclassified as cancelled, not "declined".

New modules:

- `app/ai/types.py` — shared `ToolCall` / `AssistantReply` (breaks the
  client↔sse import cycle; `client.py` re-exports them).
- `app/ai/sse.py` — push-based `SSEParser` (incremental UTF-8/CRLF/heartbeat/
  blank-line/`[DONE]` handling) + `StreamAccumulator` (content, usage, finish
  reason, index-ordered tool calls, incomplete-call reporting).
- `app/ai/events.py` — Qt-free `AgentEvent` / `EventEmitter` / `EventKind` /
  `EventStatus` contract (foundation for Stage B).

Design notes:

- No auto-restart of a turn after a network error: a stream failure raises
  `AIError` once; there is no retry loop.
- Cancel is not an error and does not roll back side effects: already-applied
  tool results stay applied; the turn returns `CANCELLED_TEXT` and shows the
  partial answer.
- Transport is injectable: `post` (sync JSON) and `stream_factory` (async
  generator) so tests stub the network without monkeypatching.

Verified: `ruff check`, `ruff format --check`, `mypy`, full `pytest`
(1635 passed / 1 skipped), `jscpd` (0 clones).

## Stage B — structured events + live action journal

Pending.

## Stage C — GUI action bridge + stable ID registry

Pending.

## Stage D — Working/saved mod-list state + preview/apply

Pending.

## Stage E — Batch / dependency / conflict / log MCP tools

Pending.

## Stage F — Token economy

Pending.

## Stage G — IPC bridge (`--attach-gui`)

Pending.
