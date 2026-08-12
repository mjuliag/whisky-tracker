"""Structured retailer promotion data."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class PromotionKind(StrEnum):
    GENERAL = "general"
    LOYALTY = "loyalty"
    PAYMENT_METHOD = "payment_method"
    QUANTITY = "quantity"
    COUPON = "coupon"
    SHIPPING = "shipping"


class DiscountType(StrEnum):
    PERCENTAGE = "percentage"
    FIXED_AMOUNT = "fixed_amount"


@dataclass(frozen=True, slots=True)
class Promotion:
    """A promotion advertised by a retailer for a product offer."""

    name: str
    kind: PromotionKind
    applied_to_current_price: bool
    discount_value: Decimal | None = None
    discount_type: DiscountType | None = None
    minimum_quantity: Decimal | None = None
    conditions: tuple[str, ...] = ()
