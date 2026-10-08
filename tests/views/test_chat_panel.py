"""Tests for the AI chat panel: layout, sessions, collapse, providers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication

from app.ai.chat_store import ChatMessage, ChatSession
from app.ai.providers import new_provider
from app.models.settings import Settings
from app.views.chat_panel import (
    ChatPanel,
    _luminance,
    _mix_colors,
    _parse_hex_color,
)

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def chat_panel(
    qapp: QApplication | QCoreApplication,
    fresh_event_bus: None,
) -> Generator[ChatPanel, None, None]:
    """A ChatPanel with real Settings backed by the mocked AppInfo."""
    panel = ChatPanel(
        settings=Settings(),
        show_settings_dialog=MagicMock(),
    )
    yield panel
    panel.shutdown()


def _save_session_with_message(panel: ChatPanel, title: str, text: str) -> str:
    session = panel._store.new_session()
    session.title = title
    session.messages.append(ChatMessage(role="user", content=text))
    panel._store.save(session)
    return session.id


class TestChatPanelLayout:
    def test_starts_with_new_chat_and_visible_panels(
        self, chat_panel: ChatPanel
    ) -> None:
        """A fresh panel opens a new chat with sidebar and body visible."""
        assert chat_panel.title_label.text() == "New chat"
        assert chat_panel._session.is_empty
        assert chat_panel.history_list.count() == 0
        assert not chat_panel.splitter.isHidden()
        assert chat_panel.history_button.isChecked()
        assert not chat_panel.collapse_button.isChecked()

    def test_collapse_hides_body_and_expand_restores(
        self, chat_panel: ChatPanel
    ) -> None:
        """Collapse reduces the panel to its header; focus re-expands."""
        chat_panel.collapse_button.setChecked(True)
        assert chat_panel.splitter.isHidden()
        assert chat_panel.collapse_button.text() == "Expand"

        chat_panel.focus_input()
        assert not chat_panel.splitter.isHidden()
        assert chat_panel.collapse_button.text() == "Collapse"
        assert not chat_panel._collapsed

    def test_history_button_toggles_sidebar(self, chat_panel: ChatPanel) -> None:
        chat_panel.history_button.setChecked(False)
        assert chat_panel.history_list.isHidden()
        chat_panel.history_button.setChecked(True)
        assert not chat_panel.history_list.isHidden()


class TestChatPanelSessions:
    def test_saved_session_appears_in_sidebar(self, chat_panel: ChatPanel) -> None:
        _save_session_with_message(chat_panel, "My chat", "hello world")
        chat_panel._refresh_sidebar()
        assert chat_panel.history_list.count() == 1
        assert chat_panel.history_list.item(0).text() == "My chat"

    def test_switch_chat_renders_messages_and_title(
        self, chat_panel: ChatPanel
    ) -> None:
        chat_id = _save_session_with_message(chat_panel, "Saved chat", "payload")
        chat_panel._switch_chat(chat_id)

        assert chat_panel._session.id == chat_id
        assert chat_panel.title_label.text() == "Saved chat"
        assert "payload" in chat_panel.history_view.toPlainText()

    def test_new_chat_clears_view_and_keeps_saves(self, chat_panel: ChatPanel) -> None:
        chat_id = _save_session_with_message(chat_panel, "Old", "bye")
        chat_panel._switch_chat(chat_id)
        chat_panel._new_chat()

        assert chat_panel._session.is_empty
        assert chat_panel.title_label.text() == "New chat"
        assert "bye" not in chat_panel.history_view.toPlainText()
        assert chat_panel._store.load(chat_id) is not None
        assert chat_panel.history_list.count() == 1

    def test_clear_deletes_saved_session(self, chat_panel: ChatPanel) -> None:
        chat_id = _save_session_with_message(chat_panel, "Doomed", "bye")
        chat_panel._switch_chat(chat_id)

        chat_panel._clear()

        assert chat_panel._store.load(chat_id) is None
        assert chat_panel.history_list.count() == 0
        assert chat_panel.title_label.text() == "New chat"

    def test_send_titles_and_saves_session(
        self, chat_panel: ChatPanel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first sent message titles the chat and persists it."""
        from app.views import chat_panel as chat_panel_module

        class _FakeWorker:
            def __init__(self, **kwargs: Any) -> None:
                self.activity = MagicMock()
                self.confirm_requested = MagicMock()
                self.finished_ok = MagicMock()
                self.failed = MagicMock()
                self.finished = MagicMock()

            def start(self) -> None:
                return None

        monkeypatch.setattr(chat_panel_module, "AgentWorker", _FakeWorker)
        chat_panel.settings.ai_model = "test-model"
        chat_panel.input.setPlainText("sort my mods please")

        chat_panel._send()

        assert chat_panel._session.title == "sort my mods please"
        assert chat_panel.history_list.count() == 1
        assert chat_panel.history_list.item(0).text() == "sort my mods please"
        loaded = chat_panel._store.load(chat_panel._session.id)
        assert loaded is not None
        assert loaded.messages[0].content == "sort my mods please"
        # Drop the fake worker so panel shutdown does not wait for it.
        chat_panel._worker = None


class TestChatPanelProviders:
    def test_model_menu_lists_providers_and_models(self, chat_panel: ChatPanel) -> None:
        provider = new_provider(
            "Local", "http://localhost:11434/v1", models=["llama3", "mistral"]
        )
        chat_panel.settings.ai_providers = [provider]

        chat_panel._rebuild_model_menu()

        actions = [action.text() for action in chat_panel.model_menu.actions()]
        assert "Add Provider..." in actions
        # The panel keeps strong references to submenus (PySide GC hazard).
        assert len(chat_panel._model_submenus) == 1
        submenu = chat_panel._model_submenus[0]
        sub_actions = [action.text() for action in submenu.actions()]
        assert "llama3" in sub_actions
        assert "mistral" in sub_actions

    def test_selecting_model_switches_active_provider(
        self, chat_panel: ChatPanel
    ) -> None:
        provider = new_provider("Local", "http://localhost:11434/v1", models=["llama3"])
        chat_panel.settings.ai_providers = [provider]

        chat_panel._select_model(provider["id"], "llama3")

        assert chat_panel.settings.ai_provider_id == provider["id"]
        assert chat_panel.settings.ai_model == "llama3"
        assert chat_panel.settings.ai_base_url == "http://localhost:11434/v1"

    def test_send_without_model_opens_settings_and_stops(
        self, chat_panel: ChatPanel
    ) -> None:
        """No model selected: send reports it and opens the AI settings."""
        chat_panel.settings.ai_model = ""
        chat_panel.input.setPlainText("hello")

        chat_panel._send()

        assert chat_panel._worker is None
        show_settings = cast(Any, chat_panel._show_settings_dialog)
        show_settings.assert_called_once_with("AI Assistant")

    def test_add_provider_with_accepted_dialog_returns_early(
        self, chat_panel: ChatPanel
    ) -> None:
        """An accepted-but-empty dialog result adds nothing (no crash)."""
        before = list(chat_panel.settings.ai_providers)
        chat_panel._add_provider()
        assert chat_panel.settings.ai_providers == before

    def test_switch_chat_keeps_model_selection(self, chat_panel: ChatPanel) -> None:
        """Opening a saved chat must not overwrite the chosen model."""
        first = new_provider("First", "https://a.example/v1", models=["ma"])
        second = new_provider("Second", "https://b.example/v1", models=["mb"])
        chat_panel.settings.ai_providers = [first, second]
        chat_panel._select_model(second["id"], "mb")

        # A chat saved earlier while a different model was active.
        session = chat_panel._store.new_session()
        session.title = "Old model"
        session.messages.append(ChatMessage(role="user", content="hi"))
        session.provider_id = first["id"]
        session.model = "ma"
        chat_panel._store.save(session)

        chat_panel._switch_chat(session.id)

        assert chat_panel.settings.ai_provider_id == second["id"]
        assert chat_panel.settings.ai_model == "mb"

    def test_model_selection_persists_across_new_chat(
        self, chat_panel: ChatPanel
    ) -> None:
        """New Chat keeps the active provider/model selection."""
        provider = new_provider("Local", "http://localhost:11434/v1", models=["llama3"])
        chat_panel.settings.ai_providers = [provider]
        chat_panel._select_model(provider["id"], "llama3")

        chat_panel._new_chat()

        assert chat_panel.settings.ai_provider_id == provider["id"]
        assert chat_panel.settings.ai_model == "llama3"


class TestChatPanelColors:
    def test_parse_hex_color(self) -> None:
        assert _parse_hex_color("#ffffff") == (255, 255, 255)
        assert _parse_hex_color("#fff") == (255, 255, 255)
        assert _parse_hex_color("19232d") == (0x19, 0x23, 0x2D)
        assert _parse_hex_color("rgba(1,2,3)") is None
        assert _parse_hex_color("nope") is None

    def test_mix_colors_and_luminance(self) -> None:
        assert _mix_colors("#000000", "#ffffff", 0.5) == "#808080"
        assert _mix_colors("#000000", "zzz", 0.5) == "#000000"
        assert _luminance("#000000") == 0.0
        assert _luminance("#ffffff") == pytest.approx(1.0)
        assert _luminance("garbage") == 1.0

    def test_fallback_colors_without_stylesheet(self, chat_panel: ChatPanel) -> None:
        """Without a theme stylesheet the panel falls back to sane colors."""
        background, foreground, accent = chat_panel._chat_colors()
        assert background == "#ffffff"
        assert foreground == "#000000"
        assert accent == "#346792"
        # Light fallback background -> assistant uses the readable text color.
        assert chat_panel._assistant_color() == "#000000"
        # Light background -> dark error red.
        assert chat_panel._error_color() == "#c01c28"
        assert chat_panel.history_view.objectName() == "ChatHistory"
        assert chat_panel.input.objectName() == "ChatInput"

    def test_dark_theme_colors_are_readable(self, chat_panel: ChatPanel) -> None:
        """With the RimPy QSS applied, chat text stays readable."""
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        # Simulate the app stylesheet containing the theme rules.
        app.setStyleSheet(
            "QTextBrowser#ChatHistory { background-color: #19232d;"
            " color: #e6edf3; }"
            "QPushButton#primaryButton { background-color: #346792; }"
        )
        try:
            background, foreground, accent = chat_panel._chat_colors()
            assert background == "#19232d"
            assert accent == "#346792"
            assistant = chat_panel._assistant_color()
            # Assistant replies are pure white on dark backgrounds.
            assert assistant == "#ffffff"
            assert _luminance(assistant) > _luminance(accent)
            assert chat_panel._error_color() == "#ff7b72"
            assert chat_panel._activity_color() != foreground
        finally:
            app.setStyleSheet("")

    def test_assistant_renders_markdown_in_white(self, chat_panel: ChatPanel) -> None:
        """Markdown from the model renders (bold/headings/lists) in white."""
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        app.setStyleSheet(
            "QTextBrowser#ChatHistory { background-color: #19232d; color: #e6edf3; }"
        )
        try:
            chat_panel._append_assistant(
                "**жирный** и `код`\n\n### Заголовок\n\n- пункт\n"
            )
            html = chat_panel.history_view.document().toHtml()
            assert "font-weight:700" in html.replace(" ", "")
            assert "Menlo" in html  # inline code rendered as monospace span
            assert "<h3" in html
            assert "<li" in html
            assert "#ffffff" in html
        finally:
            app.setStyleSheet("")

    def test_history_item_selection_uses_user_role(self, chat_panel: ChatPanel) -> None:
        """Sidebar items carry the chat id and the open chat is selected."""
        chat_id = _save_session_with_message(chat_panel, "Pick me", "x")
        chat_panel._switch_chat(chat_id)
        item = chat_panel.history_list.item(0)
        assert item.data(Qt.ItemDataRole.UserRole) == chat_id
        assert item.isSelected()


class TestChatPanelSessionData:
    def test_session_roundtrip_through_store(self, chat_panel: ChatPanel) -> None:
        """Messages roundtrip; provider/model stay global and are not stamped."""
        session: ChatSession = chat_panel._session
        session.messages.append(ChatMessage(role="user", content="hi"))
        chat_panel._save_session()

        loaded = chat_panel._store.load(session.id)
        assert loaded is not None
        assert loaded.provider_id == ""
        assert loaded.model == ""
        assert loaded.messages[0].content == "hi"
