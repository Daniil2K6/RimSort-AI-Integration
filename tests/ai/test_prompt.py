"""Tests for the built-in default system prompt."""

from __future__ import annotations

from app.ai.prompt import (
    DEFAULT_SYSTEM_PROMPT,
    effective_system_prompt,
    normalize_system_prompt,
)

PART_1 = "== ЧАСТЬ 1. ПОВЕДЕНИЕ И МАНЕРА ОБЩЕНИЯ =="
PART_2 = "== ЧАСТЬ 2. СТРОГИЕ ИНСТРУКЦИИ: ГДЕ, КАК И ЧТО БРАТЬ =="
PART_3 = "== ЧАСТЬ 3. ТЕХНИЧЕСКИЕ УКАЗАНИЯ (обычный пользователь сюда не заходит) =="

REQUIRED_REFERENCES = [
    "https://steamcommunity.com/app/294100/workshop/",
    "https://steamcommunity.com/sharedfiles/filedetails/?id=<числовой id>",
    "Download -> GitHub Mods",
    "https://github.com/<user>/<repo>",
    "https://github.com/Daniil2K6/RimSort-AI-Integration/issues",
    "https://rimsort.github.io/RimSort/",
    "https://ludeon.com/forums/",
    "python -m app mcp",
    "RIMSORT_MCP_SETTINGS",
    "app/mcp/server.py",
    "app/mcp/context.py",
    "ModsConfig.xml",
    "Settings -> Locations",
    "set_instance",
    "get_status",
    "dry_run",
]


def test_prompt_contains_three_ordered_parts() -> None:
    first = DEFAULT_SYSTEM_PROMPT.find(PART_1)
    second = DEFAULT_SYSTEM_PROMPT.find(PART_2)
    third = DEFAULT_SYSTEM_PROMPT.find(PART_3)
    assert first != -1
    assert second != -1
    assert third != -1
    assert first < second < third


def test_prompt_lists_required_references() -> None:
    for needle in REQUIRED_REFERENCES:
        assert needle in DEFAULT_SYSTEM_PROMPT, needle


def test_prompt_lines_have_no_trailing_whitespace() -> None:
    lines = DEFAULT_SYSTEM_PROMPT.splitlines()
    assert not any(line.endswith(" ") for line in lines)
    assert "\t" not in DEFAULT_SYSTEM_PROMPT
    assert DEFAULT_SYSTEM_PROMPT.endswith("\n") is False


def test_normalize_stores_default_as_empty() -> None:
    assert normalize_system_prompt(DEFAULT_SYSTEM_PROMPT) == ""
    assert normalize_system_prompt(DEFAULT_SYSTEM_PROMPT + "\n") == ""
    assert normalize_system_prompt("") == ""
    assert normalize_system_prompt("   \n ") == ""


def test_normalize_keeps_custom_prompt() -> None:
    assert normalize_system_prompt("  Always answer in English.  ") == (
        "Always answer in English."
    )


def test_effective_prompt_falls_back_to_default() -> None:
    assert effective_system_prompt("") == DEFAULT_SYSTEM_PROMPT
    assert effective_system_prompt("   ") == DEFAULT_SYSTEM_PROMPT
    assert effective_system_prompt("Custom rules") == "Custom rules"
