"""Tests for Carrefour postcode-aware VTEX retrieval."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from whisky_tracker.models.context import ContextResolution, FulfillmentMode, RetailerContext
from whisky_tracker.retailers.carrefour import CarrefourAdapter, CarrefourLocationError

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "vtex_products.json").read_text())


def requested_context() -> RetailerContext:
    return RetailerContext(
        fulfillment_mode=FulfillmentMode.SCHEDULED_DELIVERY,
        postal_code="1428",
        sales_channel="3",
    )


def test_resolves_postcode_establishes_session_and_propagates_context() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/regions"):
            assert request.url.params["postalCode"] == "1428"
            assert request.url.params["sc"] == "3"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "dynamic-region",
                        "sellers": [{"id": "dynamic-seller", "name": "Dynamic Store"}],
                    }
                ],
            )
        if request.url.path.endswith("/sessions"):
            assert request.url.params["sc"] == "3"
            return httpx.Response(
                201,
                json={"sessionToken": "session", "segmentToken": "segment"},
                headers=[
                    ("set-cookie", "vtex_session=session; Path=/"),
                    ("set-cookie", "vtex_segment=segment; Path=/"),
                ],
            )
        assert request.url.params["sc"] == "3"
        assert "vtex_session=session" in request.headers.get("cookie", "")
        assert "vtex_segment=segment" in request.headers.get("cookie", "")
        return httpx.Response(206, json=FIXTURE, headers={"resources": "0-0/1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = CarrefourAdapter(client=client)

    async def run() -> list:
        async with client:
            return await adapter.search_products("whisky", context=requested_context())

    products = asyncio.run(run())
    context = products[0].context
    assert context.context_resolution is ContextResolution.POSTCODE_RESOLVED
    assert context.region_id == "dynamic-region"
    assert context.seller_id == "dynamic-seller"
    assert context.store_name == "Dynamic Store"
    assert context.sales_channel == "3"
    assert all(product.context == context for product in products)
    assert len(requests) == 3


@pytest.mark.parametrize(
    ("region_payload", "message"),
    [([], "no delivery region"), ([{"id": "region", "sellers": []}], "no usable seller")],
)
def test_fails_when_postcode_has_no_usable_region(region_payload: object, message: str) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=region_payload))
    )
    adapter = CarrefourAdapter(client=client)

    async def run() -> None:
        with pytest.raises(CarrefourLocationError, match=message):
            async with client:
                await adapter.search_products("whisky", context=requested_context())

    asyncio.run(run())


@pytest.mark.parametrize(
    ("gtin", "title"),
    [
        ("5000299611104", "Estuche whisky importado Chivas extra 750 cc."),
        (
            "5099873017623",
            "Whisky importado Jack Daniels tennesse apple en botella 750 cc.",
        ),
    ],
)
def test_discards_carrefour_gtin_when_live_title_has_wrong_750_ml_volume(
    gtin: str, title: str
) -> None:
    product = asyncio.run(_generic_products())[0]
    conflicting = replace(
        product,
        gtin=gtin,
        title=title,
        size_value=None,
        size_unit=None,
    )

    sanitized = CarrefourAdapter._sanitize_product_identity(conflicting)

    assert sanitized.gtin is None
    assert sanitized.title == title


def test_retains_known_carrefour_gtin_when_volume_is_corrected_to_700_ml() -> None:
    product = asyncio.run(_generic_products())[0]
    corrected = replace(
        product,
        gtin="5099873017623",
        title="Whisky Jack Daniels Tennessee Apple 700 ml",
        size_value=None,
        size_unit=None,
    )

    assert CarrefourAdapter._sanitize_product_identity(corrected).gtin == "5099873017623"


async def _generic_products() -> list:
    response = httpx.Response(206, json=FIXTURE, headers={"resources": "0-0/1"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response))
    async with client:
        return await CarrefourAdapter(client=client).search_products("whisky")
