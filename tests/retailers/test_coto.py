import asyncio
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from whisky_tracker.alerts import AlertEngine, format_alert
from whisky_tracker.display import display_retailer
from whisky_tracker.matching import CanonicalProduct
from whisky_tracker.models.context import ContextResolution, FulfillmentMode, RetailerContext
from whisky_tracker.models.promotion import PromotionKind
from whisky_tracker.persistence import SQLiteRepository
from whisky_tracker.retailers.coto import (
    CotoAdapter,
    CotoConfig,
    CotoContextError,
    CotoSchemaError,
)

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


def test_discount_price_reference_keeps_promotions_on_their_exact_listing() -> None:
    fixture = load_fixture()
    small = fixture["response"]["results"][0]
    small["data"]["discounts"].append(
        {
            "id": "discount-for-other-volume",
            "comments": "No acumulable con otras promos",
            "takingText": "Precio regular: $30.000\nNo acumulable con otras promociones",
            "discountText": "25%Dto",
            "discountPrice": "$22500",
            "regularPriceText": "Precio Contado: $30000",
        }
    )
    products = run_search(lambda _request: httpx.Response(200, json=fixture))
    selected = products[0]

    assert selected.regular_price == Decimal("20000")
    assert selected.current_price == Decimal("15000")
    assert len(selected.promotions) == 2  # Its own discount and payment method.
    assert all(
        "30000" not in condition for promo in selected.promotions for condition in promo.conditions
    )


def test_black_label_750_alert_excludes_one_liter_promotion_but_keeps_own_distinct_promos(
    tmp_path: Path,
) -> None:
    fixture = load_fixture()
    small = fixture["response"]["results"][0]
    large = deepcopy(small)
    small["data"].update(
        id="black-750",
        sku_id="sku-black-750",
        sku_display_name="Whisky Johnnie Walker 12 Años 750 Ml Black Label",
        product_brand="Johnnie Walker",
        product_main_ean="7790895000997",
        price=[{"store": "200", "listPrice": 79243}],
        discounts=[
            {
                "discountText": "25%Dto",
                "discountPrice": "$59432.25",
                "comments": "No acumulable con otras promos",
                "takingText": "Precio regular: $94.721\nNo acumulable con otras promociones",
                "regularPriceText": "Precio Contado: $79243",
            },
            {
                "discountText": "10% Comunidad Coto",
                "comments": "Sólo socios",
                "regularPriceText": "Precio Contado: $79.243",
            },
            {
                "discountText": "25%Dto",
                "discountPrice": "$71040.75",
                "comments": "No acumulable con otras promos",
                "takingText": "Precio regular: $94.721\nNo acumulable con otras promociones",
                "regularPriceText": "Precio Contado: $94721",
            },
        ],
        discounts_payment_methods=[],
    )
    large["data"].update(
        id="black-1000",
        sku_id="sku-black-1000",
        sku_display_name="Whisky Johnnie Walker 12 Años 1 L Black Label",
        product_brand="Johnnie Walker",
        product_main_ean="5000267107776",
        price=[{"store": "200", "listPrice": 94721}],
        discounts=[small["data"]["discounts"][2]],
        discounts_payment_methods=[],
    )
    fixture["response"].update(total_num_results=2, results=[small, large])
    selected, other = run_search(lambda _request: httpx.Response(200, json=fixture))
    assert selected.current_price == Decimal("59432.25")
    assert selected.regular_price == Decimal("79243")
    assert other.regular_price == Decimal("94721")
    assert len(selected.promotions) == 2
    assert len(other.promotions) == 1
    assert {promo.kind for promo in selected.promotions} == {
        PromotionKind.GENERAL,
        PromotionKind.LOYALTY,
    }

    canonical = CanonicalProduct(
        "black-label-750",
        "johnnie walker",
        "black label",
        12,
        750,
        1,
        frozenset((selected.gtin,)),
    )
    with SQLiteRepository(tmp_path / "coto-alert.db") as repository:
        repository.initialize()
        repository.save_observations((selected, other))
        alert = AlertEngine(repository).evaluate_product(canonical, (selected, other))
        assert alert is not None
        assert alert.offers[0].observation is selected
        assert len(alert.offers[0].qualifying_promotions) == 2
        message = format_alert(alert)

    assert "<b>Johnnie Walker Black Label 12 Years 750 ml</b>" in message
    assert "Coto: $59.432,25" in message
    assert "🥃 25% de descuento" in message
    assert "🥃 10% de descuento con programa de fidelidad" in message
    assert "Precio regular: $79.243" in message
    assert "Sólo socios" in message
    assert "Precio regular: $94.721" not in message
    assert "71040" not in message


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


def test_gentleman_jack_product_url_preserves_exact_coto_identity() -> None:
    adapter = CotoAdapter()
    assert adapter._product_url("/productos/_/R-00602091-00602091-200") == (
        "https://www.coto.com.ar/productos/_/R-00602091-00602091-200"
    )


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


def test_coordinates_resolve_branch_and_use_only_its_price_and_availability() -> None:
    seen_search = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getCobertura"):
            assert request.url.params["lat"] == "-34.0"
            assert request.url.params["lng"] == "-58.0"
            return httpx.Response(
                200,
                json={
                    "sucursal": {
                        "sucursal": "200",
                        "nombre": "Coto Digital",
                        "mensajeError": "-",
                    }
                },
            )
        query = parse_qs(request.url.query.decode())
        seen_search.append(query)
        return httpx.Response(200, json=load_fixture())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = CotoAdapter(
        client=client,
        client_uuid="00000000-0000-0000-0000-000000000001",
        config=CotoConfig(request_delay=0),
    )
    requested = RetailerContext(
        fulfillment_mode=FulfillmentMode.DELIVERY,
        postal_code="1428",
        coordinates=(-58.0, -34.0),
    )

    async def run():
        async with client:
            return await adapter.search_products("whisky", context=requested)

    products = asyncio.run(run())
    product = products[0]
    assert product.current_price == Decimal("15000.00")
    assert product.current_price != Decimal("12000")  # Cheaper branch 060 is not substituted.
    assert product.in_stock is True
    assert product.context.store_id == "200"
    assert product.context.store_name == "Coto Digital"
    assert product.context.context_resolution is ContextResolution.ADDRESS_RESOLVED
    assert product.context.fulfillment_mode is FulfillmentMode.DELIVERY
    assert product.context.coordinates is None
    assert product.context != adapter.default_context()
    assert display_retailer("Coto", product.context) == "Coto"
    assert seen_search[0]["us"] == ["200"]
    assert '"value":"200"' in seen_search[0]["pre_filter_expression"][0]
    assert all(item.context == product.context for item in products)


def test_coverage_failure_has_no_branch_200_or_search_fallback() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={"sucursal": {"mensajeError": "Fuera de cobertura"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = CotoAdapter(client=client, config=CotoConfig(request_delay=0))
    requested = RetailerContext(
        fulfillment_mode=FulfillmentMode.DELIVERY,
        coordinates=(-58.0, -34.0),
    )

    async def run() -> None:
        async with client:
            with pytest.raises(CotoContextError, match="no delivery coverage"):
                await adapter.search_products("whisky", context=requested)

    asyncio.run(run())
    assert paths == ["/rest/model/atg/actors/cProfileActor/getCobertura"]
