"""User brand preferences used only to order eligible product alerts."""

from enum import IntEnum

from whisky_tracker.matching.models import CanonicalProduct
from whisky_tracker.matching.normalization import normalize_brand, normalize_text


class PreferenceTier(IntEnum):
    PRIORITY = 0
    NORMAL = 1
    LOW = 2


# Exact canonical-brand matches after identity normalization. Add aliases here when
# a retailer's canonical brand has a known spelling variant.
_BRAND_PREFERENCES = {
    "Jack Daniel's": PreferenceTier.PRIORITY,
    "Jack Daniels": PreferenceTier.PRIORITY,
    "Jameson": PreferenceTier.PRIORITY,
    "Chivas Regal": PreferenceTier.PRIORITY,
    "Johnnie Walker": PreferenceTier.PRIORITY,
    "Old Parr": PreferenceTier.PRIORITY,
    "Grand Old Parr": PreferenceTier.PRIORITY,
    "Jim Beam": PreferenceTier.NORMAL,
    "J&B": PreferenceTier.NORMAL,
    "Singleton": PreferenceTier.NORMAL,
    "Blenders": PreferenceTier.LOW,
}
_NORMALIZED_BRAND_PREFERENCES = {
    normalize_text(brand): tier for brand, tier in _BRAND_PREFERENCES.items()
}


def preference_tier(product: CanonicalProduct) -> PreferenceTier:
    """Return the configured tier for an exact normalized canonical brand."""
    return _NORMALIZED_BRAND_PREFERENCES.get(normalize_brand(product.brand), PreferenceTier.LOW)
