"""Reusable, conservative whisky identity normalization."""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from whisky_tracker.identifiers import normalize_gtin
from whisky_tracker.models.product import ProductObservation

_VOLUME = re.compile(
    r"(?<!\d)(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>ml|cc|cl|l|lt|lts|litros?)\b",
    re.IGNORECASE,
)
_PACKS = (
    re.compile(r"\b(?:pack|combo|caja|case)\s*(?:de|x)?\s*(?P<count>\d+)\b", re.I),
    re.compile(r"\bx\s*(?P<count>\d+)\b", re.I),
    re.compile(r"\b(?P<count>\d+)\s*(?:unidades|botellas|uds?|u)\b", re.I),
)
_AGE = re.compile(r"(?<!\d)(?P<age>\d{1,2})\s*(?:a(?:n|ñ)os?|years?|y\.?o\.?)\b", re.I)
_NOISE = {
    "whisky",
    "whiskey",
    "scotch",
    "bebida",
    "alcoholica",
    "original",
}
_RETAILER_NOISE = {"carrefour", "coto", "jumbo", "mercado", "libre", "argentina"}


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    ascii_text = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text).split())


def normalize_brand(value: str | None) -> str | None:
    normalized = normalize_text(value or "")
    return normalized or None


def extract_volume_ml(text: str) -> int | None:
    """Extract one unambiguous positive bottle volume in milliliters."""
    values: set[int] = set()
    for match in _VOLUME.finditer(text):
        number = Decimal(match.group("value").replace(",", "."))
        unit = match.group("unit").casefold()
        multiplier = (
            Decimal(10) if unit == "cl" else Decimal(1000) if unit.startswith("l") else Decimal(1)
        )
        milliliters = number * multiplier
        if milliliters > 0 and milliliters == milliliters.to_integral_value():
            values.add(int(milliliters))
    return values.pop() if len(values) == 1 else None


def volume_from_observation(observation: ProductObservation) -> int | None:
    if observation.size_value is not None and observation.size_unit:
        unit = normalize_text(observation.size_unit)
        multiplier = Decimal(1000) if unit in {"l", "lt", "litro", "litros"} else Decimal(1)
        if unit in {"ml", "cc"} | {"l", "lt", "litro", "litros"}:
            value = observation.size_value * multiplier
            if value > 0 and value == value.to_integral_value():
                return int(value)
    return extract_volume_ml(observation.title)


def extract_pack_count(text: str) -> int | None:
    counts = {
        int(match.group("count"))
        for pattern in _PACKS
        if (match := pattern.search(text)) and int(match.group("count")) > 0
    }
    return counts.pop() if len(counts) == 1 else None


def pack_from_observation(observation: ProductObservation) -> int | None:
    if observation.pack_count is not None and observation.pack_count > 0:
        return observation.pack_count
    explicit = extract_pack_count(observation.title)
    if explicit is not None:
        return explicit
    # Supermarket SKUs conventionally represent one sale unit. Marketplace titles can be
    # bundles even when metadata is absent, so absence there remains unknown.
    return None if "mercado libre" in normalize_text(observation.retailer) else 1


def extract_age_statement(text: str) -> int | None:
    ages = {int(match.group("age")) for match in _AGE.finditer(text)}
    return ages.pop() if len(ages) == 1 else None


def normalized_title(text: str) -> str:
    text = _VOLUME.sub(" ", text)
    text = _AGE.sub(" ", text)
    for pattern in _PACKS:
        text = pattern.sub(" ", text)
    tokens = normalize_text(text).split()
    return " ".join(token for token in tokens if token not in _NOISE | _RETAILER_NOISE)


def derive_expression(title: str, brand: str | None) -> str | None:
    value = normalized_title(title)
    tokens = value.split()
    brand_tokens = (brand or "").split()
    if brand_tokens and tokens[: len(brand_tokens)] == brand_tokens:
        tokens = tokens[len(brand_tokens) :]
    expression = " ".join(tokens)
    return expression or None


def extract_known_expression(title: str) -> str | None:
    """Return only explicitly recognized identity-bearing whisky variants."""
    tokens = normalize_text(title)
    patterns = (
        (r"\bgentleman(?: jack)?\b", "gentleman"),
        (r"\bold (?:no|n) ?7\b", "old no 7"),
        (r"\bsingle barrel\b", "single barrel"),
        (r"\b(?:tennessee|tennesse)? ?honey\b", "tennessee honey"),
        (r"\b(?:tennessee )?apple\b", "tennessee apple"),
        (r"\b(?:tennessee|tenessee) fire\b", "tennessee fire"),
        (r"\bdouble black\b", "double black"),
        (r"\bblack label\b", "black label"),
        (r"\bblue label\b", "blue label"),
        (r"\bgreen label\b", "green label"),
        (r"\bred label\b", "red label"),
        (r"\bj b rare\b", "rare"),
    )
    matches = {expression for pattern, expression in patterns if re.search(pattern, tokens)}
    return matches.pop() if len(matches) == 1 else None


@dataclass(frozen=True, slots=True)
class NormalizedObservation:
    observation: ProductObservation
    brand: str | None
    expression: str | None
    age_statement: int | None
    volume_ml: int | None
    pack_count: int | None
    gtin: str | None
    title: str


def normalize_observation(observation: ProductObservation) -> NormalizedObservation:
    brand = normalize_brand(observation.brand)
    title = normalized_title(observation.title)
    expression = derive_expression(observation.title, brand)
    age = extract_age_statement(observation.title)
    if age is None and expression and expression.isdigit() and 3 <= int(expression) <= 50:
        age = int(expression)
    return NormalizedObservation(
        observation=observation,
        brand=brand,
        expression=expression,
        age_statement=age,
        volume_ml=volume_from_observation(observation),
        pack_count=pack_from_observation(observation),
        gtin=normalize_gtin(observation.gtin),
        title=title,
    )
