"""Conservative cross-retailer product matching."""

from whisky_tracker.matching.matcher import ProductMatcher
from whisky_tracker.matching.models import (
    CanonicalProduct,
    ListingIdentity,
    ManualOverrides,
    MatchConfidence,
    MatchingResult,
    ProductMatchGroup,
)

__all__ = [
    "CanonicalProduct",
    "ListingIdentity",
    "ManualOverrides",
    "MatchConfidence",
    "MatchingResult",
    "ProductMatchGroup",
    "ProductMatcher",
]
