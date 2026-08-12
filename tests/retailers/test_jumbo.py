"""Compatibility tests for Jumbo's intentionally generic mode."""

import asyncio
import json
from pathlib import Path

import httpx

from whisky_tracker.models.context import ContextResolution, FulfillmentMode
from whisky_tracker.retailers.jumbo import JumboAdapter


def test_jumbo_remains_generic_without_an_explicit_context() -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures" / "vtex_products.json").read_text())
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(206, json=fixture, headers={"resources": "0-0/1"})
        )
    )
    adapter = JumboAdapter(client=client)

    async def run() -> list:
        async with client:
            return await adapter.search_products("whisky")

    products = asyncio.run(run())
    assert products[0].context.context_resolution is ContextResolution.GENERIC
    assert products[0].context.fulfillment_mode is FulfillmentMode.GENERIC
    assert products[0].context.seller_id is None
