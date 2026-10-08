"""Tests for AiTabController provider-mirror view↔model sync."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from app.ai.providers import new_provider
from app.controllers.settings_tabs.ai_tab_controller import AiTabController

if TYPE_CHECKING:
    from app.models.settings import Settings


class TestAiTabUpdateView:
    def test_view_shows_active_provider(
        self, ai_tab: tuple[AiTabController, Settings, MagicMock]
    ) -> None:
        """Endpoint fields display the active provider, not raw legacy data."""
        controller, settings, dialog = ai_tab
        provider = new_provider("Second", "https://b.example/v1", "key-b", ["mb"])
        settings.ai_providers = [provider]
        settings.ai_provider_id = provider["id"]
        settings.ai_model = "mb"

        controller.update_view_from_model()

        dialog.ai_base_url.setText.assert_called_with("https://b.example/v1")
        dialog.ai_api_key.setText.assert_called_with("key-b")
        dialog.ai_model.setText.assert_called_with("mb")

    def test_view_seeds_provider_from_legacy(
        self, ai_tab: tuple[AiTabController, Settings, MagicMock]
    ) -> None:
        """A first-time open creates the provider from legacy fields."""
        controller, settings, dialog = ai_tab
        assert settings.ai_providers == []

        controller.update_view_from_model()

        assert len(settings.ai_providers) == 1
        dialog.ai_base_url.setText.assert_called_with("https://api.openai.com/v1")


class TestAiTabUpdateModel:
    def test_model_updates_active_provider_and_legacy(
        self, ai_tab: tuple[AiTabController, Settings, MagicMock]
    ) -> None:
        """Edited endpoint fields land in the active provider and ai_* fields."""
        controller, settings, dialog = ai_tab
        provider = new_provider("One", "https://a.example/v1", "old", ["m1"])
        settings.ai_providers = [provider]
        settings.ai_provider_id = provider["id"]

        dialog.ai_base_url.text.return_value = "https://c.example/v1 "
        dialog.ai_api_key.text.return_value = " new-key "
        dialog.ai_model.text.return_value = "m2"
        dialog.ai_system_prompt_edit.toPlainText.return_value = ""

        controller.update_model_from_view()

        updated = settings.ai_providers[0]
        assert updated["base_url"] == "https://c.example/v1"
        assert updated["api_key"] == "new-key"
        assert settings.ai_base_url == "https://c.example/v1"
        assert settings.ai_api_key == "new-key"
        assert settings.ai_model == "m2"
