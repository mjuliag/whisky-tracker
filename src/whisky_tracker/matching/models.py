"""Models exposed by the product matching layer."""

from dataclasses import dataclass
from enum import StrEnum

from whisky_tracker.models.product import ProductObservation


class MatchConfidence(StrEnum):
    """Inspectable, deliberately non-numeric match quality."""

    EXACT_GTIN = "exact_gtin"
    STRONG_ATTRIBUTES = "strong_attributes"
    FUZZY_SUPPORTED = "fuzzy_supported"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class CanonicalProduct:
    canonical_id: str
    brand: str | None
    expression: str | None
    age_statement: int | None
    volume_ml: int | None
    pack_count: int | None
    gtins: frozenset[str]


@dataclass(frozen=True, slots=True)
class ProductMatchGroup:
    canonical_product: CanonicalProduct
    observations: tuple[ProductObservation, ...]
    match_confidence: MatchConfidence
    match_reason: str


@dataclass(frozen=True, slots=True)
class MatchingResult:
    groups: tuple[ProductMatchGroup, ...]
    unmatched: tuple[ProductObservation, ...]


@dataclass(frozen=True, slots=True, order=True)
class ListingIdentity:
    """Stable retailer-local identity used only by manual rules."""

    retailer: str
    retailer_product_id: str
    retailer_sku_id: str

    @classmethod
    def from_observation(cls, observation: ProductObservation) -> ListingIdentity:
        return cls(
            observation.retailer,
            observation.retailer_product_id,
            observation.retailer_sku_id,
        )


IdentityPair = frozenset[ListingIdentity]


@dataclass(frozen=True, slots=True)
class ManualOverrides:
    """Explicit, testable forced and forbidden listing pairs."""

    force_match: frozenset[IdentityPair] = frozenset()
    force_non_match: frozenset[IdentityPair] = frozenset()

    def __post_init__(self) -> None:
        overlap = self.force_match & self.force_non_match
        if overlap:
            raise ValueError(f"manual override pairs conflict: {overlap!r}")

    @staticmethod
    def pair(left: ListingIdentity, right: ListingIdentity) -> IdentityPair:
        if left == right:
            raise ValueError("an override pair requires two different listings")
        return frozenset((left, right))
