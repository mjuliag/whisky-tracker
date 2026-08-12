"""Promotion parsing tests using Carrefour's live serialization patterns."""

from decimal import Decimal

from whisky_tracker.models.promotion import DiscountType, PromotionKind
from whisky_tracker.retailers.vtex import VtexAdapter


def test_parses_general_and_loyalty_backing_field_highlights() -> None:
    offer = {
        "DiscountHighLight": [
            {"<Name>k__BackingField": "PROMO-25% Off Max 8 unidades"},
            {"<Name>k__BackingField": "PROMO-25% Off Mi Crf Max 8 unidades"},
        ]
    }

    general, loyalty = VtexAdapter._extract_promotions(offer)

    assert general.kind is PromotionKind.GENERAL
    assert general.applied_to_current_price is True
    assert general.discount_value == Decimal("25")
    assert general.discount_type is DiscountType.PERCENTAGE
    assert loyalty.kind is PromotionKind.LOYALTY
    assert loyalty.applied_to_current_price is True


def test_parses_payment_method_teaser_conditions_and_effects() -> None:
    offer = {
        "Teasers": [
            {
                "<Name>k__BackingField": "Tarjeta Carrefour 15%",
                "<Conditions>k__BackingField": {
                    "<MinimumQuantity>k__BackingField": 2,
                    "<Parameters>k__BackingField": [
                        {
                            "<Name>k__BackingField": "RestrictionsBins",
                            "<Value>k__BackingField": "507858,858110",
                        }
                    ],
                },
                "<Effects>k__BackingField": {
                    "<Parameters>k__BackingField": [
                        {
                            "<Name>k__BackingField": "PercentualDiscount",
                            "<Value>k__BackingField": "15",
                        }
                    ]
                },
            }
        ]
    }

    promotion = VtexAdapter._extract_promotions(offer)[0]

    assert promotion.kind is PromotionKind.PAYMENT_METHOD
    assert promotion.applied_to_current_price is False
    assert promotion.discount_value == Decimal("15")
    assert promotion.minimum_quantity == Decimal("2")
    assert promotion.conditions == ("RestrictionsBins=507858,858110",)
