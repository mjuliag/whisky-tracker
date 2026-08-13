"""Minimal async client for the official Telegram Bot API."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from whisky_tracker.models.context import ContextResolution
from whisky_tracker.models.product import ProductObservation

_TRANSIENT_STATUSES = {500, 502, 503, 504}


class TelegramError(RuntimeError):
    """Base error raised by Telegram notification operations."""


class TelegramConfigurationError(TelegramError):
    """Required local Telegram configuration is missing."""


class TelegramAuthorizationError(TelegramError):
    """Telegram rejected the configured bot token."""


class TelegramChatError(TelegramError):
    """Telegram rejected the configured chat identifier."""


class TelegramRateLimitError(TelegramError):
    """Telegram continued rate limiting after bounded retries."""


class TelegramResponseError(TelegramError):
    """Telegram returned an unsuccessful or malformed API response."""


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Explicit Bot API configuration supplied by the application layer."""

    bot_token: str | None
    chat_id: str | int | None = None
    api_base_url: str = "https://api.telegram.org"

    def require_token(self) -> str:
        if not self.bot_token or not self.bot_token.strip():
            raise TelegramConfigurationError("Telegram bot token is required")
        return self.bot_token.strip()

    def require_chat_id(self) -> str:
        if self.chat_id is None or not str(self.chat_id).strip():
            raise TelegramConfigurationError("Telegram chat ID is required")
        return str(self.chat_id).strip()


class TelegramNotifier:
    """Send messages and inspect updates through Telegram's official Bot API."""

    def __init__(
        self,
        *,
        config: TelegramConfig,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.config = config
        self.max_attempts = max_attempts
        self._client = client
        self._owns_client = client is None
        self._timeout = httpx.Timeout(timeout)

    async def __aenter__(self) -> TelegramNotifier:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send_message(
        self,
        text: str,
        *,
        chat_id: str | int | None = None,
        parse_mode: str | None = None,
    ) -> int:
        """Send one plain-text message and return Telegram's numeric message ID."""
        if not text.strip():
            raise ValueError("Telegram message text must not be blank")
        destination = str(chat_id).strip() if chat_id is not None else self.config.require_chat_id()
        data: dict[str, Any] = {
            "chat_id": destination,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        payload = await self._call("sendMessage", data=data)
        result = payload.get("result")
        if not isinstance(result, Mapping) or not isinstance(result.get("message_id"), int):
            raise TelegramResponseError("Telegram sendMessage response has no message ID")
        return result["message_id"]

    async def get_updates(self) -> list[Mapping[str, Any]]:
        """Return pending updates for local chat-ID discovery without persisting them."""
        payload = await self._call("getUpdates", data={"limit": 20, "timeout": 0})
        result = payload.get("result")
        if not isinstance(result, list) or not all(
            isinstance(update, Mapping) for update in result
        ):
            raise TelegramResponseError("Telegram getUpdates response has invalid results")
        return result

    async def _call(self, method: str, *, data: Mapping[str, Any]) -> Mapping[str, Any]:
        token = self.config.require_token()
        url = f"{self.config.api_base_url}/bot{token}/{method}"
        for attempt in range(self.max_attempts):
            try:
                response = await self._get_client().post(url, data=data)
            except httpx.TimeoutException, httpx.NetworkError:
                if attempt == self.max_attempts - 1:
                    # Do not chain transport errors: they may contain the token-bearing URL.
                    raise TelegramError("Telegram Bot API request failed") from None
                await asyncio.sleep(0.25 * (2**attempt))
                continue

            payload = self._response_payload(response)
            if response.status_code == 401:
                raise TelegramAuthorizationError("Telegram rejected the bot token")
            if response.status_code == 429:
                if attempt == self.max_attempts - 1:
                    raise TelegramRateLimitError("Telegram rate limit persisted after retries")
                retry_after = self._retry_after(payload)
                await asyncio.sleep(min(retry_after, 2.0))
                continue
            if response.status_code in _TRANSIENT_STATUSES:
                if attempt == self.max_attempts - 1:
                    raise TelegramError(
                        f"Telegram Bot API returned HTTP {response.status_code} after retries"
                    )
                await asyncio.sleep(0.25 * (2**attempt))
                continue
            if response.status_code == 400 and self._looks_like_chat_error(payload):
                raise TelegramChatError("Telegram rejected the chat ID")
            if response.status_code >= 400:
                raise TelegramResponseError(
                    f"Telegram Bot API returned HTTP {response.status_code}"
                )
            if payload.get("ok") is not True:
                raise TelegramResponseError("Telegram Bot API reported an unsuccessful operation")
            return payload
        raise AssertionError("retry loop exited unexpectedly")

    @staticmethod
    def _response_payload(response: httpx.Response) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramResponseError("Telegram Bot API returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise TelegramResponseError("Telegram Bot API response must be an object")
        return payload

    @staticmethod
    def _retry_after(payload: Mapping[str, Any]) -> float:
        parameters = payload.get("parameters")
        if isinstance(parameters, Mapping):
            value = parameters.get("retry_after")
            if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
                return float(value)
        return 0.5

    @staticmethod
    def _looks_like_chat_error(payload: Mapping[str, Any]) -> bool:
        description = str(payload.get("description", "")).casefold()
        return "chat" in description and any(
            marker in description for marker in ("not found", "invalid", "identifier")
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client


def format_product_observation(observation: ProductObservation) -> str:
    """Format a truthful, compact Telegram message for one observation."""
    context = observation.context
    if context.store_name:
        context_text = context.store_name
    elif context.context_resolution is ContextResolution.GENERIC:
        context_text = "contexto genérico (sin ubicación resuelta)"
    else:
        context_text = "ubicación resuelta"

    lines = [
        "🥃 Whisky Tracker",
        "",
        observation.title,
        f"{observation.retailer} — {context_text}",
        "",
        f"Precio: {_format_money(observation.current_price, observation.currency)}",
        f"Stock: {'disponible' if observation.in_stock else 'no disponible'}",
    ]
    for promotion in observation.promotions:
        prefix = "🔥 Promo" if promotion.applied_to_current_price else "🏷️ Promo condicional"
        lines.append(f"{prefix}: {promotion.name}")
    lines.extend(("", observation.product_url))
    return "\n".join(lines)


def _format_money(value: Decimal, currency: str) -> str:
    amount = f"{value:,.2f}"
    whole, decimal = amount.split(".")
    localized = whole.replace(",", ".")
    if decimal != "00":
        localized = f"{localized},{decimal}"
    symbol = "$" if currency.upper() == "ARS" else f"{currency.upper()} "
    return f"{symbol}{localized}"
