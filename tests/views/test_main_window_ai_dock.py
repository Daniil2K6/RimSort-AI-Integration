"""Tests for AI chat dock visibility persistence in the main window."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.models.settings import Settings
from tests.views.conftest import make_stub_main_window


def _window_with_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    window = make_stub_main_window()
    window.settings = Settings()
    monkeypatch.setattr(window.settings, "save", MagicMock())
    return window


class TestAiDockVisibilityPersistence:
    def test_hidden_window_does_not_persist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Closing the app must not flip the saved dock visibility to False."""
        window = _window_with_settings(monkeypatch)
        monkeypatch.setattr(window, "isVisible", lambda: False)

        window._on_ai_chat_visibility_changed(False)

        assert window.settings.ai_chat_visible is True
        window.settings.save.assert_not_called()

    def test_minimized_window_does_not_persist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Minimizing the app is not a user toggle either."""
        window = _window_with_settings(monkeypatch)
        monkeypatch.setattr(window, "isVisible", lambda: True)
        monkeypatch.setattr(window, "isMinimized", lambda: True)

        window._on_ai_chat_visibility_changed(False)

        assert window.settings.ai_chat_visible is True
        window.settings.save.assert_not_called()

    def test_user_toggle_is_persisted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Toggling the dock while the window is normal is saved."""
        window = _window_with_settings(monkeypatch)
        monkeypatch.setattr(window, "isVisible", lambda: True)
        monkeypatch.setattr(window, "isMinimized", lambda: False)

        window._on_ai_chat_visibility_changed(False)

        assert window.settings.ai_chat_visible is False
        window.settings.save.assert_called_once()
