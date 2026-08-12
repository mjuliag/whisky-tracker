"""Tests for the shared VTEX retailer implementation."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from whisky_tracker.models.context import ContextResolution
from whisky_tracker.models.promotion import PromotionKind
from whisky_tracker.retailers.vtex import VtexAdapter, VtexSchemaError

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def make_adapter(handler: httpx.MockTransport) -> tuple[VtexAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=handler)
    adapter = VtexAdapter(
        retailer="test-retailer",
        endpoint="https://example.test/api/catalog_system/pub/products/search",
        client=client,
        page_size=2,
    )
    return adapter, client


def test_parses_multiple_skus_prices_promotions_and_stock() -> None:
    fixture = load_fixture("vtex_products.json")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(206, json=fixture, headers={"resources": "0-0/1"})
    )
    adapter, client = make_adapter(transport)

    async def run() -> list:
        async with client:
            return await adapter.search_products("whisky")

    products = asyncio.run(run())

    assert len(products) == 2
    first, second = products
    assert first.retailer_product_id == "product-1"
    assert first.retailer_sku_id == "sku-750"
    assert first.gtin == "7790000000001"
    assert first.current_price == Decimal("10000.25")
    assert first.regular_price == Decimal("12000")
    assert first.in_stock is True
    assert first.size_value == Decimal("750")
    assert first.size_unit == "ml"
    assert [promotion.name for promotion in first.promotions] == [
        "Oferta semanal",
        "Llevando 2 unidades",
    ]
    assert all(promotion.kind is PromotionKind.GENERAL for promotion in first.promotions)
    assert first.promotions[0].applied_to_current_price is True
    assert first.promotions[1].applied_to_current_price is False
    assert first.context.context_resolution is ContextResolution.GENERIC

    assert second.gtin is None
    assert second.current_price == Decimal("15000.90")
    assert second.regular_price == Decimal("9999999.99")
    assert second.in_stock is False
    assert second.size_value == Decimal("1")
    assert second.size_unit == "l"
    # A suspicious ListPrice is preserved; no discount is inferred from it.
    assert second.promotions == ()


def test_skips_sku_without_a_positive_offer() -> None:
    fixture = load_fixture("vtex_products.json")
    fixture[0]["items"][0]["sellers"] = [
        {
            "sellerId": "main",
            "commertialOffer": {
                "Price": 0,
                "ListPrice": 100,
                "AvailableQuantity": 0,
                "discountHighlights": [],
                "teasers": [],
            },
        }
    ]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(206, json=fixture, headers={"resources": "0-0/1"})
    )
    adapter, client = make_adapter(transport)

    async def run() -> list:
        async with client:
            return await adapter.search_products("whisky")

    products = asyncio.run(run())

    assert [product.retailer_sku_id for product in products] == ["sku-1l"]


def test_paginates_using_inclusive_ranges_and_resources_total() -> None:
    requests: list[httpx.Request] = []
    fixture = load_fixture("vtex_products.json")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        start = request.url.params["_from"]
        if start == "0":
            page = [fixture[0], {**fixture[0], "productId": "product-2"}]
            return httpx.Response(206, json=page, headers={"resources": "0-1/3"})
        return httpx.Response(
            206,
            json=[{**fixture[0], "productId": "product-3"}],
            headers={"resources": "2-2/3"},
        )

    adapter, client = make_adapter(httpx.MockTransport(handler))

    async def run() -> list:
        async with client:
            return await adapter.search_products("whisky")

    products = asyncio.run(run())

    assert len(products) == 6
    assert [(r.url.params["_from"], r.url.params["_to"]) for r in requests] == [
        ("0", "1"),
        ("2", "2"),
    ]


@pytest.mark.parametrize("payload", [{"products": []}, ["not-a-product"]])
def test_rejects_unexpected_top_level_shape(payload: object) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    adapter, client = make_adapter(transport)

    async def run() -> None:
        with pytest.raises(VtexSchemaError):
            async with client:
                await adapter.search_products("whisky")

    asyncio.run(run())


def test_rejects_malformed_product_shape() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            206,
            json=[{"productId": "broken", "productName": "Broken"}],
            headers={"resources": "0-0/1"},
        )
    )
    adapter, client = make_adapter(transport)

    async def run() -> None:
        with pytest.raises(VtexSchemaError, match="items array"):
            async with client:
                await adapter.search_products("whisky")

    asyncio.run(run())


def test_parses_and_validates_resources_header() -> None:
    assert VtexAdapter.parse_resources_header("0-49/142") == 142
    assert VtexAdapter.parse_resources_header(None) is None
    with pytest.raises(VtexSchemaError):
        VtexAdapter.parse_resources_header("not-a-range")
