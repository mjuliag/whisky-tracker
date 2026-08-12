import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from whisky_tracker.retailers.mercadolibre import (
    MercadoLibreAdapter,
    MercadoLibreAuth,
    MercadoLibreAuthorizationError,
    MercadoLibreConfig,
    MercadoLibreCredentialsError,
    MercadoLibreSchemaError,
    MercadoLibreTokenExpiredError,
)

ITEMS = json.loads((Path(__file__).parent / "fixtures" / "mercadolibre_items.json").read_text())


def run(adapter: MercadoLibreAdapter):
    async def search():
        return await adapter.search_products("whisky")

    return asyncio.run(search())


def test_authenticated_search_batches_enrichment_filters_and_paginates() -> None:
    search_offsets = []
    authorization = []
    search_rows = [
        {"id": "MLA1001", "title": "Whisky Escocés Ejemplo 750 ml"},
        {"id": "MLA9999", "title": "Set vasos para whisky"},
        {"id": "MLA1002", "title": "Pack x6 Whisky Bourbon 1 L"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        authorization.append(request.headers.get("Authorization"))
        if request.url.path == "/sites/MLA/search":
            query = parse_qs(request.url.query.decode())
            offset = int(query["offset"][0])
            search_offsets.append(offset)
            rows = search_rows[offset : offset + 2]
            return httpx.Response(
                200,
                json={"paging": {"total": 3, "limit": 2, "offset": offset}, "results": rows},
            )
        if request.url.path == "/items":
            ids = parse_qs(request.url.query.decode())["ids"][0].split(",")
            return httpx.Response(200, json=[row for row in ITEMS if row["body"]["id"] in ids])
        raise AssertionError(request.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MercadoLibreAdapter(
        auth=MercadoLibreAuth("access-token"),
        client=client,
        config=MercadoLibreConfig(page_size=2, batch_size=20, request_delay=0),
    )
    products = run(adapter)

    assert search_offsets == [0, 2]
    assert authorization and set(authorization) == {"Bearer access-token"}
    assert [product.retailer_product_id for product in products] == ["MLA1001", "MLA1002"]
    assert adapter.last_raw_result_count == 3
    assert adapter.last_filtered_count == 1


def test_missing_credentials_fails_before_request() -> None:
    adapter = MercadoLibreAdapter(auth=MercadoLibreAuth(None))
    with pytest.raises(MercadoLibreCredentialsError):
        run(adapter)


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, MercadoLibreTokenExpiredError), (403, MercadoLibreAuthorizationError)],
)
def test_authentication_errors_are_explicit(status, error) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(status))
    )
    adapter = MercadoLibreAdapter(auth=MercadoLibreAuth("bad-token"), client=client)
    with pytest.raises(error):
        run(adapter)


def test_401_refreshes_token_and_retries_request() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("Authorization")))
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "fresh-token", "refresh_token": "fresh-refresh"}
            )
        if request.headers.get("Authorization") == "Bearer old-token":
            return httpx.Response(401)
        return httpx.Response(200, json={"paging": {"total": 0, "limit": 25}, "results": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = MercadoLibreAuth("old-token", "refresh", "client", "secret")
    products = run(MercadoLibreAdapter(auth=auth, client=client))

    assert products == []
    assert auth.access_token == "fresh-token"
    assert auth.refresh_token == "fresh-refresh"
    assert seen[-1] == ("/sites/MLA/search", "Bearer fresh-token")


@pytest.mark.parametrize(
    "payload",
    [[], {}, {"results": []}, {"results": [], "paging": {"total": "0", "limit": 25}}],
)
def test_malformed_search_response(payload) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )
    adapter = MercadoLibreAdapter(auth=MercadoLibreAuth("token"), client=client)
    with pytest.raises(MercadoLibreSchemaError):
        run(adapter)
