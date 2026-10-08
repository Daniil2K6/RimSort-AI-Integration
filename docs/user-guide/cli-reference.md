---
title: CLI Reference
nav_order: 8
layout: default
parent: User Guide
permalink: user-guide/cli-reference
---
# CLI Reference

{: .no_toc}

RimSort provides a command-line interface for headless operation of key features. This enables automation workflows, CI/CD integration, and scripting without requiring the graphical interface.

## Table of Contents

{: .no_toc .text-delta }

1. TOC
{:toc}

## Overview

The RimSort CLI is designed for users who need to automate RimSort functionality in headless environments. Unlike the graphical interface, the CLI:

- **Requires no display server** - Perfect for Docker containers, remote servers, and CI/CD pipelines
- **Provides structured exit codes** - Enables reliable error handling in scripts and automation
- **Supports environment variables** - Securely configure credentials without exposing them in command history
- **Works without Qt dependencies** - Lightweight operation with minimal system requirements

Currently available commands:

- `build-db` - Build Steam Workshop metadata databases
- `mcp` - Run the local MCP (Model Context Protocol) server for AI agents
- `steam-login` - Log SteamCMD in with your Steam account (interactive)
- `update-mods` - Headless Workshop/SteamCMD mod updates via SteamCMD

Additional commands may be added in future versions to support more RimSort functionality.

## Running the CLI

If you have RimSort installed from a release build:

```bash
./RimSort build-db --help
```

or on windows:

```bash
RimSort.exe build-db --help
```

If you're running from source:

```bash
python -m app build-db --help

# Or with uv:
uv run python -m app build-db --help
```

## Commands

### `build-db`

Build a Steam Workshop metadata database by querying the Steam WebAPI. The resulting database contains mod names, URLs, dependencies, and optional DLC requirements in a JSON format compatible with RimSort and RimPy.

#### Prerequisites

**Steam WebAPI Key**
{: .d-inline-block}
Required
{: .label .label-red }

{: .important }
For detailed instructions on obtaining your Steam WebAPI key, see the [DB Builder guide](db-builder#how-to-obtain-your-steam-webapi-key-for-use-with-with-db-builder-dynamicquery).

{: .warning}
You need to own RimWorld on Steam for this to work. You may also need to have spent at least $5 USD on your Steam account to have general access to the Steam WebAPI to utilize Steamworks API (for DLC dependency data).

You need a Steam WebAPI key (32 characters) to use this command. The DB Builder has some "soft requirements" inherited from Steam's API access policies:

#### Basic Usage

```bash
# Using environment variable (recommended for security)
export RIMSORT_STEAM_API_KEY=your_32_character_key_here
RimSort build-db --output steamDB.json

# Quick build without DLC data (faster)
RimSort build-db --output steamDB.json --no-dlc-data --quiet

# Update existing database instead of overwriting
RimSort build-db --output steamDB.json --update
```

#### Options Reference

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--api-key TEXT` | String | (see below) | Steam WebAPI key (32 characters). Can also be set via `RIMSORT_STEAM_API_KEY` environment variable. |
| `--output PATH` | Path | **required** | Output JSON file path for the database. |
| `--dlc-data/--no-dlc-data` | Boolean | dlc-data | Include DLC dependency data via Steamworks API. Requires Steam client running and RimWorld ownership. Significantly slower due to additional API calls. |
| `--update/--overwrite` | Boolean | overwrite | Update existing database (merge new data) or overwrite completely. |
| `--quiet` | Flag | false | Suppress progress output. Errors are still written to stderr. |

#### Environment Variables & Configuration

The Steam API key can be provided in three ways, with the following priority order:

1. **`--api-key` command line argument** - Highest priority, but may expose the key in shell history
2. **`RIMSORT_STEAM_API_KEY` environment variable** - Recommended for security
3. **Fallback to `settings.json`** - If you've configured RimSort GUI, the CLI will use that API key

#### Exit Codes

The `build-db` command uses standard exit codes for automation:

- **0** - Success: Database built/updated successfully
- **1** - Error: Validation failed, build failed, or exception occurred
- **2** - Interrupted: User cancelled with Ctrl+C

### `mcp`

Run the local RimSort MCP server over stdio so that an AI agent (opencode, Claude Code, Cursor, …) can manage your mod lists. The server exposes tools for status, mod inspection, active-list editing, validation, sorting, modpacks, and launching the game.

#### Basic Usage

```bash
# From source
uv run python -m app mcp

# With explicit stderr logging
uv run python -m app mcp --log-level DEBUG
```

#### Options Reference

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--log-level TEXT` | String | `INFO` | Log verbosity on stderr (`DEBUG`, `INFO`, `WARNING`, `ERROR`). stdout carries only the MCP protocol. |

#### Environment Variables

| Variable | Description |
|----------|-------------|
| `RIMSORT_MCP_SETTINGS` | Path to an alternative `settings.json` (tests, isolated instances). Defaults to RimSort's normal settings file. |

{: .note }
For the full tool reference, client configuration examples, and limitations, see the [MCP Integration guide](mcp).

### `steam-login`

Log SteamCMD in with your Steam account so headless downloads can run without an interactive prompt. You type your password and Steam Guard code directly into the SteamCMD prompt — RimSort never sees or stores them.

{: .note }
Anonymous login is enough for RimWorld Workshop mods. A real account is only needed for private workshop items, depot downloads, or when SteamCMD refuses anonymous for some other reason.

#### Basic Usage

```bash
uv run python -m app steam-login my_account
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--no-install` | Flag | off | Fail instead of downloading SteamCMD when it is missing. |

For unattended runs afterwards, SteamCMD may still prompt for a password; supply it via the `RIMSORT_STEAM_PASSWORD` environment variable (plus `RIMSORT_STEAM_GUARD_CODE` for a first Steam Guard login). Credentials are never written to `settings.json` or logs.

### `update-mods`

Check the Steam WebAPI for outdated Workshop/SteamCMD mods and refresh them through SteamCMD. Designed for scheduled auto-updates (cron/launchd) and servers without a GUI.

#### Basic Usage

```bash
# See what would be updated
uv run python -m app update-mods --dry-run

# Update everything that is outdated (installs SteamCMD if missing)
uv run python -m app update-mods --install-steamcmd

# Logged-in account, explicit targets
uv run python -m app update-mods --login my_account --pfid 1234567890
```

#### Options Reference

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--login TEXT` | String | `anonymous` | Steam account for SteamCMD (run `steam-login` first). |
| `--outdated-only` / `--all` | Flag | `--outdated-only` | Only mods newer on the workshop, or every resolved target. |
| `--pfid ID` | Multi | — | Explicit publishedfileid (repeatable). |
| `--package-id ID` | Multi | — | Installed packageId (repeatable). |
| `--sources TEXT` | String | `Steam CMD` | Outdated-check filter: `Steam CMD`, `Steam Workshop`, or `all`. |
| `--install-steamcmd` | Flag | off | Download SteamCMD into the instance prefix when missing. |
| `--validate` / `--no-validate` | Flag | setting | Force/disable SteamCMD `validate` on downloads. |
| `--dry-run` | Flag | off | Resolve targets without downloading anything. |

Prints a JSON report and exits non-zero when some mods failed. Example daily schedule:

```bash
0 6 * * * cd /path/to/RimSort && uv run python -m app update-mods >> /tmp/rimsort-update.log 2>&1
```

### Troubleshooting

##### **`Error: Steam API key is required`**

The command cannot find a valid API key. Provide it via:

- `--api-key` command line option
- `RIMSORT_STEAM_API_KEY` environment variable
- Configure in RimSort GUI (saved to `settings.json`)

##### **`Error: Invalid Steam WebAPI key! Key must be 32 characters`**

Your API key is not the correct length. Check your key at [https://steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey). Common issue: extra spaces or newlines when copying the key.

##### **`Error: Cannot update non-existent database`**

You used `--update` mode but the target database file doesn't exist. For the first build, use `--overwrite` (the default):

```bash
# First build
RimSort build-db --output steamDB.json --overwrite

# Subsequent updates
RimSort build-db --output steamDB.json --update
```

##### **`DLC data collection fails silently`**

DLC dependency data requires the Steamworks API, which needs:

- Steam client running and authenticated
- RimWorld owned on your Steam account

If unavailable, use `--no-dlc-data` for headless environments:

```bash
RimSort build-db --output steamDB.json --no-dlc-data
```
