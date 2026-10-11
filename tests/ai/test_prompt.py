"""Tests for the built-in default system prompt."""

from __future__ import annotations

from app.ai.prompt import (
    DEFAULT_SYSTEM_PROMPT,
    effective_system_prompt,
    normalize_system_prompt,
)

PART_1 = "ПОВЕДЕНИЕ"
PART_2 = "ИНСТРУМЕНТЫ"
PART_3 = "ИСТОЧНИКИ МОДОВ"
PART_4 = "ПОРЯДОК И ПРОВЕРКА"
PART_5 = "АГЕНТНЫЕ СЦЕНАРИИ"

REQUIRED_REFERENCES = [
    "https://steamcommunity.com/app/294100/workshop/",
    "sharedfiles/filedetails/?id=",
    "Download -> GitHub Mods",
    "About/About.xml",
    "ModsConfig.xml",
    "get_status",
    "validate_modlist",
    "preview_modlist_changes",
    "apply_modlist_plan",
    "dry_run",
]


def test_prompt_has_ordered_sections() -> None:
    order = [PART_1, PART_2, PART_3, PART_4, PART_5]
    positions = [DEFAULT_SYSTEM_PROMPT.find(section) for section in order]
    assert all(pos != -1 for pos in positions), positions
    assert positions == sorted(positions)


def test_prompt_is_compact() -> None:
    """Stage F: the default prompt is trimmed well below the original size."""
    # The original was ~1034 words / ~8469 chars. The compact prompt must cut
    # at least 40% of tokens (proxy: words).
    assert len(DEFAULT_SYSTEM_PROMPT.split()) < 700
    assert len(DEFAULT_SYSTEM_PROMPT) < 5500


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
