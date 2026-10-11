"""Headless GitHub-mod install for the MCP layer (release-asset zip).

Reuses the GUI's :class:`GitHubProvider` (releases + cache) and
:class:`GitHubInstaller` (zip download/extract with zip-slip protection
and unwrap) without any Qt. Installs the latest stable release's mod
``.zip`` asset into the instance's local mods folder. By design it only
writes to disk — adding the mod to the active list stays a separate,
explicit step (``set_active_modlist`` / ``update_modlist``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.context import MCPContext


def _resolve_owner_repo(url: str) -> str:
    """Accept a full GitHub URL or a bare ``owner/repo`` slug."""
    from app.utils.github.provider import parse_github_url

    parsed = parse_github_url(url)
    if parsed is not None:
        return f"{parsed[0]}/{parsed[1]}"
    cleaned = url.strip().removesuffix(".git")
    if "://" not in cleaned and cleaned.count("/") == 1:
        owner, repo = cleaned.split("/", 1)
        if owner and repo:
            return cleaned
    raise ToolError(
        f"Not a valid GitHub repository URL or owner/repo slug: {url!r}. "
        "Use e.g. https://github.com/author/mod or author/mod."
    )


def install_github_mod(
    ctx: MCPContext,
    url: str,
    folder_name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Install a GitHub-hosted mod's latest stable release zip to disk.

    :param ctx: Session context (instance, local mods folder, settings).
    :param url: GitHub repository URL or ``owner/repo`` slug.
    :param folder_name: Target folder name under the local mods dir
        (defaults to the repository name).
    :param dry_run: Resolve the release/asset and target path without
        downloading anything.
    """
    from app.utils.github.installer import GitHubInstaller
    from app.utils.github.provider import (
        GitHubProvider,
        GitHubRateLimitError,
    )

    owner_repo = _resolve_owner_repo(url)
    local_folder = ctx.local_folder
    if local_folder is None:
        raise ToolError("Instance has no local mods folder; set one in Settings first.")

    token_raw = ctx.load_settings().get("github_token")
    token = str(token_raw) if token_raw else None
    provider = GitHubProvider(github_token=token, cache_session=None)

    try:
        releases = provider.get_releases(owner_repo)
    except GitHubRateLimitError as exc:
        raise ToolError(str(exc)) from exc

    release = GitHubProvider.get_latest_stable_release(releases)
    if release is None:
        raise ToolError(f"No releases found for {owner_repo}.")

    zips = release.get_custom_zip_assets()
    if not zips:
        raise ToolError(
            f"Release {release.tag!r} of {owner_repo} has no mod .zip asset "
            "(only auto-generated source archives). This repo may need a git "
            "clone instead of a release install."
        )
    # Largest custom zip is the most likely full mod payload.
    asset = max(zips, key=lambda a: a.size)
    repo_name = owner_repo.split("/", 1)[1]
    target = local_folder / (folder_name or repo_name)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "owner_repo": owner_repo,
            "tag": release.tag,
            "asset": asset.name,
            "asset_url": asset.download_url,
            "size_bytes": asset.size,
            "target_dir": str(target),
        }

    backup: Path | None = None
    if target.exists():
        logger.info(f"Backing up existing mod folder before install: {target}")
        backup = GitHubInstaller.backup_mod(target)
    try:
        unwrap = GitHubInstaller.download_and_extract_release(
            asset.download_url, asset.name, str(target)
        )
    except Exception as exc:
        if backup is not None:
            GitHubInstaller.restore_backup(backup, target)
        raise ToolError(
            f"Failed to download/extract {asset.name} for {owner_repo}: {exc}"
        ) from exc
    if backup is not None:
        GitHubInstaller.delete_backup(backup)

    ctx.refresh()
    return {
        "ok": True,
        "owner_repo": owner_repo,
        "tag": release.tag,
        "asset": asset.name,
        "target_dir": str(target),
        "unwrapped": unwrap.value,
        "note": "Installed to disk only; add it to the active list to use it.",
    }


__all__ = ["install_github_mod"]
