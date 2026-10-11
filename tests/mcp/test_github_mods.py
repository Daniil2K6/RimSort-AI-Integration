"""Tests for the GitHub-mod install tool and the Workshop download wrapper."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.ai.agent import requires_confirmation
from app.mcp.github_mods import _resolve_owner_repo, install_github_mod
from tests.mcp.conftest import MCPEnv


class _FakeAsset:
    def __init__(self, name: str, url: str, size: int) -> None:
        self.name = name
        self.download_url = url
        self.size = size


class _FakeRelease:
    def __init__(self, tag: str, zips: list[_FakeAsset]) -> None:
        self.tag = tag
        self._zips = zips

    def get_custom_zip_assets(self) -> list[_FakeAsset]:
        return self._zips


class _FakeProvider:
    def __init__(self, github_token: str | None = None, cache_session: Any = None):
        self.token = github_token

    def get_releases(self, owner_repo: str) -> list[Any]:
        asset = _FakeAsset("mod.zip", "http://x/mod.zip", 10)
        return [_FakeRelease("v1.2.3", [asset])]

    @staticmethod
    def get_latest_stable_release(releases: list[Any]) -> Any:
        return releases[0] if releases else None


class _Unwrap:
    value = "unwrapped"


def test_resolve_owner_repo_url_and_slug() -> None:
    assert _resolve_owner_repo("https://github.com/author/mod") == "author/mod"
    assert _resolve_owner_repo("https://github.com/author/mod.git") == "author/mod"
    assert _resolve_owner_repo("author/mod") == "author/mod"


def test_resolve_owner_repo_rejects_garbage() -> None:
    with pytest.raises(ToolError):
        _resolve_owner_repo("not a repo")


def test_install_github_mod_dry_run(
    mcp_env: MCPEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.utils.github.provider.GitHubProvider", _FakeProvider)
    result = install_github_mod(
        mcp_env.ctx, "https://github.com/author/mod", dry_run=True
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["owner_repo"] == "author/mod"
    assert result["tag"] == "v1.2.3"
    assert result["asset"] == "mod.zip"
    assert result["target_dir"].endswith("mod")


def test_install_github_mod_downloads_to_local_folder(
    mcp_env: MCPEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}

    class _FakeInstaller:
        @staticmethod
        def download_and_extract_release(url: str, name: str, target: str) -> _Unwrap:
            calls.update(url=url, name=name, target=target)
            return _Unwrap()

        @staticmethod
        def backup_mod(path: Any) -> Any:
            return path.parent / f"{path.name}.rimsort_backup"

        @staticmethod
        def restore_backup(backup: Any, path: Any) -> None: ...

        @staticmethod
        def delete_backup(backup: Any) -> None: ...

    monkeypatch.setattr("app.utils.github.provider.GitHubProvider", _FakeProvider)
    monkeypatch.setattr("app.utils.github.installer.GitHubInstaller", _FakeInstaller)

    result = install_github_mod(mcp_env.ctx, "author/mod")
    assert result["ok"] is True
    assert calls["url"] == "http://x/mod.zip"
    assert calls["name"] == "mod.zip"
    assert calls["target"].endswith("mod")


def test_install_github_mod_no_zip_asset_errors(
    mcp_env: MCPEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _NoZipRelease:
        tag = "v1"

        def get_custom_zip_assets(self) -> list[_FakeAsset]:
            return []

    class _NoZipProvider(_FakeProvider):
        def get_releases(self, owner_repo: str) -> list[Any]:
            return [_NoZipRelease()]

    monkeypatch.setattr("app.utils.github.provider.GitHubProvider", _NoZipProvider)
    with pytest.raises(ToolError):
        install_github_mod(mcp_env.ctx, "author/mod")


def test_download_workshop_mod_wraps_update_mods(
    mcp_env: MCPEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.mcp import updates
    from app.mcp.server import build_server
    from tests.mcp.test_tools import call

    captured: dict[str, Any] = {}

    def _fake_update_mods(ctx: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(updates, "update_mods", _fake_update_mods)

    server = build_server(mcp_env.ctx)
    result = call(server, "download_workshop_mod", {"publishedfileid": "12345"})
    assert result["ok"] is True
    assert captured["publishedfileids"] == ["12345"]


def test_download_tools_require_confirmation_unless_dry_run() -> None:
    assert requires_confirmation("download_workshop_mod", {"publishedfileid": "1"})
    assert not requires_confirmation(
        "download_workshop_mod", {"publishedfileid": "1", "dry_run": True}
    )
    assert requires_confirmation("install_github_mod", {"url": "a/b"})
    assert not requires_confirmation(
        "install_github_mod", {"url": "a/b", "dry_run": True}
    )
