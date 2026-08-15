from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from whisky_tracker.alerts import (
    AlertConfig,
    AlertEngine,
    AlertType,
    alert_priority_key,
    format_alert,
)
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
from whisky_tracker.persistence import PersistenceError, SQLiteRepository

NOW = datetime(2026, 8, 10, 13, tzinfo=UTC)
GENERIC = RetailerContext()
MARKET = RetailerContext(
    fulfillment_mode=FulfillmentMode.SCHEDULED_DELIVERY,
    postal_code="1428",
    region_id="ar-ba",
    seller_id="200",
    store_id="juramento",
    store_name="Market Juramento",
    context_resolution=ContextResolution.POSTCODE_RESOLVED,
)
PRODUCT = CanonicalProduct(
    canonical_id="jw-black-750",
    brand="johnnie walker",
    expression="black label",
    age_statement=12,
    volume_ml=750,
    pack_count=1,
    gtins=frozenset(("7790895000997",)),
)


def observation(
    price: str,
    *,
    at: datetime = NOW,
    retailer: str = "Carrefour",
    product_id: str = "c1",
    context: RetailerContext = GENERIC,
    currency: str = "ARS",
    volume: int = 750,
    pack: int = 1,
    in_stock: bool = True,
    promotions: tuple[Promotion, ...] = (),
    regular_price: str | None = None,
) -> ProductObservation:
    return ProductObservation(
        retailer=retailer,
        retailer_product_id=product_id,
        retailer_sku_id=f"sku-{product_id}",
        catalog_product_id=None,
        gtin="7790895000997",
        title=f"Johnnie Walker Black Label {volume} ml",
        brand="Johnnie Walker",
        size_value=Decimal(volume),
        size_unit="ml",
        pack_count=pack,
        currency=currency,
        current_price=Decimal(price),
        regular_price=Decimal(regular_price) if regular_price else None,
        promotions=promotions,
        condition="new",
        available_quantity=1 if in_stock else 0,
        in_stock=in_stock,
        product_url=f"https://example.test/{product_id}",
        context=context,
        observed_at=at,
    )


@pytest.fixture
def repository(tmp_path: Path) -> SQLiteRepository:
    with SQLiteRepository(tmp_path / "alerts.db") as result:
        result.initialize()
        yield result


def save(repository: SQLiteRepository, *items: ProductObservation) -> None:
    group = ProductMatchGroup(PRODUCT, items, MatchConfidence.STRONG_ATTRIBUTES, "test")
    repository.save_matching_result(MatchingResult((group,), ()))


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ("50000", "45500", False),
        ("50000", "45000", True),
        ("50000", "44000", True),
        ("50000", "51000", False),
        ("50000", "50000", False),
    ],
)
def test_price_drop_thresholds(
    repository: SQLiteRepository, previous: str, current: str, expected: bool
) -> None:
    baseline = observation(previous)
    latest = observation(current, at=NOW + timedelta(hours=1))
    save(repository, baseline, latest)
    alert = AlertEngine(repository).evaluate_observation(latest, canonical_product=PRODUCT)
    assert (alert is not None and AlertType.PRICE_DROP in alert.alert_types) is expected


def test_threshold_configuration_is_injectable(repository: SQLiteRepository) -> None:
    baseline = observation("50000")
    latest = observation("45500", at=NOW + timedelta(hours=1))
    save(repository, baseline, latest)
    engine = AlertEngine(repository, config=AlertConfig(minimum_price_drop_percentage=Decimal("9")))
    alert = engine.evaluate_observation(latest, canonical_product=PRODUCT)
    assert alert is not None and AlertType.PRICE_DROP in alert.alert_types


def test_default_promotion_threshold_is_25_percent() -> None:
    assert AlertConfig().minimum_promotion_discount_percentage == Decimal("25")


def test_standalone_sub_25_promotion_does_not_alert_but_threshold_is_configurable(
    repository: SQLiteRepository,
) -> None:
    promotion = Promotion(
        "Club", PromotionKind.LOYALTY, False, Decimal("20"), DiscountType.PERCENTAGE
    )
    item = observation("50000", promotions=(promotion,))
    save(repository, item)
    assert AlertEngine(repository).evaluate_observation(item, canonical_product=PRODUCT) is None
    configured = AlertEngine(
        repository,
        config=AlertConfig(minimum_promotion_discount_percentage=Decimal("20")),
    )
    alert = configured.evaluate_observation(item, canonical_product=PRODUCT)
    assert alert is not None and AlertType.PROMOTION in alert.alert_types


def test_sub_25_promotion_is_shown_on_combined_cross_retailer_alert(
    repository: SQLiteRepository,
) -> None:
    promotion = Promotion(
        "Club", PromotionKind.LOYALTY, False, Decimal("20"), DiscountType.PERCENTAGE
    )
    candidate = observation("45000", promotions=(promotion,))
    competitor = observation("50000", retailer="Coto", product_id="co1")
    save(repository, candidate, competitor)
    alert = AlertEngine(repository).evaluate_observation(candidate, canonical_product=PRODUCT)
    assert alert is not None
    assert alert.alert_types == {AlertType.CROSS_RETAILER_DEAL}
    assert alert.qualifying_promotions[0].discount_percentage == Decimal("20")
    assert "20% de descuento" in format_alert(alert)


def test_historical_low_uses_only_prior_observations(repository: SQLiteRepository) -> None:
    first = observation("50000")
    save(repository, first)
    assert AlertEngine(repository).evaluate_observation(first, canonical_product=PRODUCT) is None

    lower = observation("47000", at=NOW + timedelta(hours=1))
    save(repository, lower)
    alert = AlertEngine(repository).evaluate_observation(lower, canonical_product=PRODUCT)
    assert alert is not None and AlertType.HISTORICAL_LOW in alert.alert_types
    assert alert.historical_minimum == Decimal("50000")

    equal = observation("47000", at=NOW + timedelta(hours=2))
    save(repository, equal)
    equal_alert = AlertEngine(repository).evaluate_observation(equal, canonical_product=PRODUCT)
    assert equal_alert is None or AlertType.HISTORICAL_LOW not in equal_alert.alert_types


def test_context_histories_do_not_mix(repository: SQLiteRepository) -> None:
    generic = observation("50000")
    located = observation("44000", at=NOW + timedelta(hours=1), context=MARKET)
    save(repository, generic, located)
    alert = AlertEngine(repository).evaluate_observation(located, canonical_product=PRODUCT)
    assert alert is None


def test_cross_retailer_deal_selects_cheapest_at_threshold(
    repository: SQLiteRepository,
) -> None:
    coto = observation("50000", retailer="Coto", product_id="co1")
    carrefour = observation("45000", retailer="Carrefour", product_id="ca1", context=MARKET)
    save(repository, coto, carrefour)
    alert = AlertEngine(repository).evaluate_observation(carrefour, canonical_product=PRODUCT)
    assert alert is not None
    assert AlertType.CROSS_RETAILER_DEAL in alert.alert_types
    assert {price.retailer for price in alert.comparison_prices} == {"Coto", "Carrefour"}


def test_cross_retailer_below_threshold_does_not_trigger(repository: SQLiteRepository) -> None:
    save(
        repository,
        observation("49000", retailer="Coto", product_id="co1"),
        observation("45000", retailer="Carrefour", product_id="ca1"),
    )
    candidate = observation("45000", retailer="Carrefour", product_id="ca1")
    assert (
        AlertEngine(repository).evaluate_observation(candidate, canonical_product=PRODUCT) is None
    )


@pytest.mark.parametrize(
    "competitor",
    [
        observation("50000", retailer="Coto", product_id="volume", volume=1000),
        observation("50000", retailer="Coto", product_id="pack", pack=6),
        observation("50000", retailer="Coto", product_id="currency", currency="USD"),
        observation("50000", retailer="Coto", product_id="stock", in_stock=False),
    ],
)
def test_incompatible_cross_retailer_offers_are_ignored(
    repository: SQLiteRepository, competitor: ProductObservation
) -> None:
    candidate = observation("40000", retailer="Carrefour", product_id="ca1")
    save(repository, competitor, candidate)
    alert = AlertEngine(repository).evaluate_observation(candidate, canonical_product=PRODUCT)
    assert alert is None


def test_multiple_ml_listings_use_best_offer_but_not_each_other(
    repository: SQLiteRepository,
) -> None:
    candidate = observation("39000", retailer="Carrefour", product_id="ca1")
    save(
        repository,
        candidate,
        observation("47000", retailer="Mercado Libre Argentina", product_id="MLA1"),
        observation("45000", retailer="Mercado Libre Argentina", product_id="MLA2"),
    )
    alert = AlertEngine(repository).evaluate_observation(candidate, canonical_product=PRODUCT)
    assert alert is not None
    ml = [item for item in alert.comparison_prices if item.retailer == "Mercado Libre Argentina"]
    assert len(ml) == 1 and ml[0].price == Decimal("45000")


@pytest.mark.parametrize(
    "kind",
    [PromotionKind.GENERAL, PromotionKind.LOYALTY, PromotionKind.PAYMENT_METHOD],
)
def test_qualifying_structured_promotions(
    repository: SQLiteRepository, kind: PromotionKind
) -> None:
    promotion = Promotion(
        name="Special offer",
        kind=kind,
        applied_to_current_price=False,
        discount_value=Decimal("25"),
        discount_type=DiscountType.PERCENTAGE,
        conditions=("eligible customers",),
    )
    item = observation("50000", promotions=(promotion,))
    save(repository, item)
    alert = AlertEngine(repository).evaluate_observation(item, canonical_product=PRODUCT)
    assert alert is not None and alert.alert_types == {AlertType.PROMOTION}
    assert alert.qualifying_promotions[0].conditional is (kind is not PromotionKind.GENERAL)


def test_unknown_promotion_discount_and_regular_price_gap_do_not_trigger(
    repository: SQLiteRepository,
) -> None:
    unknown = Promotion("Mystery", PromotionKind.COUPON, False)
    item = observation("40000", regular_price="50000", promotions=(unknown,))
    save(repository, item)
    assert AlertEngine(repository).evaluate_observation(item, canonical_product=PRODUCT) is None


def test_out_of_stock_candidate_does_not_alert(repository: SQLiteRepository) -> None:
    item = observation(
        "40000",
        in_stock=False,
        promotions=(
            Promotion("Sale", PromotionKind.GENERAL, True, Decimal("25"), DiscountType.PERCENTAGE),
        ),
    )
    save(repository, item)
    assert AlertEngine(repository).evaluate_observation(item, canonical_product=PRODUCT) is None


def test_pending_alert_retries_sent_alert_deduplicates_and_lower_price_is_new(
    repository: SQLiteRepository,
) -> None:
    baseline = observation("50000")
    lower = observation("44000", at=NOW + timedelta(hours=1))
    save(repository, baseline, lower)
    engine = AlertEngine(repository)
    first = engine.evaluate_observation(lower, canonical_product=PRODUCT)
    assert first is not None
    assert engine.evaluate_observation(lower, canonical_product=PRODUCT) is not None
    engine.mark_sent(first)
    assert engine.evaluate_observation(lower, canonical_product=PRODUCT) is None

    much_lower = observation("39000", at=NOW + timedelta(hours=2))
    save(repository, much_lower)
    assert engine.evaluate_observation(much_lower, canonical_product=PRODUCT) is not None


def test_same_sent_promotion_at_later_time_is_suppressed(
    repository: SQLiteRepository,
) -> None:
    promotion = Promotion(
        "Club", PromotionKind.LOYALTY, False, Decimal("25"), DiscountType.PERCENTAGE
    )
    first = observation("50000", promotions=(promotion,))
    save(repository, first)
    engine = AlertEngine(repository)
    alert = engine.evaluate_observation(first, canonical_product=PRODUCT)
    assert alert is not None
    engine.mark_sent(alert)
    later = replace(first, observed_at=NOW + timedelta(hours=1))
    save(repository, later)
    assert engine.evaluate_observation(later, canonical_product=PRODUCT) is None


def test_synthetic_price_alert_lifecycle_smoke(repository: SQLiteRepository) -> None:
    baseline = observation("50000")
    first_drop = observation("44000", at=NOW + timedelta(hours=1))
    save(repository, baseline, first_drop)
    engine = AlertEngine(repository)
    alert = engine.evaluate_observation(first_drop, canonical_product=PRODUCT)
    assert alert is not None
    assert {AlertType.PRICE_DROP, AlertType.HISTORICAL_LOW} <= alert.alert_types
    assert alert.percentage_change == Decimal("-12")
    engine.mark_sent(alert)

    unchanged = observation("44000", at=NOW + timedelta(hours=2))
    save(repository, unchanged)
    assert engine.evaluate_observation(unchanged, canonical_product=PRODUCT) is None

    second_drop = observation("39000", at=NOW + timedelta(hours=3))
    save(repository, second_drop)
    next_alert = engine.evaluate_observation(second_drop, canonical_product=PRODUCT)
    assert next_alert is not None
    assert {AlertType.PRICE_DROP, AlertType.HISTORICAL_LOW} <= next_alert.alert_types


def test_new_alert_type_changes_semantic_fingerprint(repository: SQLiteRepository) -> None:
    baseline = observation("50000")
    lower = observation("44000", at=NOW + timedelta(hours=1))
    save(repository, baseline, lower)
    engine = AlertEngine(repository)
    initial = engine.evaluate_observation(lower, canonical_product=PRODUCT)
    assert initial is not None
    engine.mark_sent(initial)
    promotion = Promotion(
        "Club", PromotionKind.LOYALTY, False, Decimal("25"), DiscountType.PERCENTAGE
    )
    promoted = replace(lower, promotions=(promotion,))
    save(repository, promoted)
    changed = engine.evaluate_observation(promoted, canonical_product=PRODUCT)
    assert changed is not None and AlertType.PROMOTION in changed.alert_types


def test_alert_persistence_uniqueness_and_failed_mark_is_transactional(
    repository: SQLiteRepository,
) -> None:
    promotion = Promotion(
        "Sale", PromotionKind.GENERAL, True, Decimal("25"), DiscountType.PERCENTAGE
    )
    item = observation("50000", promotions=(promotion,))
    save(repository, item)
    engine = AlertEngine(repository)
    alert = engine.evaluate_observation(item, canonical_product=PRODUCT)
    assert alert is not None
    engine.evaluate_observation(item, canonical_product=PRODUCT)
    count = repository.connection.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0]
    assert count == 1
    with pytest.raises(PersistenceError, match="does not exist"):
        repository.mark_alert_sent("missing")
    assert not repository.is_alert_sent(alert.fingerprint)


def test_formatter_labels_resolved_and_generic_contexts(repository: SQLiteRepository) -> None:
    candidate = observation("39000", retailer="Carrefour", product_id="ca1", context=MARKET)
    jumbo = observation("45000", retailer="Jumbo", product_id="j1")
    save(repository, candidate, jumbo)
    alert = AlertEngine(repository).evaluate_observation(candidate, canonical_product=PRODUCT)
    assert alert is not None
    message = format_alert(alert)
    assert "Market Juramento" in message
    assert "Jumbo: $45.000" in message
    assert "genérico" not in message
    assert "Belgrano" not in message


def test_formatter_preserves_unknown_promotion_without_percentage(
    repository: SQLiteRepository,
) -> None:
    baseline = observation("50000")
    unknown = Promotion("Cupón sorpresa", PromotionKind.COUPON, False)
    latest = observation("44000", at=NOW + timedelta(hours=1), promotions=(unknown,))
    save(repository, baseline, latest)
    alert = AlertEngine(repository).evaluate_observation(latest, canonical_product=PRODUCT)
    assert alert is not None
    message = format_alert(alert)
    assert "🥃 Cupón sorpresa" in message
    assert "descuento no cuantificado" not in message
    assert "% off" not in message


def test_coto_promotion_is_human_readable_and_hides_raw_fields(
    repository: SQLiteRepository,
) -> None:
    promotion = Promotion(
        "35%Dto",
        PromotionKind.GENERAL,
        True,
        Decimal("35"),
        DiscountType.PERCENTAGE,
        conditions=(
            "id=36969076",
            "comments=No acumulable con otras promos",
            "regularPriceText=Precio Contado: $78400",
            "store=200",
        ),
    )
    item = observation("50960", retailer="Coto", promotions=(promotion,), regular_price="78400")
    save(repository, item)
    alert = AlertEngine(repository).evaluate_observation(item, canonical_product=PRODUCT)
    assert alert is not None
    message = format_alert(alert)
    assert "🥃 35% de descuento" in message
    assert "Precio regular: $78.400" in message
    assert "No acumulable con otras promociones" in message
    for raw in ("id=", "store=", "comments=", "regularPriceText"):
        assert raw not in message


def test_argentina_price_and_percentage_formatting_uses_local_separators(
    repository: SQLiteRepository,
) -> None:
    baseline = observation("50000")
    latest = observation("41558.4", at=NOW + timedelta(hours=1))
    save(repository, baseline, latest)
    alert = AlertEngine(repository).evaluate_observation(latest, canonical_product=PRODUCT)
    assert alert is not None
    message = format_alert(alert)
    assert "Ahora: $41.558,40" in message
    assert "Bajó: 16,88%" in message


def test_redundant_single_installment_payment_metadata_is_suppressed(
    repository: SQLiteRepository,
) -> None:
    payment = Promotion(
        "Promoción con medio de pago",
        PromotionKind.PAYMENT_METHOD,
        False,
        conditions=(
            "installments=1",
            "installment_price=$50960",
            "image=/content/card.png",
            "store=200",
        ),
    )
    baseline = observation("60000", retailer="Coto")
    latest = observation(
        "50960",
        at=NOW + timedelta(hours=1),
        retailer="Coto",
        promotions=(payment,),
    )
    save(repository, baseline, latest)
    alert = AlertEngine(repository).evaluate_observation(latest, canonical_product=PRODUCT)
    assert alert is not None
    message = format_alert(alert)
    for raw in ("payment_method", "installment_price", "image=", "store="):
        assert raw not in message
    assert "medio de pago" not in message


def test_useful_payment_discount_is_displayed_without_debug_metadata(
    repository: SQLiteRepository,
) -> None:
    payment = Promotion(
        "Tarjeta Carrefour",
        PromotionKind.PAYMENT_METHOD,
        False,
        Decimal("15"),
        DiscountType.PERCENTAGE,
        conditions=("image=/content/card.png", "store=200"),
    )
    candidate = observation("44000", promotions=(payment,))
    competitor = observation("50000", retailer="Coto", product_id="co1")
    save(repository, candidate, competitor)
    alert = AlertEngine(repository).evaluate_observation(candidate, canonical_product=PRODUCT)
    assert alert is not None
    message = format_alert(alert)
    assert "💳 15% con medio de pago elegible" in message
    assert "adicional" not in message
    assert "15%" in message
    assert "image=" not in message and "store=" not in message


def test_formatter_uses_safe_friendly_html_link(repository: SQLiteRepository) -> None:
    item = replace(
        observation("50000", retailer="Coto"),
        product_url='https://example.test/item?a=1&name="rare"',
    )
    promotion = Promotion(
        "Club <Especial>",
        PromotionKind.LOYALTY,
        False,
        Decimal("25"),
        DiscountType.PERCENTAGE,
    )
    item = replace(item, promotions=(promotion,))
    save(repository, item)
    alert = AlertEngine(repository).evaluate_observation(item, canonical_product=PRODUCT)
    assert alert is not None
    message = format_alert(alert)
    expected_link = (
        '🔗 <a href="https://example.test/item?a=1&amp;name=&quot;rare&quot;">Ver en Coto</a>'
    )
    assert expected_link in message
    assert "Club &lt;Especial&gt;" in message


def test_candidate_ordering_uses_explicit_signal_tiers(repository: SQLiteRepository) -> None:
    promotion = Promotion(
        "Sale", PromotionKind.GENERAL, True, Decimal("25"), DiscountType.PERCENTAGE
    )
    item = observation("50000", promotions=(promotion,))
    save(repository, item)
    base = AlertEngine(repository).evaluate_observation(item, canonical_product=PRODUCT)
    assert base is not None
    promotion_only = replace(base, alert_types=frozenset((AlertType.PROMOTION,)))
    historical = replace(base, alert_types=frozenset((AlertType.HISTORICAL_LOW,)))
    price_drop = replace(base, alert_types=frozenset((AlertType.PRICE_DROP,)))
    cross_combined = replace(
        base,
        alert_types=frozenset((AlertType.CROSS_RETAILER_DEAL, AlertType.PROMOTION)),
    )
    strongest = replace(
        base,
        alert_types=frozenset((AlertType.HISTORICAL_LOW, AlertType.PRICE_DROP)),
    )
    ranked = sorted(
        (promotion_only, price_drop, historical, cross_combined, strongest),
        key=alert_priority_key,
    )
    assert ranked == [strongest, cross_combined, price_drop, historical, promotion_only]


def test_product_alert_consolidates_retailers_and_keeps_signal_ownership(
    repository: SQLiteRepository,
) -> None:
    coto_promotion = Promotion(
        "35%Dto", PromotionKind.GENERAL, True, Decimal("35"), DiscountType.PERCENTAGE
    )
    carrefour_promotion = Promotion(
        "25%", PromotionKind.GENERAL, True, Decimal("25"), DiscountType.PERCENTAGE
    )
    coto = observation("31200", retailer="Coto", product_id="co", promotions=(coto_promotion,))
    carrefour = observation(
        "36839.25",
        retailer="Carrefour",
        product_id="ca",
        context=MARKET,
        promotions=(carrefour_promotion,),
    )
    jumbo = observation("49500", retailer="Jumbo", product_id="ju")
    save(repository, coto, carrefour, jumbo)

    alert = AlertEngine(repository).evaluate_product(PRODUCT, (coto, carrefour, jumbo))

    assert alert is not None
    assert [offer.observation.retailer for offer in alert.offers] == [
        "Coto",
        "Carrefour",
        "Jumbo",
    ]
    assert alert.best_offer.observation is coto
    assert alert.savings_amount == Decimal("5639.25")
    assert AlertType.PROMOTION in alert.offers[0].alert_types
    assert AlertType.PROMOTION in alert.offers[1].alert_types
    assert alert.offers[2].alert_types == frozenset()
    assert AlertType.CROSS_RETAILER_DEAL in alert.offers[0].alert_types
    message = format_alert(alert)
    assert message.count("Johnnie Walker Black Label 12 Years 750 ml") == 1
    assert "🏆 <b>Mejor precio</b>\nCoto: $31.200" in message
    assert "Carrefour — Market Juramento: $36.839,25" in message
    assert "Jumbo: $49.500" in message
    assert "Ahorrás $5.639,25 vs. Carrefour — Market Juramento" in message
    assert message.count("🔗") == 3
    for raw in ("seller_id", "region_id", "store=", "generic"):
        assert raw not in message


def test_product_alert_historical_signals_stay_with_their_offer(
    repository: SQLiteRepository,
) -> None:
    coto_before = observation("50000", retailer="Coto", product_id="co")
    carrefour_before = observation("60000", retailer="Carrefour", product_id="ca")
    save(repository, coto_before, carrefour_before)
    coto = observation("49000", at=NOW + timedelta(hours=1), retailer="Coto", product_id="co")
    carrefour = observation(
        "50000", at=NOW + timedelta(hours=1), retailer="Carrefour", product_id="ca"
    )
    save(repository, coto, carrefour)

    alert = AlertEngine(repository).evaluate_product(PRODUCT, (coto, carrefour))

    assert alert is not None
    by_retailer = {offer.observation.retailer: offer for offer in alert.offers}
    assert by_retailer["Carrefour"].alert_types == {
        AlertType.PRICE_DROP,
        AlertType.HISTORICAL_LOW,
    }
    assert by_retailer["Coto"].alert_types == {AlertType.HISTORICAL_LOW}
    message = format_alert(alert)
    coto_section, carrefour_section = message.split("\n\nCarrefour:")
    assert "Bajó" not in coto_section
    assert "Bajó desde $60.000" in carrefour_section


def test_product_offer_selection_ignores_unavailable_currency_and_duplicate_listings(
    repository: SQLiteRepository,
) -> None:
    promotion = Promotion(
        "Sale", PromotionKind.GENERAL, True, Decimal("30"), DiscountType.PERCENTAGE
    )
    coto = observation("40000", retailer="Coto", product_id="co", promotions=(promotion,))
    unavailable = observation(
        "10000", retailer="Jumbo", product_id="out", in_stock=False, promotions=(promotion,)
    )
    usd = observation("10", retailer="Mercado Libre Argentina", product_id="usd", currency="USD")
    ml_expensive = observation(
        "48000", retailer="Mercado Libre Argentina", product_id="ml-expensive"
    )
    ml_best = observation("45000", retailer="Mercado Libre Argentina", product_id="ml-best")
    save(repository, coto, unavailable, usd, ml_expensive, ml_best)

    alert = AlertEngine(repository).evaluate_product(
        PRODUCT, (coto, unavailable, usd, ml_expensive, ml_best)
    )

    assert alert is not None
    assert len(alert.current_observations) == 5
    assert [
        (offer.observation.retailer, offer.observation.current_price) for offer in alert.offers
    ] == [
        ("Coto", Decimal("40000")),
        ("Mercado Libre Argentina", Decimal("45000")),
    ]


def test_product_fingerprint_tracks_material_state_but_not_timestamps(
    repository: SQLiteRepository,
) -> None:
    promotion = Promotion(
        "Sale", PromotionKind.GENERAL, True, Decimal("30"), DiscountType.PERCENTAGE
    )
    first = observation("40000", retailer="Coto", product_id="co", promotions=(promotion,))
    comparison = observation("50000", retailer="Jumbo", product_id="ju")
    save(repository, first, comparison)
    engine = AlertEngine(repository)
    initial = engine.evaluate_product(PRODUCT, (first, comparison))
    assert initial is not None
    engine.mark_sent(initial)

    later = replace(first, observed_at=NOW + timedelta(hours=1))
    later_comparison = replace(comparison, observed_at=NOW + timedelta(hours=1))
    save(repository, later, later_comparison)
    assert engine.evaluate_product(PRODUCT, (later, later_comparison)) is None

    changed_price = replace(later_comparison, current_price=Decimal("43000"))
    save(repository, changed_price)
    changed = engine.evaluate_product(PRODUCT, (later, changed_price))
    assert changed is not None and changed.fingerprint != initial.fingerprint


def test_product_fingerprint_changes_with_best_retailer_promotion_or_composition(
    repository: SQLiteRepository,
) -> None:
    promotion = Promotion(
        "Sale", PromotionKind.GENERAL, True, Decimal("30"), DiscountType.PERCENTAGE
    )
    coto = observation("40000", retailer="Coto", product_id="co", promotions=(promotion,))
    jumbo = observation("50000", retailer="Jumbo", product_id="ju")
    save(repository, coto, jumbo)
    engine = AlertEngine(repository)
    initial = engine.evaluate_product(PRODUCT, (coto, jumbo))
    assert initial is not None
    engine.mark_sent(initial)

    without_promotion = replace(coto, promotions=())
    save(repository, without_promotion)
    promotion_disappeared = engine.evaluate_product(PRODUCT, (without_promotion, jumbo))
    assert promotion_disappeared is not None
    assert promotion_disappeared.fingerprint != initial.fingerprint

    carrefour = observation("35000", retailer="Carrefour", product_id="ca")
    save(repository, carrefour)
    best_changed = engine.evaluate_product(PRODUCT, (coto, jumbo, carrefour))
    assert best_changed is not None
    assert best_changed.best_offer.observation.retailer == "Carrefour"
    engine.mark_sent(best_changed)

    promoted_carrefour = replace(carrefour, promotions=(promotion,))
    save(repository, promoted_carrefour)
    promotion_changed = engine.evaluate_product(PRODUCT, (coto, jumbo, promoted_carrefour))
    assert promotion_changed is not None
    assert promotion_changed.fingerprint != best_changed.fingerprint


def test_product_alert_version_leaves_legacy_pending_event_inert(
    repository: SQLiteRepository,
) -> None:
    promotion = Promotion(
        "Sale", PromotionKind.GENERAL, True, Decimal("30"), DiscountType.PERCENTAGE
    )
    coto = observation("40000", retailer="Coto", product_id="co", promotions=(promotion,))
    jumbo = observation("50000", retailer="Jumbo", product_id="ju")
    save(repository, coto, jumbo)
    repository.record_alert_candidate(
        fingerprint="legacy-listing-pending",
        listing=repository.get_latest_canonical_observations(PRODUCT.canonical_id)[0].listing,
        observed_at=coto.observed_at,
        context=coto.context,
        canonical_id=PRODUCT.canonical_id,
        alert_types=("promotion",),
        price=coto.current_price,
        currency=coto.currency,
    )

    alert = AlertEngine(repository).evaluate_product(PRODUCT, (coto, jumbo))

    assert alert is not None and alert.model_version == 2
    rows = repository.connection.execute(
        "SELECT fingerprint, model_version, status FROM alert_events ORDER BY model_version"
    ).fetchall()
    assert [(row["model_version"], row["status"]) for row in rows] == [
        (1, "pending"),
        (2, "pending"),
    ]
    AlertEngine(repository).mark_sent(alert)
    legacy = repository.connection.execute(
        "SELECT status FROM alert_events WHERE fingerprint = 'legacy-listing-pending'"
    ).fetchone()
    assert legacy["status"] == "pending"


def test_product_alert_refuses_stale_conflicting_expression_membership(
    repository: SQLiteRepository,
) -> None:
    stale_apple = replace(PRODUCT, brand="jack daniels", expression="tennessee apple")
    fire = replace(
        observation("40000", retailer="Carrefour", product_id="fire"),
        title="Whisky Jack Daniels tennesse fire 750 ml",
        brand="Jack Daniels",
        promotions=(
            Promotion(
                "Sale",
                PromotionKind.GENERAL,
                True,
                Decimal("30"),
                DiscountType.PERCENTAGE,
            ),
        ),
    )
    apple = replace(
        observation("45000", retailer="Coto", product_id="apple"),
        title="Whisky Jack Daniels Tennessee Apple 750 ml",
        brand="Jack Daniels",
    )
    save(repository, fire, apple)

    alert = AlertEngine(repository).evaluate_product(stale_apple, (fire, apple))

    assert alert is None
