"""Developer-facing formatting for matcher output (not a production CLI)."""

from collections.abc import Iterable

from whisky_tracker.matching.matcher import ProductMatcher
from whisky_tracker.matching.models import MatchingResult
from whisky_tracker.models.product import ProductObservation


def inspect_observations(
    observations: Iterable[ProductObservation], *, matcher: ProductMatcher | None = None
) -> MatchingResult:
    """Run matching, print readable groups and return the complete result."""
    result = (matcher or ProductMatcher()).match(observations)
    for group in result.groups:
        product = group.canonical_product
        name = " ".join(part for part in (product.brand, product.expression) if part)
        size = f" {product.volume_ml} ml" if product.volume_ml is not None else ""
        pack = f" x{product.pack_count}" if product.pack_count not in {None, 1} else ""
        print(f"{name or product.canonical_id}{size}{pack}")
        print(f"  match: {group.match_confidence.value} ({group.match_reason})")
        for observation in group.observations:
            print(
                f"  {observation.retailer} -> {observation.currency} "
                f"{observation.current_price} [{observation.retailer_product_id}]"
            )
    if result.unmatched:
        print("Unmatched")
        for observation in result.unmatched:
            print(
                f"  {observation.retailer} -> {observation.title} "
                f"[{observation.retailer_product_id}]"
            )
    return result
