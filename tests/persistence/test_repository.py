from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from whisky_tracker.matching import (
    CanonicalProduct,
    MatchConfidence,
    MatchingResult,
    ProductMatchGroup,
)
from whisky_tracker.models import (
    ContextResolution,
    DiscountType,
    FulfillmentMode,
    ProductObservation,
    Promotion,
    PromotionKind,
    RetailerContext,
)
from whisky_tracker.persistence import (
    HistoryFilter,
    ListingKey,
    PersistenceError,
    SQLiteRepository,
)

GENERIC = RetailerContext()
STORE = RetailerContext(
    fulfillment_mode=FulfillmentMode.REPRESENTATIVE_STORE,
    store_id="juramento",
    store_name="Market Juramento",
    sales_channel="market",
    context_resolution=ContextResolution.MANUALLY_SELECTED_STORE,
)
NOW = datetime(2026, 8, 10, 13, tzinfo=UTC)


def product(canonical_id: str = "jw-black-750", **changes: object) -> CanonicalProduct:
    values = {
        "canonical_id": canonical_id,
        "brand": "johnnie walker",
        "expression": "black label",
        "age_statement": 12,
        "volume_ml": 750,
        "pack_count": 1,
        "gtins": frozenset(("7790895000997",)),
    }
    values.update(changes)
    return CanonicalProduct(**values)  # type: ignore[arg-type]


def observation(
    product_id: str = "c1",
    *,
    retailer: str = "Carrefour",
    price: str = "48900.10",
    regular_price: str | None = "52000",
    observed_at: datetime = NOW,
    context: RetailerContext = GENERIC,
    promotions: tuple[Promotion, ...] = (),
) -> ProductObservation:
    return ProductObservation(
        retailer=retailer,
        retailer_product_id=product_id,
        retailer_sku_id=f"sku-{product_id}",
        catalog_product_id=f"catalog-{product_id}",
        gtin="7790895000997",
        title="Johnnie Walker Black Label 750 ml",
        brand="Johnnie Walker",
        size_value=Decimal(750),
        size_unit="ml",
        pack_count=1,
        currency="ARS",
        current_price=Decimal(price),
        regular_price=Decimal(regular_price) if regular_price else None,
        promotions=promotions,
        condition="new",
        available_quantity=3,
        in_stock=True,
        product_url=f"https://example.test/{product_id}",
        context=context,
        observed_at=observed_at,
    )


@pytest.fixture
def repository(tmp_path: Path) -> SQLiteRepository:
    with SQLiteRepository(tmp_path / "history.db") as result:
        result.initialize()
        yield result


def group(canonical: CanonicalProduct, *items: ProductObservation) -> ProductMatchGroup:
    return ProductMatchGroup(
        canonical_product=canonical,
        observations=items,
        match_confidence=MatchConfidence.STRONG_ATTRIBUTES,
        match_reason="test",
    )


def test_initialization_is_repeatable_and_versioned(tmp_path: Path) -> None:
    path = tmp_path / "new" / "tracker.db"
    with SQLiteRepository(path) as repository:
        repository.initialize()
        repository.initialize()
        assert repository.schema_version == 2
        versions = repository.connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[
            0
        ]
        assert versions == 2
    assert path.exists()


def test_migrates_existing_version_one_database(tmp_path: Path) -> None:
    from whisky_tracker.persistence.schema import MIGRATIONS

    with SQLiteRepository(tmp_path / "version-one.db") as repository:
        repository.connection.executescript(MIGRATIONS[0])
        repository.connection.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (1, ?)",
            (NOW.isoformat(),),
        )
        repository.connection.commit()
        repository.initialize()
        assert repository.schema_version == 2
        columns = {
            row["name"]
            for row in repository.connection.execute("PRAGMA table_info(retailer_listings)")
        }
        assert {"volume_ml", "pack_count"} <= columns


def test_canonical_upsert_deduplicates_and_enriches(repository: SQLiteRepository) -> None:
    incomplete = product(brand=None, expression=None, age_statement=None, gtins=frozenset())
    first_id = repository.upsert_canonical_product(incomplete)
    second_id = repository.upsert_canonical_product(product())
    assert first_id == second_id
    row = repository.connection.execute("SELECT * FROM canonical_products").fetchone()
    assert row["brand"] == "johnnie walker"
    assert row["expression"] == "black label"
    assert (
        repository.connection.execute("SELECT COUNT(*) FROM canonical_products").fetchone()[0] == 1
    )


def test_existing_gtin_reuses_persisted_canonical_identity(
    repository: SQLiteRepository,
) -> None:
    original = product("original-canonical")
    recomposed = product("different-group-derived-id", expression=None)
    original_pk = repository.upsert_canonical_product(original)
    recomposed_pk = repository.upsert_canonical_product(recomposed)
    assert recomposed_pk == original_pk
    assert repository.resolve_canonical_product(recomposed).canonical_id == "original-canonical"
    assert (
        repository.connection.execute("SELECT COUNT(*) FROM canonical_products").fetchone()[0] == 1
    )


def test_known_conflicting_expression_is_not_merged_by_gtin(
    repository: SQLiteRepository,
) -> None:
    repository.upsert_canonical_product(product(expression="black label"))
    with pytest.raises(ValueError, match="canonical expression"):
        repository.upsert_canonical_product(product("wrong-identity", expression="blue label"))


def test_matching_conflict_reports_sanitized_gtin_and_listing_identities(
    repository: SQLiteRepository,
) -> None:
    repository.upsert_canonical_product(product(volume_ml=700))
    live_item = replace(
        observation("live-product"),
        retailer_sku_id="live-sku",
        title="Johnnie\nWalker Black Label 750 ml",
    )

    with pytest.raises(PersistenceError) as error:
        repository.save_matching_result(
            MatchingResult((group(product("new-group", volume_ml=750), live_item),), ())
        )

    message = str(error.value)
    assert "known GTIN conflicts with persisted canonical volume_ml" in message
    assert "gtins=['7790895000997']" in message
    assert "retailer='Carrefour'" in message
    assert "product_id='live-product'" in message
    assert "sku_id='live-sku'" in message
    assert "title='Johnnie Walker Black Label 750 ml'" in message
    assert "https://" not in message


def test_listing_upsert_and_multiple_ml_listings_share_product(
    repository: SQLiteRepository,
) -> None:
    ml1 = observation("MLA1", retailer="Mercado Libre Argentina")
    ml2 = observation("MLA2", retailer="Mercado Libre Argentina")
    result = MatchingResult((group(product(), ml1, ml2),), ())
    repository.save_matching_result(result)
    repository.save_matching_result(result)
    assert (
        repository.connection.execute("SELECT COUNT(*) FROM retailer_listings").fetchone()[0] == 2
    )
    canonical_ids = repository.connection.execute(
        "SELECT DISTINCT canonical_product_id FROM retailer_listings"
    ).fetchall()
    assert len(canonical_ids) == 1


def test_observation_round_trip_promotions_and_exact_idempotency(
    repository: SQLiteRepository,
) -> None:
    promotion = Promotion(
        name="Club discount",
        kind=PromotionKind.LOYALTY,
        applied_to_current_price=True,
        discount_value=Decimal("15.25"),
        discount_type=DiscountType.PERCENTAGE,
        minimum_quantity=Decimal(2),
        conditions=("members only", "selected cards"),
    )
    item = observation(context=STORE, promotions=(promotion,))
    repository.save_observations((item, item))
    history = repository.get_price_history(
        HistoryFilter(listing=ListingKey("Carrefour", "c1", "sku-c1"), context=STORE)
    )
    assert len(history) == 1
    stored = history[0]
    assert stored.current_price == Decimal("48900.10")
    assert stored.regular_price == Decimal("52000")
    assert stored.in_stock is True
    assert stored.available_quantity == 3
    assert stored.context == STORE
    assert stored.promotions == (promotion,)


def test_later_unchanged_and_changed_prices_are_both_preserved(
    repository: SQLiteRepository,
) -> None:
    first = observation()
    second = replace(first, observed_at=NOW + timedelta(hours=4))
    third = replace(second, observed_at=NOW + timedelta(hours=8), current_price=Decimal("39999"))
    repository.save_observations((first, second, third))
    history = repository.get_price_history(HistoryFilter(retailer="Carrefour"))
    assert [item.current_price for item in history] == [
        Decimal("48900.10"),
        Decimal("48900.10"),
        Decimal("39999"),
    ]


def test_latest_previous_minimum_ranges_and_decimal_change(repository: SQLiteRepository) -> None:
    first = observation(price="48900")
    second = replace(first, observed_at=NOW + timedelta(days=1), current_price=Decimal("44500"))
    third = replace(first, observed_at=NOW + timedelta(days=2), current_price=Decimal("39999"))
    repository.save_observations((third, first, second))
    filters = HistoryFilter(
        listing=ListingKey("Carrefour", "c1", "sku-c1"),
        context=GENERIC,
        start=NOW + timedelta(hours=1),
    )
    previous = repository.get_previous_observation(filters)
    latest = repository.get_latest_price(filters)
    minimum = repository.get_historical_minimum(filters)
    assert previous is not None and previous.observed_at == second.observed_at
    assert latest is not None and latest.observed_at == third.observed_at
    assert minimum is not None and minimum.current_price == third.current_price
    change = repository.get_latest_price_change(filters)
    assert change is not None
    assert change.absolute == Decimal("-4501")
    assert change.percentage == Decimal("-4501") / Decimal("44500") * Decimal(100)


def test_contexts_create_separate_price_series(repository: SQLiteRepository) -> None:
    generic = observation(price="48000")
    store = observation(price="41000", context=STORE)
    other_channel = observation(
        price="39000", context=replace(STORE, sales_channel="scheduled-delivery")
    )
    repository.save_observations((generic, store, other_channel))
    key = ListingKey("Carrefour", "c1", "sku-c1")
    assert (
        repository.get_latest_price(HistoryFilter(listing=key, context=GENERIC))
        == repository.get_price_history(HistoryFilter(listing=key, context=GENERIC))[0]
    )
    assert repository.get_latest_price(
        HistoryFilter(listing=key, context=STORE)
    ).current_price == Decimal("41000")
    assert len(repository.get_price_history(HistoryFilter(listing=key))) == 3
    assert len(repository.get_price_history(HistoryFilter(store_id="juramento"))) == 2
    assert len(repository.get_price_history(HistoryFilter(sales_channel="market"))) == 1


def test_transient_coordinates_are_rejected_before_listing_context_or_fingerprint_persistence(
    repository: SQLiteRepository,
) -> None:
    transient = RetailerContext(
        fulfillment_mode=FulfillmentMode.DELIVERY,
        postal_code="test-postcode",
        coordinates=(12.345678, -45.678912),
    )
    item = observation(context=transient)

    with pytest.raises(PersistenceError, match="transient location coordinates"):
        repository.save_observations((item,))

    assert repository.observation_count() == 0
    listing_count = repository.connection.execute(
        "SELECT COUNT(*) FROM retailer_listings"
    ).fetchone()[0]
    assert listing_count == 0
    dump = "\n".join(repository.connection.iterdump())
    assert "12.345678" not in dump
    assert "-45.678912" not in dump


def test_resolved_stores_remain_distinct_without_coordinates(
    repository: SQLiteRepository,
) -> None:
    first_context = RetailerContext(
        fulfillment_mode=FulfillmentMode.DELIVERY,
        postal_code="test-postcode",
        sales_channel="32",
        seller_id="seller-a",
        store_id="store-a",
        region_id="region-a",
        context_resolution=ContextResolution.ADDRESS_RESOLVED,
    )
    second_context = replace(
        first_context,
        seller_id="seller-b",
        store_id="store-b",
        region_id="region-b",
    )
    first = observation(context=first_context)
    second = replace(first, observed_at=NOW + timedelta(hours=1), context=second_context)

    repository.save_observations((first, second))

    rows = repository.connection.execute(
        "SELECT longitude, latitude, context_key, seller_id, store_id FROM observations ORDER BY id"
    ).fetchall()
    assert all(row["longitude"] is None and row["latitude"] is None for row in rows)
    assert len({row["context_key"] for row in rows}) == 2
    assert {(row["seller_id"], row["store_id"]) for row in rows} == {
        ("seller-a", "store-a"),
        ("seller-b", "store-b"),
    }
    key = ListingKey("Carrefour", "c1", "sku-c1")
    assert (
        repository.get_latest_price(HistoryFilter(listing=key, context=first_context)).context
        == first_context
    )
    assert (
        repository.get_latest_price(HistoryFilter(listing=key, context=second_context)).context
        == second_context
    )


def test_unmatched_history_survives_later_canonical_association(
    repository: SQLiteRepository,
) -> None:
    unmatched = observation()
    repository.save_matching_result(MatchingResult((), (unmatched,)))
    partner = observation("j1", retailer="Jumbo", observed_at=NOW + timedelta(hours=1))
    repository.save_matching_result(MatchingResult((group(product(), unmatched, partner),), ()))
    canonical_history = repository.get_price_history(HistoryFilter(canonical_id="jw-black-750"))
    assert {item.listing.retailer for item in canonical_history} == {"Carrefour", "Jumbo"}
    assert len(canonical_history) == 2


def test_batch_rolls_back_on_conflicting_listing_assignment(repository: SQLiteRepository) -> None:
    item = observation()
    repository.save_matching_result(MatchingResult((group(product("first"), item),), ()))
    before = repository.connection.total_changes
    new_item = observation("j1", retailer="Jumbo")
    bad_result = MatchingResult(
        (
            group(product("new-product", gtins=frozenset()), new_item),
            group(product("conflict", gtins=frozenset()), item),
        ),
        (),
    )
    with pytest.raises(PersistenceError, match="already assigned"):
        repository.save_matching_result(bad_result)
    assert (
        repository.connection.execute(
            "SELECT COUNT(*) FROM canonical_products WHERE canonical_id = 'new-product'"
        ).fetchone()[0]
        == 0
    )
    assert (
        repository.connection.execute(
            "SELECT COUNT(*) FROM retailer_listings WHERE retailer = 'Jumbo'"
        ).fetchone()[0]
        == 0
    )
    assert repository.connection.total_changes > before  # SQLite counts rolled-back attempts too.
