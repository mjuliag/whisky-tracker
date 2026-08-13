from datetime import UTC, datetime
from decimal import Decimal

from whisky_tracker.matching import (
    ListingIdentity,
    ManualOverrides,
    MatchConfidence,
    ProductMatcher,
)
from whisky_tracker.models import (
    ContextResolution,
    FulfillmentMode,
    ProductObservation,
    RetailerContext,
)

CONTEXT = RetailerContext(
    fulfillment_mode=FulfillmentMode.GENERIC,
    context_resolution=ContextResolution.GENERIC,
)


def observation(
    retailer: str,
    product_id: str,
    title: str,
    *,
    brand: str | None = "Johnnie Walker",
    gtin: str | None = None,
    size: Decimal | None = None,
    unit: str | None = None,
    pack: int | None = 1,
) -> ProductObservation:
    return ProductObservation(
        retailer=retailer,
        retailer_product_id=product_id,
        retailer_sku_id=product_id,
        catalog_product_id=None,
        gtin=gtin,
        title=title,
        brand=brand,
        size_value=size,
        size_unit=unit,
        pack_count=pack,
        currency="ARS",
        current_price=Decimal("10000"),
        regular_price=None,
        promotions=(),
        condition="new",
        available_quantity=1,
        in_stock=True,
        product_url=f"https://example.test/{product_id}",
        context=CONTEXT,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def confidence(left: ProductObservation, right: ProductObservation) -> MatchConfidence | None:
    result = ProductMatcher().match([left, right])
    return result.groups[0].match_confidence if result.groups else None


def test_valid_gtin_matches_different_titles_across_retailers() -> None:
    left = observation("Carrefour", "c1", "Whisky Black Label 750 ml", gtin="7790895000997")
    right = observation("Coto", "c2", "J. Walker Etiqueta Negra", gtin="7790895000997")
    assert confidence(left, right) is MatchConfidence.EXACT_GTIN


def test_malformed_gtin_is_not_trusted() -> None:
    left = observation("Carrefour", "c1", "Completely Different", gtin="7791234567891")
    right = observation("Coto", "c2", "Another Product", gtin="7791234567891")
    assert confidence(left, right) is None


def test_same_whisky_matches_by_strong_attributes() -> None:
    left = observation("Carrefour", "c1", "Johnnie Walker Black Label 750 ml")
    right = observation("Coto", "c2", "WHISKY JOHNNIE WALKER BLACK LABEL 750 CC")
    assert confidence(left, right) is MatchConfidence.STRONG_ATTRIBUTES


def test_supermarket_and_mercado_libre_match_and_keep_all_listings() -> None:
    products = [
        observation("Carrefour", "c1", "Johnnie Walker Black Label 750 ml"),
        observation("Mercado Libre Argentina", "MLA1", "Whisky Johnnie Walker Black Label 750ml"),
        observation("Mercado Libre Argentina", "MLA2", "Johnnie Walker Black Label 750 cc"),
    ]
    result = ProductMatcher().match(products)
    assert len(result.groups) == 1
    assert {item.retailer_product_id for item in result.groups[0].observations} == {
        "c1",
        "MLA1",
        "MLA2",
    }


def test_one_liter_matches_1000_ml() -> None:
    left = observation("Carrefour", "c1", "Johnnie Walker Black Label 1 L")
    right = observation("Jumbo", "j1", "Johnnie Walker Black Label 1000 ml")
    assert confidence(left, right) is MatchConfidence.STRONG_ATTRIBUTES


def test_incompatible_volume_never_matches() -> None:
    left = observation("Carrefour", "c1", "Johnnie Walker Black Label 700 ml")
    right = observation("Coto", "c2", "Johnnie Walker Black Label 750 ml")
    assert confidence(left, right) is None


def test_single_bottle_does_not_match_pack() -> None:
    single = observation("Coto", "c1", "Johnnie Walker Black Label 750 ml")
    pack = observation("Jumbo", "j1", "Johnnie Walker Black Label 750 ml pack x 6", pack=6)
    assert confidence(single, pack) is None


def test_equivalent_pack_formats_match() -> None:
    left = observation("Coto", "c1", "Johnnie Walker Black Label 750 ml pack x 6", pack=None)
    right = observation("Jumbo", "j1", "Johnnie Walker Black Label 750 cc caja de 6", pack=None)
    assert confidence(left, right) is MatchConfidence.STRONG_ATTRIBUTES


def test_age_must_agree() -> None:
    twelve_a = observation("Coto", "c1", "Johnnie Walker Black Label 12 años 750 ml")
    twelve_b = observation("Jumbo", "j1", "Johnnie Walker Black Label 12 years 750ml")
    eighteen = observation("Jumbo", "j2", "Johnnie Walker Black Label 18 años 750ml")
    assert confidence(twelve_a, twelve_b) is MatchConfidence.STRONG_ATTRIBUTES
    assert confidence(twelve_a, eighteen) is None


def test_singleton_known_ages_remain_distinct() -> None:
    twelve = observation(
        "Coto", "singleton-12", "Whisky Singleton 12 Años 700 ml", brand="Singleton"
    )
    fifteen = observation(
        "Jumbo", "singleton-15", "Whisky Singleton 15 Years 700 ml", brand="Singleton"
    )
    assert confidence(twelve, fifteen) is None


def test_expression_variants_remain_distinct() -> None:
    black = observation("Coto", "c1", "Johnnie Walker Black Label 750 ml")
    double = observation("Jumbo", "j1", "Johnnie Walker Double Black 750 ml")
    red = observation("Jumbo", "j2", "Johnnie Walker Red Label 750 ml")
    assert confidence(black, double) is None
    assert confidence(black, red) is None


def test_jack_daniels_expressions_remain_distinct() -> None:
    gentleman = observation(
        "Coto",
        "gentleman",
        "Whisky Gentleman Jack Daniels 700ml",
        brand="JACK DANIELS",
    )
    old_no_7 = observation(
        "Jumbo",
        "old-no-7",
        "Whisky Jack Daniels Old N°7 700 cc",
        brand="Jack Daniels",
    )
    assert confidence(gentleman, old_no_7) is None


def test_gentleman_jack_exact_gtin_group_retains_canonical_expression() -> None:
    products = (
        observation(
            "Coto",
            "prod00602091",
            "Whisky Gentleman Jack Daniels 700ml",
            brand="JACK DANIELS",
            gtin="5099873038758",
        ),
        observation(
            "Jumbo",
            "gentleman-jack",
            "Whisky Gentleman Jack 700 Cc Jack Daniels Whisky Jack Daniels Gentleman Jack 700cc",
            brand="Jack Daniels",
            gtin="5099873038758",
        ),
        observation(
            "Carrefour",
            "gentleman",
            "Whisky importado Jack Daniels gentleman 700 ml",
            brand="JACK DANIELS",
            gtin="5099873038758",
        ),
    )
    group = ProductMatcher().match(products).groups[0]
    assert group.match_confidence is MatchConfidence.EXACT_GTIN
    assert group.canonical_product.expression == "gentleman"


def test_missing_gtin_with_attributes_matches() -> None:
    left = observation("Coto", "c1", "Johnnie Walker Black Label 750 ml", gtin=None)
    right = observation("Jumbo", "j1", "Johnnie Walker Black Label 750 ml", gtin=None)
    assert confidence(left, right) is MatchConfidence.STRONG_ATTRIBUTES


def test_missing_volume_reduces_match_to_fuzzy_supported() -> None:
    known = observation("Coto", "c1", "Johnnie Walker Black Label 750 ml")
    unknown = observation("Jumbo", "j1", "Johnnie Walker Black Label", size=None, unit=None)
    assert confidence(known, unknown) is MatchConfidence.FUZZY_SUPPORTED


def test_ambiguous_marketplace_pack_stays_unmatched() -> None:
    known = observation("Coto", "c1", "Johnnie Walker Black Label 750 ml")
    ambiguous = observation(
        "Mercado Libre Argentina", "MLA1", "Johnnie Walker Black Label 750 ml", pack=None
    )
    assert confidence(known, ambiguous) is None


def test_harmless_spelling_difference_uses_fuzzy_support() -> None:
    left = observation("Coto", "c1", "Johnnie Walker Black Label 750 ml")
    right = observation("Jumbo", "j1", "Johnnie Walker Blak Label 750ml")
    assert confidence(left, right) is MatchConfidence.FUZZY_SUPPORTED


def test_fuzzy_never_overrides_volume_or_expression_conflicts() -> None:
    black = observation("Coto", "c1", "Johnnie Walker Black Label 700 ml")
    wrong_volume = observation("Jumbo", "j1", "Johnnie Walker Black Label 750 ml")
    double = observation("Jumbo", "j2", "Johnnie Walker Double Black 700 ml")
    assert confidence(black, wrong_volume) is None
    assert confidence(black, double) is None


def test_pack_listing_remains_separate_from_single_with_multiple_ml_results() -> None:
    single = observation("Coto", "c1", "Johnnie Walker Black Label 750 ml")
    ml_single = observation("Mercado Libre Argentina", "MLA1", "Johnnie Walker Black Label 750 ml")
    ml_pack = observation(
        "Mercado Libre Argentina", "MLA6", "Johnnie Walker Black Label 750 ml x6", pack=6
    )
    result = ProductMatcher().match([single, ml_single, ml_pack])
    assert len(result.groups) == 1
    assert result.unmatched == (ml_pack,)


def test_manual_force_match_and_force_non_match() -> None:
    left = observation("Coto", "c1", "Mystery listing", brand=None, pack=None)
    right = observation("Jumbo", "j1", "Unknown bottle", brand=None, pack=None)
    pair = ManualOverrides.pair(
        ListingIdentity.from_observation(left), ListingIdentity.from_observation(right)
    )
    forced = ProductMatcher(overrides=ManualOverrides(force_match=frozenset((pair,))))
    assert forced.match([left, right]).groups[0].match_confidence is MatchConfidence.MANUAL

    black_a = observation("Coto", "c2", "Johnnie Walker Black Label 750 ml")
    black_b = observation("Jumbo", "j2", "Johnnie Walker Black Label 750 ml")
    blocked_pair = ManualOverrides.pair(
        ListingIdentity.from_observation(black_a), ListingIdentity.from_observation(black_b)
    )
    blocked = ProductMatcher(overrides=ManualOverrides(force_non_match=frozenset((blocked_pair,))))
    assert not blocked.match([black_a, black_b]).groups


def test_transitive_fuzzy_edges_cannot_merge_conflicting_label_families() -> None:
    black = observation(
        "Coto",
        "black-coto",
        "Whisky Johnnie Walker 12 Años 750 Ml Black Label",
        gtin="5000267024011",
    )
    black_jumbo = observation(
        "Jumbo",
        "black-jumbo",
        "Whisky Black Label 750 Ml Johnnie Walker",
        gtin="5000267024011",
    )
    blue = observation(
        "Coto",
        "blue-coto",
        "Whisky Blue Label JOHNNIE WALKER 750ml",
        gtin="5000267114279",
    )
    blue_jumbo = observation(
        "Jumbo",
        "blue-jumbo",
        "Whisky Blue Label 750 Ml Johnnie Walker Whisky Johnnie Walker Blue Label Botella",
        gtin="5000267114279",
    )
    result = ProductMatcher().match((black, black_jumbo, blue, blue_jumbo))
    assert len(result.groups) == 2
    titles = [{item.title for item in group.observations} for group in result.groups]
    assert all(
        not (
            any("Black" in title for title in group_titles)
            and any("Blue" in title for title in group_titles)
        )
        for group_titles in titles
    )
