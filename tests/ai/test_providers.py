"""Tests for AI provider management (app.ai.providers)."""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from app.ai.providers import (
    ProviderError,
    add_provider,
    ensure_providers,
    fetch_models,
    get_provider,
    new_provider,
    remove_provider,
    set_active,
    update_provider,
)
from app.models.settings import Settings


@pytest.fixture
def settings(
    qapp: QApplication | QCoreApplication,
    fresh_event_bus: None,
) -> Settings:
    """A real Settings instance backed by the mocked AppInfo storage."""
    return Settings()


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_ensure_providers_seeds_from_legacy_fields(settings: Settings) -> None:
    """Empty provider list is seeded from the legacy ai_* fields."""
    assert settings.ai_providers == []
    provider = ensure_providers(settings)
    assert provider is not None
    assert provider["base_url"] == "https://api.openai.com/v1"
    assert provider["models"] == ["gpt-4o-mini"]
    assert settings.ai_provider_id == provider["id"]
    # Idempotent: a second call does not create another provider.
    ensure_providers(settings)
    assert len(settings.ai_providers) == 1


def test_ensure_providers_keeps_existing(settings: Settings) -> None:
    """Existing providers survive and a bad active id is repaired."""
    provider = new_provider("Local", "http://localhost:11434/v1", models=["m"])
    settings.ai_providers = [provider]
    settings.ai_provider_id = "nonexistent"

    active = ensure_providers(settings)
    assert active is not None
    assert active["id"] == provider["id"]
    assert len(settings.ai_providers) == 1


def test_set_active_syncs_legacy_fields(settings: Settings) -> None:
    """Switching providers mirrors base URL and key into ai_* fields."""
    first = new_provider("First", "https://a.example/v1", "key-a", ["ma"])
    second = new_provider("Second", "http://localhost:11434/v1", "", ["mb"])
    settings.ai_providers = [first, second]

    set_active(settings, second["id"], "mb")

    assert settings.ai_provider_id == second["id"]
    assert settings.ai_model == "mb"
    assert settings.ai_base_url == "http://localhost:11434/v1"
    assert settings.ai_api_key == ""


def test_add_update_remove_provider(settings: Settings) -> None:
    """Add/update/remove keep the list and active selection consistent."""
    provider = new_provider("One", "https://a.example/v1", "k", ["m1"])
    settings.ai_providers = []
    add_provider(settings, provider)
    assert settings.ai_provider_id == provider["id"]

    update_provider(settings, provider["id"], {"name": "Renamed"})
    renamed = get_provider(settings, provider["id"])
    assert renamed is not None
    assert renamed["name"] == "Renamed"
    assert settings.ai_base_url == "https://a.example/v1"

    remove_provider(settings, provider["id"])
    assert settings.ai_providers == []
    assert settings.ai_provider_id == ""
    assert get_provider(settings) is None


def test_fetch_models_success() -> None:
    """Model ids are extracted from an OpenAI-style /models response."""
    calls: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        calls["url"] = url
        return _FakeResponse(
            200,
            {"data": [{"id": "b"}, {"id": "a"}, {"name": "ignored"}]},
        )

    models = fetch_models("https://api.example/v1/", "key", get=fake_get)
    assert models == ["a", "b"]
    assert calls["url"] == "https://api.example/v1/models"


def test_fetch_models_http_error() -> None:
    """Non-200 responses raise a user-facing ProviderError."""

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(401, {})

    with pytest.raises(ProviderError, match="HTTP 401"):
        fetch_models("https://api.example/v1", get=fake_get)


def test_fetch_models_bad_payload() -> None:
    """Malformed payloads raise ProviderError instead of crashing."""
    with pytest.raises(ProviderError):
        fetch_models(
            "https://api.example/v1",
            get=lambda url, **kwargs: _FakeResponse(200, {"nope": True}),
        )
    with pytest.raises(ProviderError):
        fetch_models(
            "https://api.example/v1",
            get=lambda url, **kwargs: _FakeResponse(200, ValueError("bad json")),
        )
