"""Tests for the steam-login / update-mods CLI subcommands."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from app.cli.main import cli
from app.mcp.context import SETTINGS_ENV_VAR
from tests.mcp.conftest import MCPEnv


def test_steam_subcommands_registered() -> None:
    for name in ("steam-login", "update-mods"):
        result = CliRunner().invoke(cli, [name, "--help"])
        assert result.exit_code == 0, result.output


def test_update_mods_dry_run(mcp_env: MCPEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(mcp_env.settings_path))
    result = CliRunner().invoke(
        cli,
        ["update-mods", "--dry-run", "--pfid", "11111111", "--pfid", "22222222"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["count"] == 2
    assert payload["publishedfileids"] == ["11111111", "22222222"]
    assert payload["login"] == "anonymous"


def test_update_mods_bad_login(
    mcp_env: MCPEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(mcp_env.settings_path))
    result = CliRunner().invoke(
        cli,
        ["update-mods", "--login", "bad name", "--dry-run", "--pfid", "1"],
    )
    assert result.exit_code != 0
    assert "login" in result.output


def test_update_mods_no_targets(
    mcp_env: MCPEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(mcp_env.settings_path))
    result = CliRunner().invoke(cli, ["update-mods", "--dry-run", "--all"])
    assert result.exit_code != 0
    assert "Nothing to update" in result.output
