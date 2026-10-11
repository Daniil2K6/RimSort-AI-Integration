"""Provider-independent, Russian diagnostics for AI connection failures.

Only explicit provider evidence identifies billing, model or region failures;
an HTTP status alone never proves that a country or a model is blocked.
"""

from __future__ import annotations

import json
import re
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import requests

_MAX_BODY = 16_384


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove credentials before diagnostics reach the GUI, logs or clipboard."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[скрыто]")
    text = re.sub(r"(?i)bearer\s+[^\s\"'<>]+", "Bearer [скрыто]", text)
    text = re.sub(r"\bsk-[\w-]+", "[скрыто]", text)
    text = re.sub(
        r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|authorization|password|secret)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,}\n]+",
        r"\1[скрыто]",
        text,
    )
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[скрыто]@", text)
    return text


@dataclass(frozen=True)
class AIErrorReport:
    code: str
    title: str
    explanation: str
    steps: tuple[str, ...]
    status: int | None = None
    provider_code: str = ""
    provider_message: str = ""
    endpoint: str = ""
    model: str = ""
    operation: str = ""
    retry_after: str = ""

    def render(self) -> str:
        """Plain text report, equally usable in a chat, dialog and clipboard."""
        parts = [self.title, self.explanation]
        if self.steps:
            parts.append(
                "Что можно сделать:\n"
                + "\n".join(f"{i}. {step}" for i, step in enumerate(self.steps, 1))
            )
        details = [f"Код ошибки: {self.code}"]
        for label, value in (
            ("HTTP", self.status),
            ("Код провайдера", self.provider_code),
            ("Сервер", self.endpoint),
            ("Модель", self.model),
            ("Операция", self.operation),
            ("Retry-After", self.retry_after),
            ("Ответ провайдера", self.provider_message),
        ):
            if value is not None and value != "":
                details.append(f"{label}: {value}")
        parts.append("Диагностика:\n" + "\n".join(details))
        return "\n\n".join(parts)


class AIError(Exception):
    """A user-facing failure; structured diagnostics are available when known."""

    def __init__(self, message: str | AIErrorReport) -> None:
        self.report = message if isinstance(message, AIErrorReport) else None
        super().__init__(self.report.render() if self.report is not None else message)


def _endpoint_host(endpoint: str) -> str:
    try:
        return urlsplit(endpoint).hostname or ""
    except ValueError:
        return "Некорректный URL"


def http_error_report(
    status: int,
    body: str,
    *,
    endpoint: str = "",
    model: str = "",
    operation: str = "Запрос к ИИ",
    secrets: tuple[str, ...] = (),
    headers: Any = None,
) -> AIErrorReport:
    """Classify structured JSON first, then conservative provider message hints."""
    code, message, limit_source = "", "", ""
    try:
        data = json.loads(body[:_MAX_BODY])
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        error = data.get("error", data)
        if isinstance(error, dict):
            code = " ".join(str(error[k]) for k in ("code", "type") if error.get(k))
            message = str(error.get("message") or "")
            metadata = error.get("metadata")
            if isinstance(metadata, dict):
                limit_source = str(metadata.get("limit_source") or "")
                # Some routers place the upstream error in metadata.raw.
                raw = metadata.get("raw")
                if not message and isinstance(raw, str):
                    message = raw[:1000]
        elif isinstance(error, str):
            message = error
    if not message and not re.search(
        r"<\s*(?:!doctype|html|body)", body, re.IGNORECASE
    ):
        message = body[:1000].strip()
    evidence = f"{code} {message}".lower()

    def contains(*hints: str) -> bool:
        return any(hint in evidence for hint in hints)

    settings = "Откройте настройки ИИ и проверьте адрес сервера и выбранную модель."
    steps: tuple[str, ...]
    if limit_source == "openrouter_in_flight_budget":
        kind, title, reason, steps = (
            "in_flight_budget",
            "Временно исчерпан бюджет параллельных запросов",
            "Провайдер зарезервировал доступный баланс для других выполняемых запросов.",
            ("Дождитесь завершения других запросов и повторите отправку.",),
        )
    elif contains(
        "unsupported_country",
        "country_not_supported",
        "region_not_supported",
        "unsupported region",
        "not available in your country",
        "not available in your region",
        "country, region",
        "country not supported",
        "region not supported",
        "geographic restriction",
        "geo blocked",
        "geoblocked",
    ):
        kind, title, reason, steps = (
            "region_restricted",
            "Провайдер сообщил о региональном ограничении",
            "Этот сервис или модель недоступны для региона запроса по правилам провайдера.",
            (
                "Проверьте список поддерживаемых регионов и условия доступа на сайте провайдера.",
                "Выберите провайдера, доступного в вашем регионе, или настроенную локальную модель.",
            ),
        )
    elif (
        contains(
            "insufficient_quota",
            "insufficient credits",
            "insufficient balance",
            "credit balance",
            "billing_hard_limit",
            "payment_required",
            "requires payment",
            "requires a paid plan",
        )
        or status == 402
    ):
        kind, title, reason, steps = (
            "billing",
            "Недостаточно средств или исчерпана квота API",
            "Провайдер отказал в запросе из-за баланса, платного доступа или лимита расходов аккаунта.",
            (
                "Проверьте баланс, квоту API и лимит расходов в кабинете провайдера.",
                "При необходимости пополните баланс или выберите доступную бесплатную/локальную модель.",
                "Подписка на чат-сервис не обязательно включает оплату его API.",
            ),
        )
    elif contains(
        "model_not_found",
        "invalid_model",
        "model not found",
        "model does not exist",
        "no endpoints found",
        "not a valid model",
        "invalid model",
        "invalid slug",
        "model is unavailable",
        "model is not available",
        "unavailable for free",
        "do not have access to the model",
    ) and not contains("support tool", "support function"):
        kind, title, reason, steps = (
            "model_unavailable",
            "Выбранная модель недоступна",
            "Провайдер не нашёл эту модель или не предоставляет к ней доступ этому аккаунту.",
            (
                "Обновите список моделей в настройках провайдера и выберите точный идентификатор.",
                "Проверьте права API-ключа и доступность модели в кабинете провайдера.",
            ),
        )
    elif contains(
        "context_length_exceeded", "maximum context length", "too many tokens"
    ):
        kind, title, reason, steps = (
            "context_limit",
            "Превышен размер контекста модели",
            "История и результаты инструментов превышают допустимый объём запроса.",
            (
                "Начните новый чат или сократите запрос и объём запрашиваемых данных.",
                "При необходимости выберите модель с большим контекстом.",
            ),
        )
    elif contains(
        "does not support tools",
        "does not support tool",
        "tool use is not supported",
        "no endpoints found that support tool",
        "tools are not supported",
    ):
        kind, title, reason, steps = (
            "tools_unsupported",
            "Модель не поддерживает вызов инструментов",
            "Для работы с MCP в чате нужен API с поддержкой function calling (tools).",
            ("Выберите у провайдера модель с поддержкой tools/function calling.",),
        )
    elif status == 401 or contains(
        "invalid_api_key", "invalid api key", "incorrect api key"
    ):
        kind, title, reason, steps = (
            "authentication",
            "API-ключ не принят",
            "Ключ отсутствует, неверен, отозван или относится к другому провайдеру.",
            (
                "Проверьте API-ключ и адрес сервера в настройках ИИ.",
                "Если ключ отозван, создайте новый в кабинете провайдера и замените его.",
            ),
        )
    elif status == 403:
        kind, title, reason, steps = (
            "access_denied",
            "Провайдер запретил доступ",
            "Ответ не позволяет однозначно определить причину: это могут быть права аккаунта, политика сервиса или ограничения доступа.",
            (
                "Проверьте права API-ключа, доступ к модели и сообщения в кабинете провайдера.",
                "Если причина не указана, передайте обезличенную диагностику поддержке провайдера.",
            ),
        )
    elif status == 429 or contains(
        "rate_limit", "rate limit", "too many requests", "too_many_requests"
    ):
        kind, title, reason, steps = (
            "rate_limit",
            "Достигнут лимит запросов",
            "Провайдер ограничил частоту запросов или доступную квоту. Этот статус сам по себе не означает, что модель платная.",
            (
                "Подождите и повторите запрос; учитывайте Retry-After, если он указан ниже.",
                "Проверьте лимиты аккаунта и время их сброса в кабинете провайдера.",
            ),
        )
    elif status == 404:
        kind, title, reason, steps = (
            "endpoint_not_found",
            "Адрес API или ресурс не найден",
            "Сервер вернул HTTP 404 без однозначного указания, что ошибка связана с моделью.",
            (
                settings,
                "Используйте базовый URL API провайдера, а не адрес его сайта или страницы чата.",
            ),
        )
    elif status in (408, 504):
        kind, title, reason, steps = (
            "timeout",
            "Сервер не ответил вовремя",
            "Время ожидания ответа истекло.",
            ("Проверьте соединение и повторите запрос позже.",),
        )
    elif status >= 500:
        kind, title, reason, steps = (
            "provider_unavailable",
            "Сервис ИИ временно недоступен",
            "На стороне провайдера или его шлюза произошла ошибка.",
            (
                "Проверьте страницу состояния сервиса и повторите запрос позже.",
                "Если сбой повторяется, выберите другую доступную модель или провайдера.",
            ),
        )
    else:
        kind, title, reason, steps = (
            "request_rejected",
            "Провайдер не смог выполнить запрос",
            "Проверьте диагностику: сервер отклонил формат, параметры или содержимое запроса.",
            (
                settings,
                "Если настройки верны, сохраните диагностику для отчёта об ошибке.",
            ),
        )
    host = _endpoint_host(endpoint)
    retry = str((headers or {}).get("Retry-After", ""))[:100]
    return AIErrorReport(
        kind,
        title,
        reason,
        steps,
        status or None,
        redact(code, secrets)[:100],
        redact(message, secrets)[:1000],
        host,
        redact(model, secrets)[:200],
        operation,
        redact(retry, secrets),
    )


def network_error_report(
    exc: BaseException,
    *,
    endpoint: str = "",
    model: str = "",
    secrets: tuple[str, ...] = (),
) -> AIErrorReport:
    """Map requests and aiohttp failures consistently without guessing location."""
    steps: tuple[str, ...]
    if isinstance(
        exc,
        (
            requests.exceptions.SSLError,
            ssl.SSLError,
            aiohttp.ClientSSLError,
            aiohttp.ClientConnectorCertificateError,
        ),
    ):
        code, title, reason, steps = (
            "tls",
            "Не удалось проверить защищённое соединение",
            "Проверка TLS-сертификата сервера завершилась ошибкой.",
            (
                "Проверьте дату и время компьютера, адрес сервера и доверенные сертификаты.",
                "Проверьте настройки прокси, если он используется. Не отключайте проверку сертификатов.",
            ),
        )
    elif isinstance(exc, (TimeoutError, requests.exceptions.Timeout)):
        code, title, reason, steps = (
            "timeout",
            "ИИ не ответил вовремя",
            "Истекло время ожидания подключения или ответа.",
            ("Проверьте соединение и доступность сервера, затем повторите запрос.",),
        )
    else:
        code, title, reason, steps = (
            "connection",
            "Не удалось подключиться к ИИ",
            "Соединение с сервером не установлено или было прервано. Это не доказывает региональную блокировку.",
            (
                "Проверьте интернет, базовый URL API и настройки прокси/межсетевого экрана.",
                "Для локальной модели запустите сервер Ollama/LM Studio и проверьте его порт.",
            ),
        )
    return AIErrorReport(
        code,
        title,
        reason,
        steps,
        endpoint=_endpoint_host(endpoint),
        model=redact(model, secrets)[:200],
        provider_message=redact(str(exc), secrets)[:1000],
    )
