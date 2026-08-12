import asyncio
import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from whisky_tracker.models.context import ContextResolution, FulfillmentMode
from whisky_tracker.models.promotion import PromotionKind
from whisky_tracker.retailers.coto import CotoAdapter, CotoConfig, CotoSchemaError

FIXTURE = Path(__file__).parent / "fixtures" / "coto_search.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def run_search(handler, *, page_size: int = 24):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = CotoAdapter(
        client=client,
        client_uuid="00000000-0000-0000-0000-000000000001",
        config=CotoConfig(page_size=page_size, request_delay=0),
    )

    async def run():
        async with client:
            return await adapter.search_products("whisky")

    return asyncio.run(run())


def test_parses_branch_specific_product_and_promotions() -> None:
    products = run_search(lambda _request: httpx.Response(200, json=load_fixture()))

    assert len(products) == 3  # The product priced only at branch 060 is deliberately skipped.
    product = products[0]
    assert product.retailer == "Coto"
    assert product.retailer_product_id == "prod001"
    assert product.retailer_sku_id == "sku001"
    assert product.gtin == "7791234567890"
    assert product.current_price == Decimal("15000.00")
    assert product.regular_price == Decimal("20000")
    assert product.current_price != Decimal("12000")
    assert product.in_stock is True
    assert product.size_value == Decimal("750")
    assert product.size_unit == "ml"
    assert product.product_url == "https://www.coto.com.ar/productos/_/R-001-001-200"
    assert product.context.store_id == "200"
    assert product.context.store_name == "Coto Digital default"
    assert product.context.fulfillment_mode is FulfillmentMode.DIGITAL
    assert product.context.context_resolution is ContextResolution.GENERIC

    general, payment = product.promotions
    assert general.kind is PromotionKind.GENERAL
    assert general.applied_to_current_price is True
    assert general.discount_value == Decimal("25")
    assert "store=200" in general.conditions
    assert payment.kind is PromotionKind.PAYMENT_METHOD
    assert payment.applied_to_current_price is False
    assert "installments=3" in payment.conditions


def test_availability_is_for_active_branch_only() -> None:
    products = run_search(lambda _request: httpx.Response(200, json=load_fixture()))
    unavailable = next(product for product in products if product.retailer_product_id == "prod002")
    assert unavailable.in_stock is False


def test_member_discount_is_conditional_and_not_current_price() -> None:
    products = run_search(lambda _request: httpx.Response(200, json=load_fixture()))
    product = next(product for product in products if product.retailer_product_id == "prod004")
    assert product.current_price == Decimal("40000")
    assert product.promotions[0].kind is PromotionKind.LOYALTY
    assert product.promotions[0].applied_to_current_price is False
    assert product.product_url == "https://www.coto.com.ar/productos/_/R-004"


def test_paginates_one_based_and_stops_at_total() -> None:
    fixture = load_fixture()
    source = fixture["response"]["results"][:2]
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        page = int(query["page"][0])
        pages.append(page)
        start = (page - 1) * 2
        return httpx.Response(
            200,
            json={
                "response": {
                    "total_num_results": 3,
                    "results": [*source, fixture["response"]["results"][3]][start : start + 2],
                }
            },
        )

    products = run_search(handler, page_size=2)
    assert pages == [1, 2]
    assert len(products) == 3


@pytest.mark.parametrize(
    "payload",
    [[], {}, {"response": {}}, {"response": {"results": [], "total_num_results": "0"}}],
)
def test_rejects_malformed_response(payload) -> None:
    with pytest.raises(CotoSchemaError):
        run_search(lambda _request: httpx.Response(200, json=payload))


def test_empty_result_returns_empty_without_fallback() -> None:
    products = run_search(
        lambda _request: httpx.Response(
            200, json={"response": {"total_num_results": 0, "results": []}}
        )
    )
    assert products == []


def test_request_uses_branch_filter_and_stable_client_uuid() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(parse_qs(request.url.query.decode()))
        return httpx.Response(200, json={"response": {"total_num_results": 0, "results": []}})

    run_search(handler)
    assert seen[0]["us"] == ["200"]
    assert seen[0]["c"] == ["00000000-0000-0000-0000-000000000001"]
    assert '"value":"200"' in seen[0]["pre_filter_expression"][0]
