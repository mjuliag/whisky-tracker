"""Typed inputs and results for historical price queries."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from whisky_tracker.models.context import RetailerContext
from whisky_tracker.models.promotion import Promotion


@dataclass(frozen=True, slots=True)
class ListingKey:
    retailer: str
    retailer_product_id: str
    retailer_sku_id: str


@dataclass(frozen=True, slots=True)
class HistoryFilter:
    listing: ListingKey | None = None
    retailer: str | None = None
    canonical_id: str | None = None
    context: RetailerContext | None = None
    seller_id: str | None = None
    store_id: str | None = None
    sales_channel: str | None = None
    start: datetime | None = None
    end: datetime | None = None


@dataclass(frozen=True, slots=True)
class StoredObservation:
    observation_id: int
    listing_id: int
    canonical_id: str | None
    listing: ListingKey
    title: str
    product_url: str
    volume_ml: int | None
    pack_count: int | None
    observed_at: datetime
    current_price: Decimal
    regular_price: Decimal | None
    currency: str
    in_stock: bool
    available_quantity: int | None
    context: RetailerContext
    promotions: tuple[Promotion, ...]


@dataclass(frozen=True, slots=True)
class PriceChange:
    absolute: Decimal
    percentage: Decimal | None


def calculate_price_change(current: StoredObservation, previous: StoredObservation) -> PriceChange:
    """Calculate a signed price change using exact decimal arithmetic."""
    absolute = current.current_price - previous.current_price
    percentage = None
    if previous.current_price != 0:
        percentage = absolute / previous.current_price * Decimal(100)
    return PriceChange(absolute=absolute, percentage=percentage)
