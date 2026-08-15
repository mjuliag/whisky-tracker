import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from whisky_tracker.alerts import AlertEngine
from whisky_tracker.application import (
    AppConfig,
    ConfigurationError,
    RetailerCollection,
    RetailerRunStatus,
    WhiskyTrackerRunner,
    load_config,
)
from whisky_tracker.application.bootstrap import build_runtime
from whisky_tracker.application.formatting import format_run_summary
from whisky_tracker.matching import ProductMatcher
from whisky_tracker.models import (
    ContextResolution,
    DiscountType,
    FulfillmentMode,
    ProductObservation,
    Promotion,
    PromotionKind,
    RetailerContext,
)
from whisky_tracker.persistence import HistoryFilter, PersistenceError, SQLiteRepository

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)
PROMOTION = Promotion(
    "30% Club",
    PromotionKind.LOYALTY,
    False,
    Decimal("30"),
    DiscountType.PERCENTAGE,
)


def observation(
    retailer: str,
    product_id: str,
    *,
    price: str = "40000",
    promotion: bool = True,
    context: RetailerContext | None = None,
) -> ProductObservation:
    return ProductObservation(
        retailer=retailer,
        retailer_product_id=product_id,
        retailer_sku_id=f"sku-{product_id}",
        catalog_product_id=None,
        gtin="7790895000997",
        title="Johnnie Walker Black Label 750 ml",
        brand="Johnnie Walker",
        size_value=Decimal(750),
        size_unit="ml",
        pack_count=1,
        currency="ARS",
        current_price=Decimal(price),
        regular_price=None,
        promotions=(PROMOTION,) if promotion else (),
        condition="new",
        available_quantity=2,
        in_stock=True,
        product_url=f"https://example.test/{product_id}",
        context=context or RetailerContext(),
        observed_at=NOW,
    )


class FakeAdapter:
    def __init__(
        self,
        items: list[ProductObservation] | None = None,
        error: Exception | None = None,
        expected_context: RetailerContext | None = None,
    ) -> None:
        self.items = items or []
        self.error = error
        self.expected_context = expected_context
        self.calls = 0

    async def search_products(
        self, _query: str, *, context: RetailerContext | None = None
    ) -> list[ProductObservation]:
        self.calls += 1
        if self.expected_context is not None:
            assert context == self.expected_context
        if self.error:
            raise self.error
        return self.items


class MatcherSpy:
    def __init__(self) -> None:
        self.calls = 0
        self.matcher = ProductMatcher()

    def match(self, observations):
        self.calls += 1
        return self.matcher.match(observations)


class FakeNotifier:
    def __init__(self, failures: set[int] | None = None) -> None:
        self.messages: list[str] = []
        self.failures = failures or set()

    async def send_message(self, text: str, *, parse_mode: str | None = None) -> int:
        assert parse_mode == "HTML"
        index = len(self.messages)
        self.messages.append(text)
        if index in self.failures:
            raise RuntimeError("delivery failed")
        return index + 1


@pytest.fixture
def repository(tmp_path: Path) -> SQLiteRepository:
    with SQLiteRepository(tmp_path / "runner.db") as result:
        result.initialize()
        yield result


def runner(
    repository: SQLiteRepository,
    collections: tuple[RetailerCollection, ...],
    *,
    notifier: FakeNotifier | None = None,
    matcher: MatcherSpy | None = None,
    notification_cap: int = 10,
) -> tuple[WhiskyTrackerRunner, MatcherSpy]:
    matcher_spy = matcher or MatcherSpy()
    return (
        WhiskyTrackerRunner(
            collections=collections,
            matcher=matcher_spy,  # type: ignore[arg-type]
            repository=repository,
            alert_engine=AlertEngine(repository),
            notifier=notifier,
            database_path=str(repository.path),
            max_notifications_per_run=notification_cap,
        ),
        matcher_spy,
    )


def run(coroutine):
    return asyncio.run(coroutine)


def test_happy_path_collects_matches_persists_sends_and_marks(
    repository: SQLiteRepository,
) -> None:
    notifier = FakeNotifier()
    service, matcher = runner(
        repository,
        (
            RetailerCollection("Carrefour", FakeAdapter([observation("Carrefour", "c1")])),
            RetailerCollection("Coto", FakeAdapter([observation("Coto", "co1")])),
        ),
        notifier=notifier,
    )
    summary = run(service.run())
    assert matcher.calls == 1
    assert summary.total_observations == 2
    assert summary.canonical_groups == 1
    assert summary.observations_stored == 2
    assert len(summary.eligible_alerts) == 2
    assert len(notifier.messages) == 2
    assert summary.alerts_sent == 2
    assert all(repository.is_alert_sent(alert.fingerprint) for alert in summary.eligible_alerts)


def test_retailer_failure_is_isolated_and_empty_result_is_explicit(
    repository: SQLiteRepository,
) -> None:
    good = FakeAdapter([])
    service, _ = runner(
        repository,
        (
            RetailerCollection("Carrefour", FakeAdapter(error=RuntimeError("location"))),
            RetailerCollection("Jumbo", good),
        ),
    )
    summary = run(service.run())
    assert summary.retailer_statuses[0].status is RetailerRunStatus.FAILED
    assert summary.retailer_statuses[0].reason == "RuntimeError"
    assert summary.retailer_statuses[1].status is RetailerRunStatus.OK
    assert summary.retailer_statuses[1].observations == 0
    assert good.calls == 1


def test_missing_ml_credentials_are_skipped(tmp_path: Path) -> None:
    runtime = build_runtime(
        AppConfig(database_path=tmp_path / "ml.db"),
        selected_retailers=frozenset(("mercadolibre",)),
    )
    try:
        collection = runtime.runner.collections[0]
        assert collection.adapter is None
        assert collection.skip_reason == "credentials_not_configured"
        summary = run(runtime.runner.run(dry_run=True))
        assert summary.retailer_statuses[0].status is RetailerRunStatus.SKIPPED
        assert summary.retailer_statuses[0].reason == "credentials_not_configured"
    finally:
        run(runtime.close())


def test_ml_auth_failure_is_reported_and_other_retailers_continue(
    repository: SQLiteRepository,
) -> None:
    service, _ = runner(
        repository,
        (
            RetailerCollection("Mercado Libre", FakeAdapter(error=RuntimeError("secret-token"))),
            RetailerCollection("Coto", FakeAdapter([observation("Coto", "co1")])),
        ),
    )
    summary = run(service.run())
    assert summary.retailer_statuses[0].status is RetailerRunStatus.FAILED
    assert summary.retailer_statuses[1].status is RetailerRunStatus.OK


def test_carrefour_location_failure_has_no_generic_fallback(
    repository: SQLiteRepository,
) -> None:
    location = RetailerContext(
        fulfillment_mode=FulfillmentMode.SCHEDULED_DELIVERY, postal_code="1428"
    )
    adapter = FakeAdapter(error=RuntimeError("resolution"), expected_context=location)
    service, _ = runner(repository, (RetailerCollection("Carrefour", adapter, location),))
    summary = run(service.run())
    assert adapter.calls == 1
    assert summary.retailer_statuses[0].status is RetailerRunStatus.FAILED


def test_missing_telegram_succeeds_with_eligible_pending_alert(
    repository: SQLiteRepository,
) -> None:
    service, _ = runner(
        repository,
        (RetailerCollection("Coto", FakeAdapter([observation("Coto", "co1")])),),
    )
    summary = run(service.run())
    assert len(summary.eligible_alerts) == 1
    assert summary.alerts_sent == 0
    assert summary.alerts_pending == 1
    assert summary.notifications_enabled is False
    assert not repository.is_alert_sent(summary.eligible_alerts[0].fingerprint)


def test_one_telegram_failure_does_not_stop_later_alerts(
    repository: SQLiteRepository,
) -> None:
    notifier = FakeNotifier(failures={0})
    service, _ = runner(
        repository,
        (
            RetailerCollection(
                "Coto",
                FakeAdapter(
                    [observation("Coto", "one"), observation("Coto", "two", price="41000")]
                ),
            ),
        ),
        notifier=notifier,
    )
    summary = run(service.run())
    assert len(notifier.messages) == 2
    assert summary.alerts_sent == 1
    assert len(summary.notification_failures) == 1
    sent = [repository.is_alert_sent(alert.fingerprint) for alert in summary.eligible_alerts]
    assert sent == [False, True]


def test_dry_run_persists_but_never_sends_or_marks(repository: SQLiteRepository) -> None:
    notifier = FakeNotifier()
    service, _ = runner(
        repository,
        (RetailerCollection("Coto", FakeAdapter([observation("Coto", "co1")])),),
        notifier=notifier,
    )
    summary = run(service.run(dry_run=True))
    assert summary.dry_run is True
    assert summary.observations_stored == 1
    assert notifier.messages == []
    assert summary.alerts_pending == 1
    assert not repository.is_alert_sent(summary.eligible_alerts[0].fingerprint)
    assert "notifications not sent" in format_run_summary(summary)


def test_default_notification_cap_sends_ten_and_defers_remaining(
    repository: SQLiteRepository,
) -> None:
    notifier = FakeNotifier()
    items = [observation("Coto", f"item-{index}") for index in range(12)]
    service, _ = runner(
        repository,
        (RetailerCollection("Coto", FakeAdapter(items)),),
        notifier=notifier,
    )
    summary = run(service.run())
    assert len(summary.eligible_alerts) == 12
    assert len(notifier.messages) == 10
    assert summary.alerts_sent == 10
    assert summary.alerts_deferred_by_cap == 2
    assert all(
        not repository.is_alert_sent(alert.fingerprint) for alert in summary.eligible_alerts[10:]
    )


def test_notification_cap_is_configurable(repository: SQLiteRepository) -> None:
    notifier = FakeNotifier()
    items = [observation("Coto", f"item-{index}") for index in range(4)]
    service, _ = runner(
        repository,
        (RetailerCollection("Coto", FakeAdapter(items)),),
        notifier=notifier,
        notification_cap=2,
    )
    summary = run(service.run())
    assert len(notifier.messages) == 2
    assert summary.alerts_deferred_by_cap == 2


def test_persistence_failure_stops_before_notifications(repository: SQLiteRepository) -> None:
    notifier = FakeNotifier()
    service, _ = runner(
        repository,
        (RetailerCollection("Coto", FakeAdapter([observation("Coto", "co1")])),),
        notifier=notifier,
    )

    def fail(_result) -> None:
        raise PersistenceError("transaction failed")

    repository.save_matching_result = fail  # type: ignore[method-assign]
    with pytest.raises(PersistenceError):
        run(service.run())
    assert notifier.messages == []


def test_logs_and_status_do_not_expose_secret_exception_text(
    repository: SQLiteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    token = "very-secret-access-token"
    service, _ = runner(
        repository,
        (RetailerCollection("Mercado Libre", FakeAdapter(error=RuntimeError(token))),),
    )
    with caplog.at_level(logging.INFO):
        summary = run(service.run())
    rendered = caplog.text + format_run_summary(summary)
    assert token not in rendered


def test_config_loads_dotenv_with_environment_override_and_thresholds(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WHISKY_TRACKER_DB_PATH=local.db\n"
        "TELEGRAM_BOT_TOKEN=from-file\n"
        "MINIMUM_PRICE_DROP_PERCENTAGE=12.5\n"
    )
    config = load_config(
        env_file=env_file,
        environment={"TELEGRAM_BOT_TOKEN": "from-environment", "CARREFOUR_POSTAL_CODE": "1428"},
    )
    assert config.database_path == Path("local.db")
    assert config.telegram_bot_token == "from-environment"
    assert config.carrefour_postal_code == "1428"
    assert config.alert_config.minimum_price_drop_percentage == Decimal("12.5")


def test_config_loads_one_exact_user_location_without_logging_or_retailer_coupling(
    tmp_path: Path,
) -> None:
    config = load_config(
        env_file=tmp_path / "absent.env",
        environment={
            "USER_LATITUDE": "-34.0",
            "USER_LONGITUDE": "-58.0",
            "USER_POSTAL_CODE": "1428",
        },
    )
    assert config.user_coordinates == (-58.0, -34.0)
    assert config.user_postal_code == "1428"
    assert config.carrefour_postal_code == "1428"


@pytest.mark.parametrize(
    "environment",
    [
        {"USER_LATITUDE": "-34.0"},
        {"USER_LONGITUDE": "-58.0"},
        {"USER_LATITUDE": "91", "USER_LONGITUDE": "-58.0"},
    ],
)
def test_config_rejects_incomplete_or_invalid_coordinates(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    with pytest.raises(ConfigurationError):
        load_config(env_file=tmp_path / "absent.env", environment=environment)


def test_bootstrap_injects_location_and_never_silently_uses_generic_coto_or_jumbo(
    tmp_path: Path,
) -> None:
    runtime = build_runtime(
        AppConfig(
            database_path=tmp_path / "located.db",
            user_latitude=-34.0,
            user_longitude=-58.0,
            user_postal_code="1428",
        ),
        selected_retailers=frozenset(("coto", "jumbo")),
    )
    try:
        assert all(collection.adapter is not None for collection in runtime.runner.collections)
        assert all(
            collection.context
            == RetailerContext(
                fulfillment_mode=FulfillmentMode.DELIVERY,
                postal_code="1428",
                coordinates=(-58.0, -34.0),
            )
            for collection in runtime.runner.collections
        )
    finally:
        run(runtime.close())

    missing = build_runtime(
        AppConfig(database_path=tmp_path / "missing.db"),
        selected_retailers=frozenset(("coto", "jumbo")),
    )
    try:
        assert all(collection.adapter is None for collection in missing.runner.collections)
        assert all(
            collection.skip_reason == "coordinates_not_configured"
            for collection in missing.runner.collections
        )
    finally:
        run(missing.close())


def test_end_to_end_preserves_three_resolved_retailer_contexts(
    repository: SQLiteRepository,
) -> None:
    carrefour = RetailerContext(
        fulfillment_mode=FulfillmentMode.SCHEDULED_DELIVERY,
        postal_code="1428",
        sales_channel="3",
        region_id="carrefour-region",
        seller_id="carrefour-seller",
        store_name="Market Juramento",
        context_resolution=ContextResolution.POSTCODE_RESOLVED,
    )
    coto = RetailerContext(
        fulfillment_mode=FulfillmentMode.DELIVERY,
        postal_code="1428",
        store_id="coto-branch",
        context_resolution=ContextResolution.ADDRESS_RESOLVED,
    )
    jumbo = RetailerContext(
        fulfillment_mode=FulfillmentMode.DELIVERY,
        postal_code="1428",
        sales_channel="32",
        region_id="jumbo-region",
        seller_id="jumbo-seller",
        store_id="jumbo-store",
        context_resolution=ContextResolution.ADDRESS_RESOLVED,
    )
    service, _ = runner(
        repository,
        (
            RetailerCollection(
                "Carrefour", FakeAdapter([observation("Carrefour", "cf", context=carrefour)])
            ),
            RetailerCollection("Coto", FakeAdapter([observation("Coto", "co", context=coto)])),
            RetailerCollection("Jumbo", FakeAdapter([observation("Jumbo", "ju", context=jumbo)])),
            RetailerCollection("Mercado Libre", None, skip_reason="credentials_not_configured"),
        ),
    )
    summary = run(service.run(dry_run=True))
    assert summary.canonical_groups == 1
    assert summary.observations_stored == 3
    assert len(summary.eligible_alerts) == 3
    canonical_id = repository.connection.execute(
        "SELECT canonical_id FROM canonical_products"
    ).fetchone()[0]
    history = repository.get_price_history(HistoryFilter(canonical_id=canonical_id))
    assert {item.context for item in history} == {carrefour, coto, jumbo}


def test_transient_runner_coordinates_never_reach_observations_state_alerts_or_summary(
    repository: SQLiteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    transient = RetailerContext(
        fulfillment_mode=FulfillmentMode.DELIVERY,
        postal_code="test-postcode",
        coordinates=(12.345678, -45.678912),
    )
    coto = RetailerContext(
        fulfillment_mode=FulfillmentMode.DELIVERY,
        postal_code="test-postcode",
        store_id="resolved-coto",
        context_resolution=ContextResolution.ADDRESS_RESOLVED,
    )
    jumbo = RetailerContext(
        fulfillment_mode=FulfillmentMode.DELIVERY,
        postal_code="test-postcode",
        sales_channel="32",
        seller_id="resolved-seller",
        store_id="resolved-jumbo",
        region_id="resolved-region",
        context_resolution=ContextResolution.ADDRESS_RESOLVED,
    )
    service, _ = runner(
        repository,
        (
            RetailerCollection(
                "Coto",
                FakeAdapter(
                    [observation("Coto", "privacy-coto", context=coto)],
                    expected_context=transient,
                ),
                transient,
            ),
            RetailerCollection(
                "Jumbo",
                FakeAdapter(
                    [observation("Jumbo", "privacy-jumbo", context=jumbo)],
                    expected_context=transient,
                ),
                transient,
            ),
        ),
    )

    with caplog.at_level(logging.INFO):
        summary = run(service.run(dry_run=True))

    rendered = caplog.text + format_run_summary(summary, include_alert_messages=True)
    database_dump = "\n".join(repository.connection.iterdump())
    for exact_coordinate in ("12.345678", "-45.678912"):
        assert exact_coordinate not in rendered
        assert exact_coordinate not in database_dump
    assert all(alert.observation.context.coordinates is None for alert in summary.eligible_alerts)
    rows = repository.connection.execute("SELECT longitude, latitude FROM observations").fetchall()
    assert rows and all(row["longitude"] is None and row["latitude"] is None for row in rows)


def test_cloud_database_path_comes_from_environment(tmp_path: Path) -> None:
    cloud_path = tmp_path / "runtime-state" / "whisky_tracker.db"
    config = load_config(
        env_file=tmp_path / "absent.env",
        environment={"WHISKY_TRACKER_DB_PATH": str(cloud_path)},
    )
    assert config.database_path == cloud_path
