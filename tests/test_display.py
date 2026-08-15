from datetime import UTC, datetime
from decimal import Decimal

from whisky_tracker.display import display_brand, display_product_name, display_retailer
from whisky_tracker.matching import CanonicalProduct
from whisky_tracker.models import ProductObservation, RetailerContext


def observation(title: str, brand: str) -> ProductObservation:
    return ProductObservation(
        retailer="Coto",
        retailer_product_id="prod00602091",
        retailer_sku_id="sku00602091",
        catalog_product_id=None,
        gtin="5099873038758",
        title=title,
        brand=brand,
        size_value=Decimal(700),
        size_unit="ml",
        pack_count=1,
        currency="ARS",
        current_price=Decimal(50960),
        regular_price=Decimal(78400),
        promotions=(),
        condition=None,
        available_quantity=1,
        in_stock=True,
        product_url="https://www.coto.com.ar/productos/_/R-00602091-00602091-200",
        context=RetailerContext(),
        observed_at=datetime(2026, 8, 13, tzinfo=UTC),
    )


def test_known_brand_display_preserves_punctuation() -> None:
    assert display_brand("jack daniels") == "Jack Daniel's"
    assert display_brand("j b") == "J&B"
    assert display_brand("johnnie walker") == "Johnnie Walker"


def test_gentleman_jack_display_retains_expression() -> None:
    item = observation("Whisky Gentleman Jack Daniels 700ml", "JACK DANIELS")
    product = CanonicalProduct(
        "gentleman",
        "jack daniels",
        "gentleman",
        None,
        700,
        1,
        frozenset(("5099873038758",)),
    )
    assert display_product_name(product, item) == "Jack Daniel's Gentleman Jack 700 ml"


def test_retailer_display_hides_internal_generic_and_default_context() -> None:
    assert display_retailer("Coto", RetailerContext(store_name="Coto Digital default")) == "Coto"
    assert display_retailer("jumbo", RetailerContext()) == "Jumbo"


def test_display_removes_redundant_brand_and_bottle_noise() -> None:
    item = observation("Whisky J&B en botella 750 ml", "J&B")
    product = CanonicalProduct("jb", "j b", "j b", None, 750, 1, frozenset())
    assert display_product_name(product, item) == "J&B 750 ml"

    old_parr = CanonicalProduct(
        "old-parr", "grand old parr", "old parr botella", None, 750, 1, frozenset()
    )
    assert display_product_name(old_parr, item) == "Grand Old Parr 750 ml"


def test_jb_display_suppresses_generic_blended_category_wording() -> None:
    item = observation("Whisky Blended Scotch J&B 750ml", "J&B")
    product = CanonicalProduct("jb", "j b", "j b", None, 750, 1, frozenset())

    assert display_product_name(product, item) == "J&B 750 ml"


def test_blenders_pride_display_suppresses_scoped_brand_lineage_noise() -> None:
    item = observation("Whisky Seagram's Blenders Pride 200ml", "BLENDERS")
    product = CanonicalProduct("blenders-pride", None, None, None, 200, 1, frozenset())

    assert display_product_name(product, item) == "Blenders Pride 200 ml"


def test_blenders_americano_display_normalizes_live_retailer_wording() -> None:
    item = observation("Whisky Estilo America BLENDERS Bot 750 Ml", "BLENDERS")
    product = CanonicalProduct(
        "blenders-americano", "blenders", "blenders", None, 750, 1, frozenset()
    )

    assert display_product_name(product, item) == "Blenders Americano 750 ml"


def test_scoped_blenders_cleanup_leaves_unrelated_expressions_unchanged() -> None:
    item = observation("Whisky Golden Blue Blenders 750 ml", "BLENDERS")
    product = CanonicalProduct(
        "blenders-golden-blue", "blenders", "golden blue", None, 750, 1, frozenset()
    )

    assert display_product_name(product, item) == "Blenders Golden Blue 750 ml"

    unrelated = CanonicalProduct(
        "seagrams-pride", "seagrams", "seagram s pride", None, 750, 1, frozenset()
    )
    assert display_product_name(unrelated, item) == "Seagrams Seagram S Pride 750 ml"


def test_jack_daniels_flavor_names_are_human_ordered() -> None:
    item = observation("Whisky Honey Jack Daniel 700ml", "JACK DANIELS")
    product = CanonicalProduct("honey", "jack daniels", "honey jack", None, 700, 1, frozenset())
    assert display_product_name(product, item) == "Jack Daniel's Tennessee Honey 700 ml"


def test_display_removes_reordered_or_repeated_brand_tokens() -> None:
    item = observation("Whisky Extra Chivas Regal 700 ml", "Chivas Regal")
    product = CanonicalProduct(
        "chivas-extra", "chivas regal", "extra chivas regal", None, 700, 1, frozenset()
    )
    assert display_product_name(product, item) == "Chivas Regal Extra 700 ml"


def test_chivas_regal_xv_display_removes_scoped_catalog_noise() -> None:
    item = observation("Whisky Xv Clear Chivas Regal 700 Ml", "Chivas Regal")
    product = CanonicalProduct(
        "chivas-xv",
        "chivas regal",
        "xv clear chivas",
        15,
        700,
        1,
        frozenset(("5000299622049",)),
    )

    assert display_product_name(product, item) == "Chivas Regal XV 15 Years 700 ml"


def test_chivas_regal_xv_uses_listing_expression_when_canonical_only_repeats_brand() -> None:
    item = observation("Whisky Xv Clear Chivas Regal 700 Ml", "Chivas Regal")
    product = CanonicalProduct(
        "chivas-xv", "chivas regal", "chivas", 15, 700, 1, frozenset(("5000299622049",))
    )

    assert display_product_name(product, item) == "Chivas Regal XV 15 Years 700 ml"


def test_chivas_regal_xv_roman_numeral_is_consistently_formatted() -> None:
    item = observation("Whisky Chivas Regal XV 15 Years 700 Ml", "Chivas Regal")

    for expression in ("xv", "Xv", "XV"):
        product = CanonicalProduct(
            "chivas-xv",
            "chivas regal",
            expression,
            15,
            700,
            1,
            frozenset(("5000299622049",)),
        )
        assert display_product_name(product, item) == "Chivas Regal XV 15 Years 700 ml"


def test_singleton_age_statement_is_displayed() -> None:
    item = observation("Whisky 12 Años Singleton Bot 700 Ml", "Singleton")
    product = CanonicalProduct("singleton-12", "singleton", None, 12, 700, 1, frozenset())
    assert display_product_name(product, item) == "Singleton 12 Years 700 ml"


def test_display_does_not_invent_missing_age() -> None:
    item = observation("Whisky Singleton 700 Ml", "Singleton")
    product = CanonicalProduct("singleton", "singleton", None, None, 700, 1, frozenset())
    assert display_product_name(product, item) == "Singleton 700 ml"
