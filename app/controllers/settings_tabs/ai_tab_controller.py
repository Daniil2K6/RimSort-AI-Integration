"""Controller for the AI Assistant tab of the settings dialog."""

from __future__ import annotations

from app.ai.prompt import (
    DEFAULT_SYSTEM_PROMPT,
    effective_system_prompt,
    normalize_system_prompt,
)
from app.ai.providers import active_provider, ensure_providers
from app.controllers.settings_tabs.base_tab_controller import BaseTabController


class AiTabController(BaseTabController):
    """Binds the AI Assistant tab widgets to the settings model.

    The endpoint fields mirror the *active provider* (see
    ``app.ai.providers``); legacy ``ai_*`` settings stay in sync so older
    settings files keep working.
    """

    def connect_signals(self) -> None:
        self.dialog.ai_reset_prompt_button.clicked.connect(self._on_reset_prompt)
        self.dialog.ai_add_provider_button.clicked.connect(self._on_add_provider)

    def update_view_from_model(self) -> None:
        ensure_providers(self.settings)
        provider = active_provider(self.settings)
        if provider is not None:
            self.dialog.ai_base_url.setText(str(provider.get("base_url", "")))
            self.dialog.ai_api_key.setText(str(provider.get("api_key", "")))
        else:
            self.dialog.ai_base_url.setText(self.settings.ai_base_url)
            self.dialog.ai_api_key.setText(self.settings.ai_api_key)
        self.dialog.ai_model.setText(self.settings.ai_model)
        self.dialog.ai_system_prompt_edit.setPlainText(
            effective_system_prompt(self.settings.ai_system_prompt)
        )
        self._update_prompt_status()

    def update_model_from_view(self) -> None:
        base_url = self.dialog.ai_base_url.text().strip()
        api_key = self.dialog.ai_api_key.text().strip()
        model = self.dialog.ai_model.text().strip()
        provider = active_provider(self.settings)
        if provider is not None:
            updated = {**provider, "base_url": base_url, "api_key": api_key}
            self.settings.ai_providers = [
                updated if item.get("id") == updated.get("id") else item
                for item in self.settings.ai_providers
            ]
        self.settings.ai_base_url = base_url
        self.settings.ai_api_key = api_key
        self.settings.ai_model = model
        self.settings.ai_system_prompt = normalize_system_prompt(
            self.dialog.ai_system_prompt_edit.toPlainText()
        )
        self._update_prompt_status()

    def _on_reset_prompt(self) -> None:
        self.dialog.ai_system_prompt_edit.setPlainText(DEFAULT_SYSTEM_PROMPT)
        self._update_prompt_status()

    def _on_add_provider(self) -> None:
        from app.windows.ai_provider_dialog import prompt_add_provider

        ensure_providers(self.settings)
        provider = prompt_add_provider(self.settings, self.dialog)
        if provider is None:
            return
        self.update_view_from_model()

    def _update_prompt_status(self) -> None:
        is_default = (
            self.dialog.ai_system_prompt_edit.toPlainText().strip()
            == DEFAULT_SYSTEM_PROMPT.strip()
        )
        self.dialog.update_ai_prompt_status(is_default)
