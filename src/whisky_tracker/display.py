"""Human-facing product and retailer presentation, separate from matching tokens."""

from whisky_tracker.matching.models import CanonicalProduct
from whisky_tracker.matching.normalization import (
    derive_expression,
    extract_age_statement,
    extract_known_expression,
    normalize_brand,
    normalize_text,
)
from whisky_tracker.models.context import RetailerContext
from whisky_tracker.models.product import ProductObservation

_BRANDS = {
    "j b": "J&B",
    "jack daniels": "Jack Daniel's",
    "johnnie walker": "Johnnie Walker",
}
_EXPRESSIONS = {
    "gentleman": "Gentleman Jack",
    "gentleman jack": "Gentleman Jack",
    "old n7": "Old No. 7",
    "old n 7": "Old No. 7",
    "old no 7": "Old No. 7",
    "single barrel": "Single Barrel",
    "honey": "Tennessee Honey",
    "honey jack": "Tennessee Honey",
    "tennessee honey": "Tennessee Honey",
    "apple": "Tennessee Apple",
    "apple jack": "Tennessee Apple",
    "tennessee apple": "Tennessee Apple",
    "fire": "Tennessee Fire",
    "fire jack daniels": "Tennessee Fire",
    "tennessee fire": "Tennessee Fire",
    "black label": "Black Label",
    "double black": "Double Black",
    "blue label": "Blue Label",
    "green label": "Green Label",
    "red label": "Red Label",
    "rare": "Rare",
}


def display_product_name(product: CanonicalProduct | None, observation: ProductObservation) -> str:
    brand_key = product.brand if product and product.brand else normalize_brand(observation.brand)
    brand = display_brand(brand_key)
    expression_key = (
        product.expression
        if product and product.expression
        else extract_known_expression(observation.title)
        or derive_expression(observation.title, normalize_brand(observation.brand))
    )
    expression_key = _remove_redundant_expression(brand_key, expression_key)
    if expression_key is None:
        expression_key = _remove_redundant_expression(
            brand_key,
            extract_known_expression(observation.title)
            or derive_expression(observation.title, normalize_brand(observation.brand)),
        )
    expression = _display_product_expression(brand_key, expression_key)
    age = (
        product.age_statement
        if product and product.age_statement is not None
        else extract_age_statement(observation.title)
    )
    volume = (
        product.volume_ml if product and product.volume_ml else _observation_volume(observation)
    )
    pack = product.pack_count if product and product.pack_count else observation.pack_count
    identity = " ".join(part for part in (brand, expression) if part)
    if not identity:
        identity = observation.title.strip()
    if age is not None:
        identity += f" {age} Years"
    if volume:
        identity += f" {volume} ml"
    if pack not in {None, 1}:
        identity += f" x{pack}"
    return identity


def display_brand(value: str | None) -> str | None:
    normalized = normalize_text(value or "")
    if not normalized:
        return None
    if normalized in _BRANDS:
        return _BRANDS[normalized]
    return _human_case(normalized)


def display_expression(value: str | None) -> str | None:
    normalized = normalize_text(value or "")
    if not normalized:
        return None
    if normalized in _EXPRESSIONS:
        return _EXPRESSIONS[normalized]
    return _human_case(normalized)


def _display_product_expression(brand: str | None, expression: str | None) -> str | None:
    normalized_brand = normalize_text(brand or "")
    normalized_expression = normalize_text(expression or "")
    if normalized_brand == "chivas regal" and normalized_expression in {"xv", "xv clear"}:
        return "XV"
    if normalized_brand == "j b" and normalized_expression in {"blended", "blended scotch"}:
        return None
    if normalized_brand == "blenders":
        if normalized_expression in {"seagram s pride", "pride", "blenders pride"}:
            return "Pride"
        if normalized_expression in {
            "america",
            "americano",
            "estilo america",
            "estilo americano",
        }:
            return "Americano"
    return display_expression(expression)


def _remove_redundant_expression(brand: str | None, expression: str | None) -> str | None:
    brand_tokens = normalize_text(brand or "").split()
    expression_tokens = [
        token
        for token in normalize_text(expression or "").split()
        if token not in {"bot", "botella", "en"}
    ]
    if not expression_tokens:
        return None
    expression_text = " ".join(expression_tokens)
    brand_text = " ".join(brand_tokens)
    if expression_text == brand_text:
        return None
    meaningful = [token for token in expression_tokens if token not in set(brand_tokens)]
    return " ".join(meaningful) or None


def display_retailer(retailer: str, context: RetailerContext | None = None) -> str:
    normalized = normalize_text(retailer)
    if "carrefour" in normalized:
        label = "Carrefour"
        if context and context.store_name:
            label += f" — {context.store_name}"
        return label
    if "mercado libre" in normalized:
        return "Mercado Libre"
    if "coto" in normalized:
        return "Coto"
    if "jumbo" in normalized:
        return "Jumbo"
    return _human_case(normalized)


def _human_case(value: str) -> str:
    small = {"de", "del", "and", "of"}
    return " ".join(token if token in small else token.capitalize() for token in value.split())


def _observation_volume(observation: ProductObservation) -> int | None:
    if observation.size_value is None or not observation.size_unit:
        return None
    unit = normalize_text(observation.size_unit)
    multiplier = 1000 if unit in {"l", "lt", "litro", "litros"} else 1
    value = observation.size_value * multiplier
    return int(value) if value == value.to_integral_value() else None


def retailer_link_label(retailer: str) -> str:
    return f"Ver en {display_retailer(retailer)}"
