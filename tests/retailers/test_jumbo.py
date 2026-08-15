import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from whisky_tracker.display import display_retailer
from whisky_tracker.models.context import ContextResolution, FulfillmentMode, RetailerContext
from whisky_tracker.retailers.jumbo import JumboAdapter, JumboLocationError

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "vtex_products.json").read_text())
LOCATION = RetailerContext(
    fulfillment_mode=FulfillmentMode.DELIVERY,
    postal_code="1428",
    coordinates=(-58.0, -34.0),
)


def test_jumbo_remains_generic_only_when_explicitly_called_without_location() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(206, json=FIXTURE, headers={"resources": "0-0/1"})
        )
    )
    adapter = JumboAdapter(client=client)

    async def run() -> list:
        async with client:
            return await adapter.search_products("whisky")

    products = asyncio.run(run())
    assert products[0].context.context_resolution is ContextResolution.GENERIC
    assert products[0].context != LOCATION


def test_coordinates_resolve_fulfilled_seller_session_and_catalog_context() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/NT/search"):
            assert request.url.params["_where"] == "hasDelivery=true"
            assert request.url.params["isActive"] == "true"
            return httpx.Response(
                200,
                json=[
                    {"SellerName": "no-coverage", "name": "No Coverage", "storeid": "n"},
                    {"SellerName": "delivery-seller", "name": "Jumbo Norte", "storeid": "42"},
                ],
            )
        if request.url.path.endswith("/simulation"):
            body = json.loads(request.content)
            assert request.url.params["sc"] == "32"
            assert body["geoCoordinates"] == [-58.0, -34.0]
            slas = [] if body["items"][0]["seller"] == "no-coverage" else [{"name": "Envío"}]
            return httpx.Response(200, json={"logisticsInfo": [{"slas": slas}]})
        if request.url.path.endswith("/api/sessions"):
            body = json.loads(request.content)["public"]
            assert request.url.params["sc"] == "32"
            assert body["buyselectMethod"]["value"] == "delivery"
            assert body["selectedSeller"]["value"] == "delivery-seller"
            assert body["regionId"]["value"] == base64.b64encode(b"SW#delivery-seller").decode()
            return httpx.Response(
                200,
                json={"sessionToken": "session", "segmentToken": "segment"},
                headers=[
                    ("set-cookie", "vtex_session=session; Path=/"),
                    ("set-cookie", "vtex_segment=segment; Path=/"),
                ],
            )
        assert request.url.path.endswith("/products/search")
        assert request.url.params["sc"] == "32"
        assert "vtex_session=session" in request.headers.get("cookie", "")
        assert "vtex_segment=segment" in request.headers.get("cookie", "")
        return httpx.Response(206, json=FIXTURE, headers={"resources": "0-0/1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = JumboAdapter(client=client)

    async def run() -> list:
        async with client:
            return await adapter.search_products("whisky", context=LOCATION)

    products = asyncio.run(run())
    context = products[0].context
    assert context.fulfillment_mode is FulfillmentMode.DELIVERY
    assert context.context_resolution is ContextResolution.ADDRESS_RESOLVED
    assert context.sales_channel == "32"
    assert context.seller_id == "delivery-seller"
    assert context.store_id == "42"
    assert context.store_name == "Jumbo Norte"
    assert context.coordinates is None
    assert display_retailer("Jumbo", context) == "Jumbo"
    assert all(product.context == context for product in products)
    assert len(requests) == 5


def test_no_delivery_sla_fails_without_catalog_or_generic_fallback() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/NT/search"):
            return httpx.Response(200, json=[{"SellerName": "seller"}])
        return httpx.Response(200, json={"logisticsInfo": [{"slas": []}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = JumboAdapter(client=client)

    async def run() -> None:
        async with client:
            with pytest.raises(JumboLocationError, match="no valid delivery SLA"):
                await adapter.search_products("whisky", context=LOCATION)

    asyncio.run(run())
    assert not any(path.endswith("/products/search") for path in paths)


def test_resolved_and_generic_contexts_are_distinct() -> None:
    resolved = RetailerContext(
        fulfillment_mode=FulfillmentMode.DELIVERY,
        sales_channel="32",
        seller_id="seller",
        store_id="store",
        region_id="region",
        context_resolution=ContextResolution.ADDRESS_RESOLVED,
    )
    assert resolved != RetailerContext()
