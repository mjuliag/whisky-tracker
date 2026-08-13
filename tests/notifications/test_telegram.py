import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from whisky_tracker.models.context import ContextResolution, FulfillmentMode, RetailerContext
from whisky_tracker.models.product import ProductObservation
from whisky_tracker.models.promotion import Promotion, PromotionKind
from whisky_tracker.notifications.telegram import (
    TelegramAuthorizationError,
    TelegramChatError,
    TelegramConfig,
    TelegramConfigurationError,
    TelegramNotifier,
    TelegramRateLimitError,
    TelegramResponseError,
    format_product_observation,
)


def observation(
    *, promotions: tuple[Promotion, ...] = (), context: RetailerContext | None = None
) -> ProductObservation:
    return ProductObservation(
        retailer="Carrefour",
        retailer_product_id="product-1",
        retailer_sku_id="sku-1",
        catalog_product_id=None,
        gtin="7790895000997",
        title="Johnnie Walker Black Label 750 ml",
        brand="Johnnie Walker",
        size_value=Decimal("750"),
        size_unit="ml",
        pack_count=1,
        currency="ARS",
        current_price=Decimal("39999"),
        regular_price=Decimal("45000"),
        promotions=promotions,
        condition=None,
        available_quantity=3,
        in_stock=True,
        product_url="https://example.test/whisky",
        context=context or RetailerContext(),
        observed_at=datetime.now(UTC),
    )


def run(coroutine):
    return asyncio.run(coroutine)


def test_send_message_uses_official_request_shape() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(config=TelegramConfig("fake-token", "123456"), client=client)
    message_id = run(notifier.send_message("hello"))

    assert message_id == 42
    assert seen["path"] == "/botfake-token/sendMessage"
    assert seen["form"] == {
        "chat_id": ["123456"],
        "text": ["hello"],
        "disable_web_page_preview": ["true"],
    }


def test_send_message_supports_explicit_html_parse_mode() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(config=TelegramConfig("fake-token", "123"), client=client)
    run(notifier.send_message("<b>Safe</b>", parse_mode="HTML"))
    assert seen["form"]["parse_mode"] == ["HTML"]


def test_formats_located_carrefour_product_with_applied_promotion() -> None:
    promotion = Promotion(
        name="25% Mi Carrefour",
        kind=PromotionKind.LOYALTY,
        applied_to_current_price=True,
    )
    context = RetailerContext(
        fulfillment_mode=FulfillmentMode.SCHEDULED_DELIVERY,
        postal_code="1428",
        sales_channel="3",
        region_id="region",
        seller_id="seller",
        store_name="Market Juramento",
        context_resolution=ContextResolution.POSTCODE_RESOLVED,
    )
    message = format_product_observation(observation(promotions=(promotion,), context=context))

    assert "🥃 Whisky Tracker" in message
    assert "Johnnie Walker Black Label 750 ml" in message
    assert "Carrefour — Market Juramento" in message
    assert "Precio: $39.999" in message
    assert "Stock: disponible" in message
    assert "🔥 Promo: 25% Mi Carrefour" in message
    assert message.endswith("https://example.test/whisky")


def test_formats_generic_context_honestly_without_promotion() -> None:
    message = format_product_observation(observation())
    assert "contexto genérico (sin ubicación resuelta)" in message
    assert "Promo" not in message


@pytest.mark.parametrize(
    ("config", "operation"),
    [
        (TelegramConfig(None, "1"), lambda notifier: notifier.send_message("hello")),
        (TelegramConfig("token", None), lambda notifier: notifier.send_message("hello")),
    ],
)
def test_missing_configuration(config, operation) -> None:
    notifier = TelegramNotifier(config=config)
    with pytest.raises(TelegramConfigurationError):
        run(operation(notifier))


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (401, {"ok": False, "description": "Unauthorized"}, TelegramAuthorizationError),
        (
            400,
            {"ok": False, "description": "Bad Request: chat not found"},
            TelegramChatError,
        ),
        (
            429,
            {"ok": False, "parameters": {"retry_after": 1}},
            TelegramRateLimitError,
        ),
    ],
)
def test_api_errors_are_explicit(status, payload, error) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=payload))
    )
    notifier = TelegramNotifier(config=TelegramConfig("token", "1"), client=client, max_attempts=1)
    with pytest.raises(error):
        run(notifier.send_message("hello"))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"ok": False}),
        httpx.Response(200, json={"ok": True, "result": {}}),
    ],
)
def test_malformed_or_unsuccessful_response(response) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: response))
    notifier = TelegramNotifier(config=TelegramConfig("token", "1"), client=client)
    with pytest.raises(TelegramResponseError):
        run(notifier.send_message("hello"))


def test_get_updates_returns_only_valid_update_objects() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"ok": True, "result": [{"update_id": 1, "message": {}}]}
            )
        )
    )
    notifier = TelegramNotifier(config=TelegramConfig("token"), client=client)
    assert run(notifier.get_updates())[0]["update_id"] == 1
