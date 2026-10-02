---
title: MCP Integration
nav_order: 9
layout: default
parent: User Guide
permalink: user-guide/mcp
---
# MCP Integration

{: .no_toc}

RimSort ships a local **MCP (Model Context Protocol) server** over stdio. It lets an AI agent running in your IDE or terminal (opencode, Claude Code, Cursor, Windsurf, and any other MCP-capable client) inspect your RimWorld setup, build and validate mod lists, manage modpacks, and launch the game — using the same sorting logic as the RimSort GUI.

## Table of Contents

{: .no_toc .text-delta }

1. TOC
{:toc}

## Overview

The server is built on the official Python [MCP SDK](https://pypi.org/project/mcp/) and runs entirely on your machine. It exposes:

- **14 tools** — for reading status, managing the active mod list, sorting, modpacks, and launching the game
- **2 resources** — `rimsort://status` and `rimsort://modlist/active` for lazy reads
- **2 prompts** — guided workflows for assembling and troubleshooting mod lists

The server:

- **Speaks only stdio** — the client spawns it as a child process; no ports, no network.
- **Writes logs to stderr only** — stdout is reserved for the MCP protocol.
- **Never opens dialogs** — errors are returned as structured tool errors, so headless/agent use never blocks on Qt.
- **Uses your existing settings** — the same `settings.json` and instance configuration as the GUI.

## Running the Server

From source (recommended):

```bash
uv run python -m app mcp
```

or with a plain interpreter from an installed environment:

```bash
python -m app mcp
```

Options:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--log-level TEXT` | String | `INFO` | Log verbosity written to stderr (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

The process runs until the client disconnects. It is not intended to be run by hand except for debugging:

```bash
# Verify the server starts and responds
uv run python -m app mcp --log-level DEBUG
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `RIMSORT_MCP_SETTINGS` | Absolute path to an alternative `settings.json`. Useful for tests, isolated instances, or CI. Defaults to RimSort's normal app settings file. |

## Connecting an AI Client

The client (not the server) is configured. Below is a generic `opencode.json` example; the command array must point at your RimSort checkout.

```json
{
  "mcp": {
    "rimsort": {
      "type": "local",
      "command": [
        "uv", "run", "--directory",
        "/absolute/path/to/RimSort",
        "python", "-m", "app", "mcp"
      ],
      "enabled": true
    }
  }
}
```

For Claude Code (`claude mcp add`) or other clients, use the same command: `uv run --directory /absolute/path/to/RimSort python -m app mcp`.

After connecting, ask your agent things like:

> "Check my active mod list for conflicts and sort it."
> "Build a vanilla-plus mod list focused on performance."

## Tools

### Status and instances

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_status` | `refresh` | Instance name, game version, mod counts, paths, and whether `ModsConfig.xml` exists. `refresh: true` rescans mods first. |
| `set_instance` | `name` | Switch the active RimWorld instance for this MCP session (session-only; does not modify `settings.json`). Returns the new status. |

### Inspecting mods

| Tool | Parameters | Description |
|------|-----------|-------------|
| `list_mods` | `query`, `source`, `active_only`, `inactive_only`, `limit`, `offset` | List installed mods. `limit` caps at 500. Supports case-insensitive search over name/packageId and filtering by source. |
| `get_mod_details` | `package_id` | Full details for a packageId: paths, source, load-after/patch-loads-before/incompatible rules, supported versions, duplicate matches. |

### Managing the active list

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_active_modlist` | — | Current active list in load order, plus missing/unknown entries. |
| `set_active_modlist` | `package_ids`, `allow_missing`, `backup` | Replace the entire active list. Rejects unknown or duplicate packageIds unless `allow_missing`; creates a backup by default. |
| `update_modlist` | `add`, `remove`, `move_id`, `move_to_index`, `allow_missing`, `backup` | Delta update of the current list. Returns before/after diff. |
| `validate_modlist` | — | Health report: missing mods, duplicates, load-order violations, incompatible pairs, dependency cycles. Reports are capped to keep responses compact. |
| `sort_modlist` | `method`, `dry_run` | Run the selected sorter (`Topological` or `Alphabetical`) and write the result. `dry_run: true` returns the sorted order without writing. |

### Modpacks

Modpacks are JSON snapshots stored in RimSort's saved-modlists folder. Names must match `^[A-Za-z0-9_-][\w \-.]{0,63}$`.

| Tool | Parameters | Description |
|------|-----------|-------------|
| `save_modpack` | `name` | Snapshot the current active list under `name`. |
| `load_modpack` | `name` | Read a saved modpack (does not change the active list). |
| `apply_modpack` | `name`, `allow_missing`, `backup` | Load a modpack into the active list. |
| `list_modpacks` | — | List saved modpacks with packageId counts. |

### Launching

| Tool | Parameters | Description |
|------|-----------|-------------|
| `launch_game` | `dry_run` | Start RimWorld (Steam protocol or direct executable, per settings). `dry_run: true` returns the launch plan without starting anything. |

## Resources

| URI | Description |
|-----|-------------|
| `rimsort://status` | JSON status of the current instance (same payload as `get_status`). |
| `rimsort://modlist/active` | Active mod list (packageIds in load order). |

## Prompts

| Prompt | Arguments | Description |
|--------|-----------|-------------|
| `assemble_modpack` | `goal` | Guided workflow for building a mod list from a goal. |
| `troubleshoot_active_list` | — | Guided workflow for diagnosing a broken active mod list. |

## Tips for Token-Efficient Use

- Prefer `list_mods` with `query`/`source`/`limit`/`offset` over dumping the full catalog.
- Use `validate_modlist` before asking for explanations — its report already contains the conflicts.
- Use `sort_modlist` with `dry_run: true` to preview changes cheaply.
- Read `rimsort://status` resources when you only need a small status payload.

## Safety and Limitations

- **Backups**: every write of `ModsConfig.xml` creates a timestamped backup under RimSort's backups folder (`.../backups/mcp/`); only the 20 most recent are kept.
- **Instance switching** (`set_instance`) affects only the running MCP session.
- **No GUI interlock**: the server does not detect a running RimSort GUI (or the reverse). Avoid editing the same list from both at once.
- **Sorter errors**: circular dependencies are reported as tool errors instead of a dialog.
- **Launch**: launching via `steam://` requires Steam Client Integration to be enabled in settings; otherwise use direct launch.

## Troubleshooting

**`Error: Game folder does not exist`**
The instance in `settings.json` points at a missing game directory. Fix the path in the RimSort GUI settings, or switch instances with `set_instance`.

**`Error: Unknown packageIds ...`**
`set_active_modlist` refuses to enable mods that are not installed. Run `list_mods`/`get_mod_details` to check spelling, or pass `allow_missing: true` if you accept partial writes.

**The client cannot start the server**
Run `uv run --directory /path/to/RimSort python -m app mcp --log-level DEBUG` manually to see stderr logs. Make sure `uv` is on the client's `PATH`, or replace `uv run --directory ... python` with the absolute interpreter inside your virtual environment.
