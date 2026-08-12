"""Normalized product observations returned by retailer adapters."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from whisky_tracker.models.context import RetailerContext
from whisky_tracker.models.promotion import Promotion


@dataclass(frozen=True, slots=True)
class ProductObservation:
    """A retailer SKU and its price at a particular point in time."""

    retailer: str
    retailer_product_id: str
    retailer_sku_id: str
    catalog_product_id: str | None
    gtin: str | None
    title: str
    brand: str | None
    size_value: Decimal | None
    size_unit: str | None
    pack_count: int | None
    currency: str
    current_price: Decimal
    regular_price: Decimal | None
    promotions: tuple[Promotion, ...]
    condition: str | None
    available_quantity: int | None
    in_stock: bool
    product_url: str
    context: RetailerContext
    observed_at: datetime
