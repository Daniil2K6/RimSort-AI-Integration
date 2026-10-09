"""Tests for the AI chat panel: layout, sessions, collapse, providers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication, QInputDialog

from app.ai.agent import CANCELLED_TEXT
from app.ai.chat_store import ChatMessage, ChatSession
from app.ai.providers import new_provider
from app.models.settings import Settings
from app.views.chat_panel import (
    ChatPanel,
    _luminance,
    _mix_colors,
    _parse_hex_color,
)
from app.views.chat_widgets import AssistantTurn, MessageBubble, ToolCallRow

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def chat_panel(
    qapp: QApplication | QCoreApplication,
    fresh_event_bus: None,
) -> Generator[ChatPanel, None, None]:
    """A ChatPanel with real Settings backed by the mocked AppInfo."""
    settings = Settings()
    # The mock AppInfo storage is session-wide: drop any providers another
    # test may have persisted so every panel starts from a clean slate.
    settings.ai_providers = []
    settings.ai_provider_id = ""
    settings.ai_model = ""
    settings.ai_base_url = ""
    settings.ai_api_key = ""
    panel = ChatPanel(
        settings=settings,
        show_settings_dialog=MagicMock(),
    )
    yield panel
    panel.shutdown()


def _configure_model(panel: ChatPanel) -> None:
    """Give the panel a usable localhost provider and model."""
    provider = new_provider("Local", "http://localhost:11434/v1", models=["test-model"])
    panel.settings.ai_providers = [provider]
    panel.settings.ai_provider_id = provider["id"]
    panel.settings.ai_model = "test-model"


def _save_session_with_message(panel: ChatPanel, title: str, text: str) -> str:
    session = panel._store.new_session()
    session.title = title
    session.messages.append(ChatMessage(role="user", content=text))
    panel._store.save(session)
    return session.id


class _FakeWorker:
    def __init__(self, **kwargs: Any) -> None:
        self.activity = MagicMock()
        self.stream_text = MagicMock()
        self.confirm_requested = MagicMock()
        self.finished_ok = MagicMock()
        self.failed = MagicMock()
        self.finished = MagicMock()

    def start(self) -> None:
        return None


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
        assert "payload" in chat_panel._chat_text()

    def test_new_chat_clears_view_and_keeps_saves(self, chat_panel: ChatPanel) -> None:
        chat_id = _save_session_with_message(chat_panel, "Old", "bye")
        chat_panel._switch_chat(chat_id)
        chat_panel._new_chat()

        assert chat_panel._session.is_empty
        assert chat_panel.title_label.text() == "New chat"
        assert "bye" not in chat_panel._chat_text()
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

        monkeypatch.setattr(chat_panel_module, "AgentWorker", _FakeWorker)
        _configure_model(chat_panel)
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
        assert chat_panel.chat_scroll.objectName() == "ChatHistory"
        assert chat_panel.input.objectName() == "ChatInput"

    def test_dark_theme_colors_are_readable(self, chat_panel: ChatPanel) -> None:
        """With the RimPy QSS applied, chat text stays readable."""
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        # Simulate the app stylesheet containing the theme rules.
        app.setStyleSheet(
            "QScrollArea#ChatHistory { background-color: #19232d;"
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
            "QScrollArea#ChatHistory { background-color: #19232d; color: #e6edf3; }"
        )
        try:
            chat_panel._append_assistant(
                "**жирный** и `код`\n\n### Заголовок\n\n- пункт\n"
            )
            turn = next(
                widget
                for widget in chat_panel._turn_widgets
                if isinstance(widget, AssistantTurn)
            )
            bubble = turn.bubble()
            assert bubble is not None
            html = bubble.rendered_html()
            assert "font-weight:700" in html.replace(" ", "")
            # Inline code renders as a monospace span; the concrete family
            # (Menlo/Consolas/DejaVu...) is platform-dependent.
            assert "font-family" in html
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


class TestChatPanelMessages:
    def _bubbles(self, chat_panel: ChatPanel) -> list[MessageBubble]:
        return [
            widget
            for widget in chat_panel._turn_widgets
            if isinstance(widget, MessageBubble)
        ]

    def test_user_and_assistant_bubbles_are_separate(
        self, chat_panel: ChatPanel
    ) -> None:
        """User is a right-aligned accent bubble; assistant is a left turn."""
        chat_panel._append_user("мой вопрос")
        chat_panel._append_assistant("ответ модели")

        assert len(chat_panel._turn_widgets) == 2
        user_bubble, assistant_turn = chat_panel._turn_widgets
        assert isinstance(user_bubble, MessageBubble)
        assert isinstance(assistant_turn, AssistantTurn)
        # Bubbles carry their role and their own colors.
        assert user_bubble.property("role") == "user"
        assert user_bubble.plain_text() == "мой вопрос"
        assert "ответ модели" in assistant_turn.plain_text()
        assistant_bubble = assistant_turn.bubble()
        assert assistant_bubble is not None
        assert user_bubble.background_color() != assistant_bubble.background_color()

    def test_activity_rows_attach_to_the_live_turn(self, chat_panel: ChatPanel) -> None:
        """Tool activity lines become rows on the live assistant turn."""
        chat_panel._worker = cast(Any, MagicMock())  # simulate a running agent
        chat_panel._append_activity("… list_mods()")
        chat_panel._append_activity("→ list_mods()")
        turn = chat_panel._live_turn
        assert turn is not None
        rows = [child for child in turn.findChildren(ToolCallRow)]
        assert len(rows) == 1  # pending + done merge into one row
        assert rows[0].plain_text().endswith("list_mods()")
        assert "→" in rows[0].plain_text()
        chat_panel._worker = None

    def test_error_gets_its_own_bubble(self, chat_panel: ChatPanel) -> None:
        chat_panel._append_error("что-то сломалось")

        bubble = self._bubbles(chat_panel)[0]
        assert bubble.property("role") == "error"
        assert bubble.plain_text() == "что-то сломалось"

    def test_bubbles_span_the_chat_width(
        self, chat_panel: ChatPanel, qapp: QApplication
    ) -> None:
        """Messages are full-column cards so long text always wraps."""
        chat_panel.resize(600, 400)
        chat_panel.show()
        qapp.processEvents()

        chat_panel._append_user("короткий вопрос")
        chat_panel._append_assistant("короткий ответ")
        qapp.processEvents()

        user_bubble, assistant_turn = chat_panel._turn_widgets
        assert isinstance(user_bubble, MessageBubble)
        assert isinstance(assistant_turn, AssistantTurn)
        assistant_bubble = assistant_turn.bubble()
        assert assistant_bubble is not None
        viewport = chat_panel.chat_scroll.viewport().width()
        assert viewport > 0
        assert user_bubble.width() > viewport * 0.8
        assert assistant_bubble.width() > viewport * 0.8

    def test_new_messages_are_inserted_before_the_stretch(
        self, chat_panel: ChatPanel
    ) -> None:
        """The trailing stretch stays last so messages stack from the top."""
        chat_panel._append_user("первое")
        chat_panel._append_assistant("второе")

        count = chat_panel._chat_layout.count()
        last_item = chat_panel._chat_layout.itemAt(count - 1)
        assert last_item is not None
        assert last_item.widget() is None  # the stretch spacer

    def test_clear_view_keeps_layout_and_stretch(self, chat_panel: ChatPanel) -> None:
        chat_panel._append_user("до очистки")
        chat_panel._clear_chat_view()

        assert chat_panel._turn_widgets == []
        assert chat_panel._chat_text() == ""
        assert chat_panel._chat_layout.count() == 1  # only the stretch


class TestChatPanelStreaming:
    def test_stream_deltas_render_as_markdown_reply(
        self, chat_panel: ChatPanel
    ) -> None:
        """Live deltas appear immediately; finish re-renders them as Markdown."""
        chat_panel._on_stream_text("сначала ")
        chat_panel._on_stream_text("plain, потом ")
        turn = chat_panel._live_turn
        assert turn is not None
        assert "сначала plain, потом" in turn.plain_text()

        chat_panel._on_stream_text("**жирный** финал")
        chat_panel._on_finished_ok("**жирный** финал")

        assert chat_panel._live_turn is None
        bubble = turn.bubble()
        assert bubble is not None
        html = bubble.rendered_html()
        assert "font-weight:700" in html.replace(" ", "")
        # The streamed text is what gets persisted.
        saved = chat_panel._session.messages[-1]
        assert saved.role == "assistant"
        assert saved.content.startswith("сначала plain")
        assert "**жирный** финал" in saved.content

    def test_stream_cancel_keeps_partial_and_skips_save(
        self, chat_panel: ChatPanel
    ) -> None:
        """Stopping mid-stream keeps the partial text and saves nothing."""
        chat_panel._on_stream_text("частичный ответ")
        turn = chat_panel._live_turn
        assert turn is not None
        chat_panel._on_finished_ok(CANCELLED_TEXT)

        assert "частичный ответ" in chat_panel._chat_text()
        assert all(m.role != "assistant" for m in chat_panel._session.messages)
        assert "Generation stopped." in chat_panel._chat_text()

    def test_stream_failure_keeps_partial_and_reports_error(
        self, chat_panel: ChatPanel
    ) -> None:
        """A mid-stream failure keeps the text and appends the error."""
        chat_panel._on_stream_text("до ошибки")
        chat_panel._on_failed("сбой связи")

        text = chat_panel._chat_text()
        assert "до ошибки" in text
        assert "сбой связи" in text
        assert chat_panel._live_turn is None


class TestChatPanelActions:
    def test_regenerate_drops_last_reply_and_restarts(
        self, chat_panel: ChatPanel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regenerate removes the trailing reply and reruns the agent."""
        from app.views import chat_panel as chat_panel_module

        monkeypatch.setattr(chat_panel_module, "AgentWorker", _FakeWorker)
        _configure_model(chat_panel)
        chat_panel._session.messages.append(ChatMessage(role="user", content="вопрос"))
        chat_panel._session.messages.append(
            ChatMessage(role="assistant", content="старый ответ")
        )
        chat_panel._render_session()

        chat_panel._regenerate()

        assert all(m.role != "assistant" for m in chat_panel._session.messages)
        assert "старый ответ" not in chat_panel._chat_text()
        assert "вопрос" in chat_panel._chat_text()
        assert chat_panel._worker is not None
        chat_panel._worker = None

    def test_edit_last_user_rewrites_and_resends(
        self, chat_panel: ChatPanel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Editing the last user message replaces it and sends again."""
        from app.views import chat_panel as chat_panel_module

        monkeypatch.setattr(chat_panel_module, "AgentWorker", _FakeWorker)
        _configure_model(chat_panel)
        chat_panel._session.messages.append(ChatMessage(role="user", content="старое"))
        chat_panel._render_session()
        monkeypatch.setattr(
            QInputDialog,
            "getText",
            lambda *args, **kwargs: ("новый вопрос", True),
        )

        chat_panel._edit_last_user()

        assert len(chat_panel._session.messages) == 1
        assert chat_panel._session.messages[0].content == "новый вопрос"
        assert "старое" not in chat_panel._chat_text()
        assert "новый вопрос" in chat_panel._chat_text()
        assert chat_panel._worker is not None
        chat_panel._worker = None

    def test_regenerate_button_enabled_only_after_reply(
        self, chat_panel: ChatPanel
    ) -> None:
        assert not chat_panel.regenerate_button.isEnabled()
        chat_panel._session.messages.append(
            ChatMessage(role="assistant", content="готово")
        )
        chat_panel._update_actions()
        assert chat_panel.regenerate_button.isEnabled()
        chat_panel._set_busy(True)
        assert not chat_panel.regenerate_button.isEnabled()


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
