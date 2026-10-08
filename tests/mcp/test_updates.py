"""Tests for check_workshop_updates / update_mods (headless SteamCMD)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.server import build_server
from tests.mcp.conftest import MCPEnv, write_mod
from tests.mcp.test_tools import call


def _fake_webapi(remote_time: int, *, fail: tuple[str, ...] = ()) -> Any:
    def fake(pfids: list[str]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        entries: list[dict[str, Any]] = []
        for pfid in pfids:
            result = 9 if pfid in fail else 1
            entries.append(
                {
                    "publishedfileid": pfid,
                    "result": result,
                    "time_updated": remote_time if result == 1 else 0,
                    "title": f"Mod {pfid}",
                }
            )
        return entries, list(fail), ["backend error"] if fail else []

    return fake


def _write_workshop_mod(env: MCPEnv, folder: str, pid: str, pfid: str) -> None:
    mod = write_mod(env.workshop, folder, pid, f"Workshop {pid}")
    (mod / "About" / "PublishedFileId.txt").write_text(pfid, encoding="utf-8")


def _setup_pfmod(
    env: MCPEnv, monkeypatch: pytest.MonkeyPatch, remote_time: int
) -> None:
    """Add a pfid-bearing workshop mod and mock the Steam WebAPI lookup."""
    _write_workshop_mod(env, "77788899", "test.pfmod", "77788899")
    env.ctx.refresh()
    from app.mcp import updates

    monkeypatch.setattr(
        updates,
        "ISteamRemoteStorage_GetPublishedFileDetails",
        _fake_webapi(remote_time),
    )


def test_check_updates_bad_sources(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="Unknown source"):
        call(server, "check_workshop_updates", {"sources": "GOG"})


def test_check_updates_outdated(
    mcp_env: MCPEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_pfmod(mcp_env, monkeypatch, 4_000_000_000)
    server = build_server(mcp_env.ctx)
    result = call(server, "check_workshop_updates")
    assert result["status"] == "success"
    # Fixture workshop mods resolve pfids from their numeric folder names.
    assert result["mods_checked"] == 3
    by_pfid = {entry["pfid"]: entry for entry in result["outdated"]}
    entry = by_pfid["77788899"]
    assert entry["id"] == "test.pfmod"
    assert entry["workshop_ts"] == 4_000_000_000
    assert entry["installed_ts"] < 4_000_000_000


def test_check_updates_current_copy_not_flagged(
    mcp_env: MCPEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_pfmod(mcp_env, monkeypatch, 1_000)
    server = build_server(mcp_env.ctx)
    result = call(server, "check_workshop_updates")
    assert result["outdated_count"] == 0
    assert result["mods_checked"] == 3


def test_check_updates_source_filter(
    mcp_env: MCPEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_pfmod(mcp_env, monkeypatch, 4_000_000_000)
    server = build_server(mcp_env.ctx)
    result = call(server, "check_workshop_updates", {"sources": "Steam CMD"})
    assert result["mods_checked"] == 0  # fixture mods are Steam Workshop, not CMD
    result_all = call(server, "check_workshop_updates", {"sources": "all"})
    assert result_all["mods_checked"] == 3


def test_update_mods_dry_run(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    result = call(
        server,
        "update_mods",
        {"publishedfileids": ["22222222", "11111111"], "dry_run": True},
    )
    assert result["dry_run"] is True
    assert result["count"] == 2
    assert result["publishedfileids"] == ["11111111", "22222222"]
    assert result["batches"] == 1


def test_update_mods_no_targets(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="Nothing to update"):
        call(server, "update_mods", {"dry_run": True})


def test_update_mods_invalid_pfid(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="Invalid publishedfileid"):
        call(server, "update_mods", {"publishedfileids": ["abc"], "dry_run": True})


def test_update_mods_unknown_package_id(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="No installed mod"):
        call(server, "update_mods", {"package_ids": ["no.such.mod"], "dry_run": True})


def test_update_mods_package_without_pfid(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="no PublishedFileId"):
        call(server, "update_mods", {"package_ids": ["test.moda"], "dry_run": True})


def test_update_mods_resolves_package_id(mcp_env: MCPEnv) -> None:
    _write_workshop_mod(mcp_env, "77788899", "test.pfmod", "77788899")
    mcp_env.ctx.refresh()
    server = build_server(mcp_env.ctx)
    result = call(
        server,
        "update_mods",
        {"package_ids": ["test.pfmod"], "dry_run": True},
    )
    assert result["publishedfileids"] == ["77788899"]
    assert result["targets"] == ["test.pfmod"]


def test_update_mods_requires_steamcmd(mcp_env: MCPEnv) -> None:
    mcp_env.write_settings(steamcmd_install_path=str(mcp_env.tmp_path / "steam_prefix"))
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="install_steamcmd"):
        call(server, "update_mods", {"publishedfileids": ["11111111"]})


def test_update_mods_bad_sources(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="Unknown source"):
        call(server, "update_mods", {"sources": "GOG", "dry_run": True})


def test_update_mods_rejects_bad_login(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    with pytest.raises(ToolError, match="login"):
        call(
            server,
            "update_mods",
            {"publishedfileids": ["1"], "login": "bad name", "dry_run": True},
        )


def test_update_mods_login_in_dry_run(mcp_env: MCPEnv) -> None:
    server = build_server(mcp_env.ctx)
    result = call(
        server,
        "update_mods",
        {"publishedfileids": ["1"], "login": "my.user", "dry_run": True},
    )
    assert result["login"] == "my.user"


def test_build_script_login_with_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.mcp.updates import (
        STEAM_GUARD_ENV,
        STEAM_PASSWORD_ENV,
        _build_download_script,
    )

    monkeypatch.setenv(STEAM_PASSWORD_ENV, "s3cret")
    monkeypatch.setenv(STEAM_GUARD_ENV, "ABCDE")
    script = Path(_build_download_script(tmp_path, ["111"], True, login="my_user"))
    try:
        content = script.read_text(encoding="utf-8")
        assert "set_steam_guard_code ABCDE" in content
        assert "login my_user s3cret" in content
        assert "workshop_download_item 294100 111 validate" in content
        assert script.stat().st_mode & 0o777 == 0o600
    finally:
        script.unlink(missing_ok=True)


def test_build_script_anonymous_ignores_password_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.mcp.updates import STEAM_PASSWORD_ENV, _build_download_script

    monkeypatch.setenv(STEAM_PASSWORD_ENV, "s3cret")
    script = Path(_build_download_script(tmp_path, ["222"], False))
    try:
        content = script.read_text(encoding="utf-8")
        assert "login anonymous" in content
        assert "s3cret" not in content
    finally:
        script.unlink(missing_ok=True)


def test_build_script_rejects_password_newlines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.mcp.updates import STEAM_PASSWORD_ENV, _build_download_script

    monkeypatch.setenv(STEAM_PASSWORD_ENV, "pw\nquit")
    with pytest.raises(ToolError, match="newlines"):
        _build_download_script(tmp_path, ["333"], False, login="user")
