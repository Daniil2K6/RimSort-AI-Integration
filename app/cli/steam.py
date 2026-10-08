"""SteamCMD login and headless mod-update subcommands for the RimSort CLI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import click
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp import updates as updates_mod
from app.mcp.context import MCPContext, MCPContextError


def _prefix(ctx: MCPContext) -> Path:
    raw = str(ctx.instance.steamcmd_install_path or "")
    if not raw:
        raise click.ClickException("Instance has no steamcmd_install_path configured")
    return Path(raw)


def _ensure_steamcmd(prefix: Path, install: bool) -> Path:
    executable = updates_mod._steamcmd_executable(prefix)
    if executable.is_file():
        return executable
    if not install:
        raise click.ClickException(
            f"SteamCMD not found at {executable}. Re-run without --no-install "
            "to download it from the official Valve CDN (requires internet)."
        )
    click.echo(f"SteamCMD not found at {executable}.")
    click.echo(
        "Downloading SteamCMD from the official Valve CDN (requires internet)..."
    )
    try:
        updates_mod._install_steamcmd(prefix)
    except ToolError as exc:
        raise click.ClickException(str(exc)) from exc
    return executable


@click.command()
@click.argument("username", required=False)
@click.option(
    "--no-install",
    "no_install",
    is_flag=True,
    help="Fail instead of downloading SteamCMD when it is missing.",
)
def steam_login(username: str | None, no_install: bool) -> None:
    """Log SteamCMD in interactively with your Steam account.

    A SteamCMD window opens in this terminal: type your password and
    Steam Guard code yourself — RimSort never sees or stores them.
    Afterwards the session is available for headless runs via
    `update_mods(login=...)` / `rimsort update-mods --login NAME`
    (password through the RIMSORT_STEAM_PASSWORD env var if SteamCMD
    asks again).

    \b
    Examples:
        rimsort steam-login my_account
    """
    try:
        prefix = _prefix(MCPContext())
    except MCPContextError as exc:
        raise click.ClickException(str(exc)) from exc
    executable = _ensure_steamcmd(prefix, install=not no_install)

    name = username or click.prompt("Steam account name")
    try:
        name = updates_mod._validate_login(name)
    except ToolError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Starting SteamCMD for login as {name!r}...")
    click.echo("Enter your password (and Steam Guard code) when SteamCMD asks.")
    try:
        proc = subprocess.run(
            [str(executable), "+login", name, "+quit"],
            env=updates_mod._steamcmd_env(prefix),
            cwd=str(prefix),
            check=False,
        )
    except OSError as exc:
        raise click.ClickException(f"Failed to launch SteamCMD: {exc}") from exc
    if proc.returncode != 0:
        raise click.ClickException(f"SteamCMD exited with code {proc.returncode}")
    click.echo(
        f"SteamCMD session for {name!r} is ready. Headless updates can now use "
        f"--login {name}."
    )


@click.command()
@click.option(
    "--login",
    "login_name",
    default="anonymous",
    show_default=True,
    help="Steam account for SteamCMD (anonymous works for RimWorld workshop).",
)
@click.option(
    "--outdated-only/--all",
    "outdated_only",
    default=True,
    show_default=True,
    help="Only mods whose workshop copy is newer than the installed one.",
)
@click.option(
    "--pfid",
    "pfids",
    multiple=True,
    help="Explicit publishedfileid to download (repeatable).",
)
@click.option(
    "--package-id",
    "package_ids",
    multiple=True,
    help="Installed packageId to update (repeatable).",
)
@click.option(
    "--sources",
    "sources",
    default="Steam CMD",
    show_default=True,
    help='Comma-separated filter for the outdated check: "Steam CMD", '
    '"Steam Workshop", or "all".',
)
@click.option(
    "--install-steamcmd",
    is_flag=True,
    help="Download SteamCMD into the instance prefix when missing.",
)
@click.option(
    "--validate/--no-validate",
    "validate",
    default=None,
    help="Force SteamCMD validate (default: steamcmd_validate_downloads setting).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Only resolve targets, do not download anything.",
)
def update_mods(
    login_name: str,
    outdated_only: bool,
    pfids: tuple[str, ...],
    package_ids: tuple[str, ...],
    sources: str,
    install_steamcmd: bool,
    validate: bool | None,
    dry_run: bool,
) -> None:
    """Update Workshop/SteamCMD mods through SteamCMD (headless).

    Checks the Steam WebAPI and downloads what is outdated — suitable
    for scheduled auto-updates (cron/launchd). Prints a JSON report and
    exits non-zero when some mods failed.

    \b
    Examples:
        rimsort update-mods --dry-run
        rimsort update-mods --install-steamcmd
        rimsort update-mods --login my_account --outdated-only
    """
    try:
        ctx = MCPContext()
        result = updates_mod.update_mods(
            ctx,
            package_ids=list(package_ids) or None,
            publishedfileids=list(pfids) or None,
            outdated_only=outdated_only and not (package_ids or pfids),
            sources=sources,
            validate=validate,
            install_steamcmd=install_steamcmd,
            login=login_name,
            dry_run=dry_run,
        )
    except (ToolError, MCPContextError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))
    failed = int(result.get("failed_count", 0))
    if failed:
        raise click.ClickException(f"{failed} mod(s) failed to update")
