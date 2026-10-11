# AI Integration Progress

Per-stage progress for the AI chat / MCP / action-observation work.
Stages are implemented in order A→G. This file is updated as each stage
lands.

## Status

| Stage | Scope | Status |
|-------|-------|--------|
| A | Real streaming + correct cancellation | **Done** |
| B | Structured events + live action journal | **Done** |
| C | GUI action bridge + stable ID registry | **Done** |
| D | Working/saved mod-list state + preview/apply | **Done** |
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

Done on top of the `AgentEvent`/`EventEmitter` contract built in Stage A:

- `AgentEvent` carries `event_id`, `run_id`, `round_id`, `seq`, `timestamp`,
  `kind`, `tool_call_id`/`action_id`, `status`, `summary`, `payload`.
  Kinds cover the full lifecycle including `approval_resolved`/`tool_progress`.
- `AgentWorker.agent_event` is a queued cross-thread `Signal(object)`; the GUI
  renders the journal from events, not from the old activity strings. The raw
  `activity` string is no longer connected for tool rows (avoids double render).
- Live journal: tool rows keyed by `tool_call_id` (identical calls never merge),
  status requested/running/succeeded/failed/declined updated in place, real
  duration measured from `TOOL_STARTED` to completion.
- Late-event safety: a new run resets `_active_run_id`; events from a stale run
  are dropped so a queued event never leaks into a new chat.
- Markdown streaming is buffered on a 50ms QTimer (see the responsiveness fix)
  with a mandatory final drain on finish/fail/cancel.
- Auto-scroll only when the viewport is already near the bottom, so reading
  history does not jump down during streaming/journal updates.
- Settings toggles `follow actions` / `highlight action target` (panel flags;
  highlight overlay wiring lands with Stage C's bridge).

Verified: ruff/format/mypy clean; full suite 1641 passed / 1 skipped; jscpd 0
clones. New journal tests cover in-place status updates, distinct IDs for
identical calls, stale-run filtering, duration, and follow-off behaviour.

## Stage C — GUI action bridge + stable ID registry

Done. The agent can now drive the live RimSort UI through a validated,
main-thread bridge; it never touches widgets directly.

- `app/ai/gui_bridge.py`: `GuiActionBridge` (lives in the Qt main thread) +
  `SurfaceId` stable-ID registry + `INVOKE_ALLOWLIST`. A worker thread submits
  a semantic command; the bridge queues it to the GUI thread and waits on a
  `threading.Event` with a timeout (never `BlockingQueuedConnection`). Called
  from the GUI thread it dispatches inline, so tests do not deadlock. Result is
  a real acknowledgement (`ok`, `action`, surface state) or a structured error
  (`gui_unavailable` / `gui_unknown_action` / `gui_invalid_url` /
  `gui_timeout` / `gui_error`).
- Validation: Workshop tool accepts only numeric `publishedfileid` (canonical
  `https://steamcommunity.com/...?id=N`); `gui_invoke_action` is allowlist-only
  (open_settings / refresh_mods / clear_chat / toggle_ai_chat); `set_filter`
  list name and `select_mod` package_id are checked. No DOM click, no JS from
  the model, no OS mouse, no shell.
- `app/mcp/gui.py`: six GUI tools (`get_gui_state`, `gui_navigate`,
  `gui_select_mod`, `gui_set_mod_filter`, `gui_open_workshop_item`,
  `gui_invoke_action`) registered on the MCP server. With no bridge (external
  stdio / headless) every one answers `gui_unavailable`; domain tools keep
  working.
- Real executors in `MainWindow` (`_build_gui_bridge`): tab navigation via the
  real `QTabWidget`, mod selection by `packageId` in the real list model
  (scroll + focus + a temporary themed frame highlight), filter via the existing
  search `QLineEdit`, Workshop via the existing SteamBrowser, and allowlisted
  actions via existing buttons/signals. Targets come from the stable `SurfaceId`
  table — never translated labels or coordinates.
- Settings `ai_chat_follow_actions` / `ai_chat_highlight` added; the panel reads
  them as defaults. (A settings-dialog UI for these toggles is still pending.)

Verified: ruff/format/mypy clean; full suite 1654 passed / 1 skipped; jscpd 0
clones. New tests cover bridge dispatch/validation/timeout/cross-thread, the
SurfaceId table, and headless `gui_unavailable` for every GUI tool.

## Stage D — Working/saved mod-list state + preview/apply

Done. The agent now edits an in-memory *working* list separate from the saved
ModsConfig.xml, with revisions and a concurrent-edit interlock.

- `MCPContext` gains a working-state layer: `working_ids` / `set_working_ids` /
  `modlist_state` / `commit_working`, a `_revision` counter, and
  `_detect_external_edit` which snapshots the saved list and bumps the revision
  whenever ModsConfig.xml changes outside our writes (a concurrent GUI edit).
  Working state starts synced to disk and resets to the new saved list on an
  external edit, so a stale apply is rejected — never silently overwritten.
- New tools:
  - `get_modlist_state` — working vs saved lists + revision + dirty flag.
  - `preview_modlist_changes` — full replacement or add/remove/move from the
    working list, returning the complete resulting order plus validation
    diagnostics. Writes nothing and does not bump the revision.
  - `apply_modlist_plan` — validates a complete target list, refuses empty or
    unknown ids (unless `allow_missing`), then commits atomically (single
    backup + write via `commit_working`) and marks working == saved. Passing a
    stale `expected_revision` rejects with the current state (interlock).
- Validation reuses the existing resolver/dependency diagnostics: the core of
  `validate_active_list` was extracted into `_validate_config` and a new
  `validate_candidate_ids` runs the same checks on an arbitrary candidate list
  without touching disk. No raw ModsConfig writes; backups go through the
  existing prune-limited backup path.

Verified: ruff/format/mypy clean; full suite 1660 passed / 1 skipped; jscpd 0
clones. New tests cover initial sync, preview-without-write, full
replacement/move, apply + revision bump, revision interlock, empty/unknown
rejection, and external-edit detection.

## Stage E — Batch / dependency / conflict / log MCP tools

Pending.

## Stage F — Token economy

Pending.

## Stage G — IPC bridge (`--attach-gui`)

Pending.
