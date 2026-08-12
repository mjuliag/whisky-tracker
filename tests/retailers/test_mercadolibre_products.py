import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from whisky_tracker.models.context import ContextResolution, FulfillmentMode
from whisky_tracker.retailers.mercadolibre import MercadoLibreAdapter, MercadoLibreAuth

FIXTURE = Path(__file__).parent / "fixtures" / "mercadolibre_items.json"


def adapter() -> MercadoLibreAdapter:
    return MercadoLibreAdapter(auth=MercadoLibreAuth("token"))


def test_normalizes_listing_identity_price_attributes_and_context() -> None:
    body = json.loads(FIXTURE.read_text())[0]["body"]
    product = adapter()._parse_item(
        body,
        context=adapter().default_context(),
        observed_at=datetime.now(UTC),
    )

    assert product is not None
    assert product.retailer_product_id == "MLA1001"
    assert product.retailer_sku_id == "555"
    assert product.catalog_product_id == "MLA-CATALOG-1001"
    assert product.current_price == Decimal("24500")
    assert product.regular_price == Decimal("28000")
    assert product.gtin == "7790895000997"
    assert product.brand == "Ejemplo"
    assert product.size_value == Decimal("750")
    assert product.size_unit == "ml"
    assert product.pack_count == 1
    assert product.condition == "new"
    assert product.available_quantity == 7
    assert product.in_stock is True
    assert product.context.seller_id == "12345"
    assert product.context.store_id == "987"
    assert product.context.fulfillment_mode is FulfillmentMode.GENERIC
    assert product.context.context_resolution is ContextResolution.GENERIC


def test_normalizes_pack_and_unavailable_listing() -> None:
    body = json.loads(FIXTURE.read_text())[1]["body"]
    base_adapter = adapter()
    product = base_adapter._parse_item(
        body,
        context=base_adapter.default_context(),
        observed_at=datetime.now(UTC),
    )

    assert product is not None
    assert product.current_price == Decimal("120000.50")
    assert product.regular_price is None
    assert product.pack_count == 6
    assert product.size_value == Decimal("1")
    assert product.size_unit == "l"
    assert product.gtin is None
    assert product.available_quantity == 0
    assert product.in_stock is False
    assert product.context.seller_id == "45678"


def test_filter_removes_obvious_accessories_but_retains_whisky() -> None:
    predicate = MercadoLibreAdapter._is_whisky_listing
    assert predicate({"title": "Whisky single malt 750 ml", "category_id": "MLA-BEBIDAS"})
    assert predicate({"title": "Bourbon americano 1 litro", "category_id": "MLA-BEBIDAS"})
    assert not predicate({"title": "Set 6 vasos para whisky", "category_id": "MLA-BAZAR"})
    assert not predicate({"title": "Botella vacía de whisky antigua", "category_id": "MLA-ANTIG"})


def test_gtin_validation_rejects_wrong_check_digit() -> None:
    attributes = [{"id": "GTIN", "value_name": "7791234567891"}]
    assert MercadoLibreAdapter._extract_gtin(attributes) is None
