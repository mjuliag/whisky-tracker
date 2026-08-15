"""Telegram HTML alert rendering with explicit, user-facing presentation."""

from decimal import Decimal, InvalidOperation
from html import escape

from whisky_tracker.alerts.models import (
    Alert,
    AlertType,
    ProductAlert,
    ProductOffer,
    PromotionEvidence,
)
from whisky_tracker.display import display_product_name, display_retailer, retailer_link_label
from whisky_tracker.models.promotion import Promotion, PromotionKind


def format_alert(alert: Alert | ProductAlert) -> str:
    if isinstance(alert, ProductAlert):
        return _format_product_alert(alert)
    return _format_listing_alert(alert)


def _format_product_alert(alert: ProductAlert) -> str:
    sample = alert.best_offer.observation
    lines = [
        "🔥 <b>Whisky Tracker</b>",
        "",
        f"<b>{escape(display_product_name(alert.canonical_product, sample))}</b>",
        "",
        "🏆 <b>Mejor precio</b>",
    ]
    for index, offer in enumerate(alert.offers):
        if index:
            lines.append("")
        observation = offer.observation
        retailer = display_retailer(observation.retailer, observation.context)
        price = _price(observation.current_price, observation.currency)
        lines.append(f"{escape(retailer)}: {price}")
        if AlertType.PRICE_DROP in offer.alert_types and offer.previous_price is not None:
            detail = f"📉 Bajó desde {_price(offer.previous_price, observation.currency)}"
            if offer.percentage_change is not None:
                detail += f" ({_percentage(abs(offer.percentage_change))})"
            lines.append(detail)
        if AlertType.HISTORICAL_LOW in offer.alert_types:
            lines.append("📉 Nuevo mínimo histórico")
        lines.extend(_promotion_lines(offer))

    if alert.second_best_offer is not None and alert.savings_amount is not None:
        comparison = alert.second_best_offer.observation
        retailer = display_retailer(comparison.retailer, comparison.context)
        lines.extend(
            (
                "",
                f"💰 Ahorrás {_price(alert.savings_amount, sample.currency)} vs. "
                f"{escape(retailer)}",
            )
        )

    links = []
    seen_retailers: set[str] = set()
    for offer in alert.offers:
        observation = offer.observation
        if not observation.product_url or observation.retailer in seen_retailers:
            continue
        seen_retailers.add(observation.retailer)
        label = escape(retailer_link_label(observation.retailer))
        link = escape(observation.product_url, quote=True)
        links.append(f'🔗 <a href="{link}">{label}</a>')
    if links:
        lines.extend(("", *links))
    return "\n".join(lines)


def _format_listing_alert(alert: Alert) -> str:
    observation = alert.observation
    lines = [
        "🔥 <b>Whisky Tracker</b>",
        "",
        f"<b>{escape(display_product_name(alert.canonical_product, observation))}</b>",
        "",
        f"🛒 {escape(display_retailer(observation.retailer, observation.context))}",
        f"Ahora: {_price(observation.current_price, observation.currency)}",
    ]
    if AlertType.PRICE_DROP in alert.alert_types and alert.previous_price is not None:
        lines.append(f"Antes: {_price(alert.previous_price, observation.currency)}")
        if alert.percentage_change is not None:
            lines.append(f"Bajó: {_percentage(abs(alert.percentage_change))}")
    if AlertType.CROSS_RETAILER_DEAL in alert.alert_types:
        lines.extend(("", "🏆 <b>Comparación de precios</b>"))
        for comparison in alert.comparison_prices:
            if comparison.retailer == observation.retailer:
                continue
            retailer = display_retailer(comparison.retailer, comparison.context)
            lines.append(f"{escape(retailer)}: {_price(comparison.price, comparison.currency)}")
    if AlertType.HISTORICAL_LOW in alert.alert_types:
        lines.extend(("", "📉 <b>Nuevo mínimo histórico</b>"))

    promotion_lines = _promotion_lines(alert)
    if promotion_lines:
        lines.extend(("", *promotion_lines))

    link_label = escape(retailer_link_label(observation.retailer))
    link = escape(observation.product_url, quote=True)
    lines.extend(("", f'🔗 <a href="{link}">{link_label}</a>'))
    return "\n".join(lines)


def _promotion_lines(alert: Alert | ProductOffer) -> list[str]:
    evidence_by_promotion = {
        evidence.promotion: evidence for evidence in alert.qualifying_promotions
    }
    lines: list[str] = []
    regular_price_shown = False
    seen: set[tuple[str, PromotionKind]] = set()
    for promotion in alert.observation.promotions:
        key = (promotion.name, promotion.kind)
        if key in seen:
            continue
        seen.add(key)
        evidence = evidence_by_promotion.get(promotion)
        if promotion.kind is PromotionKind.PAYMENT_METHOD:
            rendered = _payment_promotion(promotion, evidence, alert)
        else:
            rendered = _discount_promotion(promotion, evidence)
        if not rendered:
            continue
        lines.extend(rendered)
        if alert.observation.regular_price is not None and not regular_price_shown:
            regular_price = _price(alert.observation.regular_price, alert.observation.currency)
            lines.append(f"Precio regular: {regular_price}")
            regular_price_shown = True
        lines.extend(_human_conditions(promotion))
    return lines


def _discount_promotion(promotion: Promotion, evidence: PromotionEvidence | None) -> list[str]:
    if evidence:
        percentage = _percentage(evidence.discount_percentage)
        icon = "🥃"
        if promotion.kind is PromotionKind.LOYALTY:
            return [f"{icon} {percentage} de descuento con {escape(promotion.name)}"]
        if promotion.kind is PromotionKind.COUPON:
            return [f"{icon} {percentage} de descuento con cupón"]
        return [f"{icon} {percentage} de descuento"]
    name = promotion.name.strip()
    if not name or name.casefold() in {"descuento coto", "promoción"}:
        return []
    return [f"🥃 {escape(name)}"]


def _payment_promotion(
    promotion: Promotion,
    evidence: PromotionEvidence | None,
    alert: Alert | ProductOffer,
) -> list[str]:
    values = _condition_values(promotion)
    installments = values.get("installments")
    installment_price = _decimal_from_text(values.get("installment_price"))
    generic_name = promotion.name.casefold() in {
        "promoción con medio de pago",
        "promocion con medio de pago",
    }
    if (
        evidence is None
        and installments == "1"
        and installment_price == alert.observation.current_price
        and generic_name
    ):
        return []
    if evidence:
        return [f"💳 {_percentage(evidence.discount_percentage)} con medio de pago elegible"]
    if generic_name:
        return []
    return [f"💳 {escape(promotion.name)}"]


def _human_conditions(promotion: Promotion) -> list[str]:
    result: list[str] = []
    for condition in promotion.conditions:
        key, separator, value = condition.partition("=")
        if separator:
            if key in {"comments", "takingText"} and value:
                result.append(escape(_clean_condition(value)))
            continue
        cleaned = _clean_condition(condition)
        if cleaned:
            result.append(escape(cleaned))
    return list(dict.fromkeys(result))


def _clean_condition(value: str) -> str:
    value = value.strip()
    if value.casefold() == "no acumulable con otras promos":
        return "No acumulable con otras promociones"
    return value


def _condition_values(promotion: Promotion) -> dict[str, str]:
    values: dict[str, str] = {}
    for condition in promotion.conditions:
        key, separator, value = condition.partition("=")
        if separator:
            values[key] = value
    return values


def _decimal_from_text(value: str | None) -> Decimal | None:
    if not value:
        return None
    cleaned = value.strip().replace("$", "").replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _percentage(value: Decimal) -> str:
    rendered = f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return rendered + "%"


def _price(value: Decimal, currency: str) -> str:
    symbol = "$" if currency == "ARS" else f"{escape(currency)} "
    whole, _, fraction = f"{value:,.2f}".partition(".")
    whole = whole.replace(",", ".")
    rendered = whole if fraction == "00" else f"{whole},{fraction}"
    return f"{symbol}{rendered}"
