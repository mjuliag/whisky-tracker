"""Alert domain models and injectable policy thresholds."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from whisky_tracker.matching.models import CanonicalProduct
from whisky_tracker.models.context import RetailerContext
from whisky_tracker.models.product import ProductObservation
from whisky_tracker.models.promotion import Promotion


class AlertType(StrEnum):
    PRICE_DROP = "price_drop"
    HISTORICAL_LOW = "historical_low"
    CROSS_RETAILER_DEAL = "cross_retailer_deal"
    PROMOTION = "promotion"


@dataclass(frozen=True, slots=True)
class AlertConfig:
    minimum_price_drop_percentage: Decimal = Decimal("10")
    minimum_cross_retailer_difference_percentage: Decimal = Decimal("10")
    minimum_promotion_discount_percentage: Decimal = Decimal("25")
    require_in_stock: bool = True

    def __post_init__(self) -> None:
        for name in (
            "minimum_price_drop_percentage",
            "minimum_cross_retailer_difference_percentage",
            "minimum_promotion_discount_percentage",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")


@dataclass(frozen=True, slots=True)
class ComparisonPrice:
    retailer: str
    price: Decimal
    currency: str
    context: RetailerContext
    product_url: str


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    promotion: Promotion
    discount_percentage: Decimal
    conditional: bool


@dataclass(frozen=True, slots=True)
class Alert:
    canonical_product: CanonicalProduct | None
    observation: ProductObservation
    alert_types: frozenset[AlertType]
    current_price: Decimal
    previous_price: Decimal | None
    historical_minimum: Decimal | None
    price_change: Decimal | None
    percentage_change: Decimal | None
    comparison_prices: tuple[ComparisonPrice, ...]
    qualifying_promotions: tuple[PromotionEvidence, ...]
    reason: str
    fingerprint: str
