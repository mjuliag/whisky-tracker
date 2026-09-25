"""Transparent business-rule ordering for eligible alerts."""

from whisky_tracker.alerts.models import Alert, AlertType, ProductAlert
from whisky_tracker.alerts.preferences import preference_tier


def alert_priority_key(alert: Alert | ProductAlert) -> tuple[int | str, ...]:
    """Sort eligible product alerts by preference, then existing signals and tie-breakers."""
    types = alert.alert_types
    if isinstance(alert, ProductAlert):
        offer_types = [offer.alert_types for offer in alert.offers]
        promoted_retailers = sum(AlertType.PROMOTION in item for item in offer_types)
        if any({AlertType.HISTORICAL_LOW, AlertType.PRICE_DROP} <= item for item in offer_types):
            tier = 0
        elif (
            AlertType.CROSS_RETAILER_DEAL in alert.best_offer.alert_types
            and len(alert.best_offer.alert_types) > 1
        ):
            tier = 1
        elif promoted_retailers >= 2:
            tier = 2
        elif any(AlertType.PRICE_DROP in item for item in offer_types):
            tier = 3
        elif any(AlertType.HISTORICAL_LOW in item for item in offer_types):
            tier = 4
        elif AlertType.PROMOTION in types:
            tier = 5
        else:
            tier = 6
        observation = alert.best_offer.observation
        return (
            preference_tier(alert.canonical_product),
            tier,
            -sum(len(item) for item in offer_types),
            alert.canonical_product.canonical_id,
            observation.title.casefold(),
            observation.retailer_product_id,
            observation.retailer_sku_id,
        )
    if {AlertType.HISTORICAL_LOW, AlertType.PRICE_DROP} <= types:
        tier = 0
    elif AlertType.CROSS_RETAILER_DEAL in types and len(types) > 1:
        tier = 1
    elif AlertType.PRICE_DROP in types:
        tier = 2
    elif AlertType.HISTORICAL_LOW in types:
        tier = 3
    elif types == {AlertType.PROMOTION}:
        tier = 4
    else:
        tier = 5
    observation = alert.observation
    return (
        tier,
        -len(types),
        observation.retailer.casefold(),
        observation.title.casefold(),
        observation.retailer_product_id,
        observation.retailer_sku_id,
    )
