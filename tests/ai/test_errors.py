"""Diagnostics distinguish provider evidence from ambiguous status codes."""

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import requests

from app.ai.client import AIClient, AIError, StreamFn
from app.ai.errors import http_error_report, network_error_report, redact
from app.ai.providers import ProviderError, fetch_models


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (401, {"message": "Invalid API key"}, "authentication"),
        (403, {"message": "Forbidden"}, "access_denied"),
        (403, {"code": "unsupported_country_region_territory"}, "region_restricted"),
        (404, {"message": "Not found"}, "endpoint_not_found"),
        (
            404,
            {"code": "model_not_found", "message": "Unknown id"},
            "model_unavailable",
        ),
        (
            404,
            {"message": "No endpoints found that support tool use"},
            "tools_unsupported",
        ),
        (402, {"message": "Insufficient credits"}, "billing"),
        (429, {"code": 429, "type": "insufficient_quota"}, "billing"),
        (429, {"message": "Too many requests"}, "rate_limit"),
        (200, {"message": "Rate limit exceeded"}, "rate_limit"),
        (400, {"code": "context_length_exceeded"}, "context_limit"),
        (503, {"message": "Upstream failed"}, "provider_unavailable"),
        (
            402,
            {"metadata": {"limit_source": "openrouter_in_flight_budget"}},
            "in_flight_budget",
        ),
    ],
)
def test_error_classification(
    status: int, error: dict[str, Any], expected: str
) -> None:
    report = http_error_report(status, json.dumps({"error": error}))
    assert report.code == expected
    assert "Что можно сделать:" in report.render()


def test_report_redacts_secret_and_url_credentials() -> None:
    secret = "custom-provider-key"
    report = http_error_report(
        401,
        json.dumps({"error": {"message": f"Key {secret}; Bearer sk-real-token"}}),
        endpoint=f"https://user:password@example.test/v1?api_key={secret}",
        model="test-model",
        secrets=(secret,),
    ).render()
    assert secret not in report
    assert "sk-real-token" not in report
    assert "password" not in report
    assert "example.test" in report
    assert "test-model" in report
    assert "[скрыто]" in report
    assert "my-token" not in redact('access_token="my-token"')


def test_unknown_html_is_not_shown_or_diagnosed_as_region_block() -> None:
    report = http_error_report(403, "<html><body>Cloud page</body></html>")
    assert report.code == "access_denied"
    assert not report.provider_message


def test_retry_after_and_model_are_in_report() -> None:
    report = http_error_report(429, "", model="model-id", headers={"Retry-After": "30"})
    assert report.retry_after == "30"
    assert "Retry-After: 30" in report.render()
    assert "Модель: model-id" in report.render()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (requests.Timeout("slow"), "timeout"),
        (TimeoutError(), "timeout"),
        (requests.exceptions.SSLError("certificate"), "tls"),
        (requests.ConnectionError("refused"), "connection"),
    ],
)
def test_network_failures(exc: BaseException, expected: str) -> None:
    assert network_error_report(exc).code == expected


def test_stream_error_after_http_success_is_not_an_empty_success() -> None:
    def factory(stop: Callable[[], bool]) -> StreamFn:
        async def stream(
            url: str,
            headers: dict[str, str],
            payload: dict[str, Any],
            timeout: tuple[float, float],
        ) -> AsyncIterator[bytes]:
            yield b'data: {"error":{"code":"model_not_found","message":"Unknown model"}}\n\n'

        return stream

    client = AIClient(
        "https://example.test/v1", "private-key", "unknown", stream_factory=factory
    )
    with pytest.raises(AIError, match="Выбранная модель недоступна") as error:
        client.chat([], on_text=lambda text: None)
    assert error.value.report is not None
    assert error.value.report.model == "unknown"


def test_model_fetch_uses_same_russian_diagnostics() -> None:
    class Response:
        status_code = 403
        text = '{"error":{"code":"unsupported_country_region_territory"}}'

    with pytest.raises(ProviderError, match="региональном ограничении") as error:
        fetch_models("https://example.test/v1", get=lambda *args, **kwargs: Response())
    assert error.value.report is not None
    assert error.value.report.operation == "Получение списка моделей"


def test_truncated_stream_never_returns_a_ready_tool_call() -> None:
    def factory(stop: Callable[[], bool]) -> StreamFn:
        async def stream(
            url: str,
            headers: dict[str, str],
            payload: dict[str, Any],
            timeout: tuple[float, float],
        ) -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"delete_mod","arguments":"{}"}}]}}]}\n\n'

        return stream

    client = AIClient("https://example.test/v1", "", "test", stream_factory=factory)
    with pytest.raises(AIError, match="прервал поток"):
        client.chat([], on_text=lambda text: None)
