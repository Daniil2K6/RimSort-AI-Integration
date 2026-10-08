"""AI provider/model management for the built-in assistant.

Providers are stored as plain dicts in ``Settings.ai_providers`` so they
serialize straight into settings.json. The legacy ``ai_base_url`` /
``ai_api_key`` fields are kept in sync with the active provider for
backward compatibility with existing settings files and tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import requests
from loguru import logger

if TYPE_CHECKING:
    from app.models.settings import Settings

MODEL_FETCH_TIMEOUT: tuple[float, float] = (5.0, 20.0)


class ProviderError(Exception):
    """A user-facing provider configuration failure (network, bad response)."""


@dataclass(frozen=True)
class ProviderPreset:
    """Preset offered in the add-provider dialog."""

    label: str
    base_url: str
    needs_key: bool
    default_models: tuple[str, ...] = ()


PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        "OpenAI",
        "https://api.openai.com/v1",
        True,
        ("gpt-4o-mini", "gpt-4o"),
    ),
    ProviderPreset(
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        True,
        ("openrouter/free",),
    ),
    ProviderPreset(
        "Groq",
        "https://api.groq.com/openai/v1",
        True,
        ("llama-3.3-70b-versatile",),
    ),
    ProviderPreset(
        "DeepSeek",
        "https://api.deepseek.com/v1",
        True,
        ("deepseek-chat",),
    ),
    ProviderPreset("Ollama (local)", "http://localhost:11434/v1", False),
    ProviderPreset("LM Studio (local)", "http://localhost:1234/v1", False),
    ProviderPreset("llama.cpp (local)", "http://localhost:8080/v1", False),
    ProviderPreset("Custom", "", False),
)


def new_provider(
    name: str,
    base_url: str,
    api_key: str = "",
    models: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a provider dict with a fresh id."""
    return {
        "id": uuid.uuid4().hex,
        "name": name.strip(),
        "base_url": base_url.strip().rstrip("/"),
        "api_key": api_key.strip(),
        "models": list(models),
    }


def ensure_providers(settings: Settings) -> dict[str, Any] | None:
    """Make ``settings.ai_providers`` valid, seeding it from legacy fields.

    Returns the active provider, or ``None`` when nothing is configured.
    """
    providers = settings.ai_providers or []
    if not providers:
        base_url = settings.ai_base_url.strip()
        api_key = settings.ai_api_key.strip()
        model = settings.ai_model.strip()
        if base_url or api_key or model:
            seeded = new_provider(
                "Default",
                base_url,
                api_key,
                [model] if model else [],
            )
            providers = [seeded]
            settings.ai_providers = providers
    provider_ids = {str(provider.get("id", "")) for provider in providers}
    if settings.ai_provider_id not in provider_ids:
        settings.ai_provider_id = providers[0]["id"] if providers else ""
    return get_provider(settings)


def get_provider(
    settings: Settings, provider_id: str | None = None
) -> dict[str, Any] | None:
    """Return a provider by id, defaulting to the active one."""
    wanted = provider_id if provider_id is not None else settings.ai_provider_id
    for provider in settings.ai_providers:
        if provider.get("id") == wanted:
            return provider
    return settings.ai_providers[0] if settings.ai_providers else None


def active_provider(settings: Settings) -> dict[str, Any] | None:
    """Ensure providers are valid and return the active one."""
    return ensure_providers(settings)


def set_active(settings: Settings, provider_id: str, model: str) -> None:
    """Switch the active provider/model and mirror them into legacy fields."""
    provider = get_provider(settings, provider_id)
    if provider is None:
        logger.warning("AI providers: unknown provider id {}", provider_id)
        return
    settings.ai_provider_id = str(provider.get("id", ""))
    if model:
        settings.ai_model = model
    sync_legacy(settings)


def sync_legacy(settings: Settings) -> None:
    """Mirror the active provider into the legacy ``ai_*`` fields."""
    provider = get_provider(settings)
    if provider is None:
        return
    settings.ai_base_url = str(provider.get("base_url", ""))
    settings.ai_api_key = str(provider.get("api_key", ""))


def add_provider(settings: Settings, provider: dict[str, Any]) -> None:
    """Append a provider and make it active when nothing was selected."""
    settings.ai_providers = [*settings.ai_providers, provider]
    if settings.ai_provider_id in ("", None):
        set_active(settings, str(provider.get("id", "")), "")


def update_provider(
    settings: Settings, provider_id: str, fields: dict[str, Any]
) -> None:
    """Update one provider in place and refresh the legacy mirror."""
    updated = [
        {**provider, **fields} if provider.get("id") == provider_id else provider
        for provider in settings.ai_providers
    ]
    settings.ai_providers = updated
    if settings.ai_provider_id == provider_id:
        sync_legacy(settings)


def remove_provider(settings: Settings, provider_id: str) -> None:
    """Delete a provider, falling back to the first remaining one."""
    settings.ai_providers = [
        provider
        for provider in settings.ai_providers
        if provider.get("id") != provider_id
    ]
    if settings.ai_provider_id == provider_id:
        settings.ai_provider_id = (
            settings.ai_providers[0]["id"] if settings.ai_providers else ""
        )
    sync_legacy(settings)


GetFn = Callable[..., Any]


def fetch_models(
    base_url: str,
    api_key: str = "",
    get: GetFn | None = None,
) -> list[str]:
    """Fetch model ids from an OpenAI-compatible ``GET /models`` endpoint."""
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    request_get: GetFn = get or requests.get
    try:
        response = request_get(url, headers=headers, timeout=MODEL_FETCH_TIMEOUT)
    except requests.RequestException as exc:
        raise ProviderError(f"Could not reach the endpoint: {exc}") from exc
    status = getattr(response, "status_code", 0)
    if status != 200:
        raise ProviderError(f"Model list request failed (HTTP {status}).")
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError("The endpoint returned a non-JSON response") from exc
    if not isinstance(data, dict):
        raise ProviderError("The endpoint returned unexpected JSON")
    items = data.get("data")
    if not isinstance(items, list):
        raise ProviderError("The endpoint response has no model list")
    models = sorted(
        {
            str(item.get("id"))
            for item in items
            if isinstance(item, dict) and item.get("id")
        }
    )
    if not models:
        raise ProviderError("The endpoint returned an empty model list")
    return models
