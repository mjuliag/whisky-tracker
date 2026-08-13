"""Transparent business-rule ordering for eligible alerts."""

from whisky_tracker.alerts.models import Alert, AlertType


def alert_priority_key(alert: Alert) -> tuple[int, int, str, str, str, str]:
    """Sort strongest combined purchasing signals first, with stable identity tie-breakers."""
    types = alert.alert_types
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
