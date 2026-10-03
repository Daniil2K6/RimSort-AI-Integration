"""Controller for the AI Assistant tab of the settings dialog."""

from __future__ import annotations

from app.ai.prompt import (
    DEFAULT_SYSTEM_PROMPT,
    effective_system_prompt,
    normalize_system_prompt,
)
from app.controllers.settings_tabs.base_tab_controller import BaseTabController


class AiTabController(BaseTabController):
    """Binds the AI Assistant tab widgets to the settings model."""

    def connect_signals(self) -> None:
        self.dialog.ai_reset_prompt_button.clicked.connect(self._on_reset_prompt)

    def update_view_from_model(self) -> None:
        self.dialog.ai_base_url.setText(self.settings.ai_base_url)
        self.dialog.ai_api_key.setText(self.settings.ai_api_key)
        self.dialog.ai_model.setText(self.settings.ai_model)
        self.dialog.ai_system_prompt_edit.setPlainText(
            effective_system_prompt(self.settings.ai_system_prompt)
        )
        self._update_prompt_status()

    def update_model_from_view(self) -> None:
        self.settings.ai_base_url = self.dialog.ai_base_url.text().strip()
        self.settings.ai_api_key = self.dialog.ai_api_key.text().strip()
        self.settings.ai_model = self.dialog.ai_model.text().strip()
        self.settings.ai_system_prompt = normalize_system_prompt(
            self.dialog.ai_system_prompt_edit.toPlainText()
        )
        self._update_prompt_status()

    def _on_reset_prompt(self) -> None:
        self.dialog.ai_system_prompt_edit.setPlainText(DEFAULT_SYSTEM_PROMPT)
        self._update_prompt_status()

    def _update_prompt_status(self) -> None:
        is_default = (
            self.dialog.ai_system_prompt_edit.toPlainText().strip()
            == DEFAULT_SYSTEM_PROMPT.strip()
        )
        self.dialog.update_ai_prompt_status(is_default)
