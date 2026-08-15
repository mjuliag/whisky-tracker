"""One-shot collection, matching, persistence, alert, and delivery orchestration."""

import logging
from collections import Counter
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from whisky_tracker.alerts import AlertEngine, alert_priority_key, format_alert
from whisky_tracker.application.models import (
    NotificationFailure,
    RetailerRunStatus,
    RetailerStatus,
    RunSummary,
)
from whisky_tracker.matching import CanonicalProduct, ProductMatcher
from whisky_tracker.models.context import ContextResolution, RetailerContext
from whisky_tracker.models.product import ProductObservation
from whisky_tracker.persistence import SQLiteRepository

logger = logging.getLogger(__name__)


class SearchAdapter(Protocol):
    async def search_products(
        self, query: str, *, context: RetailerContext | None = None
    ) -> list[ProductObservation]: ...


class AlertNotifier(Protocol):
    async def send_message(self, text: str, *, parse_mode: str | None = None) -> int: ...


@dataclass(frozen=True, slots=True)
class RetailerCollection:
    name: str
    adapter: SearchAdapter | None
    context: RetailerContext | None = None
    skip_reason: str | None = None


class WhiskyTrackerRunner:
    def __init__(
        self,
        *,
        collections: tuple[RetailerCollection, ...],
        matcher: ProductMatcher,
        repository: SQLiteRepository,
        alert_engine: AlertEngine,
        notifier: AlertNotifier | None,
        database_path: str,
        query: str = "whisky",
        max_notifications_per_run: int = 10,
    ) -> None:
        self.collections = collections
        self.matcher = matcher
        self.repository = repository
        self.alert_engine = alert_engine
        self.notifier = notifier
        self.database_path = database_path
        self.query = query
        if max_notifications_per_run < 1:
            raise ValueError("max_notifications_per_run must be positive")
        self.max_notifications_per_run = max_notifications_per_run

    async def run(self, *, dry_run: bool = False) -> RunSummary:
        started = monotonic()
        logger.info("Whisky Tracker run started")
        observations: list[ProductObservation] = []
        statuses: list[RetailerStatus] = []
        for collection in self.collections:
            if collection.adapter is None:
                logger.info("Retailer %s skipped: %s", collection.name, collection.skip_reason)
                statuses.append(
                    RetailerStatus(
                        collection.name,
                        RetailerRunStatus.SKIPPED,
                        reason=collection.skip_reason or "not_configured",
                    )
                )
                continue
            logger.info("Retailer %s collection started", collection.name)
            try:
                items = await collection.adapter.search_products(
                    self.query, context=collection.context
                )
            except Exception as exc:  # Retailers must be isolated from one another.
                error_type = type(exc).__name__
                logger.warning("Retailer %s failed (%s)", collection.name, error_type)
                statuses.append(
                    RetailerStatus(collection.name, RetailerRunStatus.FAILED, reason=error_type)
                )
                continue
            observations.extend(items)
            contexts = tuple(sorted({_context_label(item.context) for item in items}))
            retailer_contexts = {item.context for item in items}
            statuses.append(
                RetailerStatus(
                    collection.name,
                    RetailerRunStatus.OK,
                    observations=len(items),
                    in_stock=sum(item.in_stock for item in items),
                    contexts=contexts,
                    region_ids=_context_values(retailer_contexts, "region_id"),
                    seller_ids=_context_values(retailer_contexts, "seller_id"),
                    store_names=_context_values(retailer_contexts, "store_name"),
                    sales_channels=_context_values(retailer_contexts, "sales_channel"),
                )
            )
            logger.info("Retailer %s completed with %d observations", collection.name, len(items))

        observations = list(dict.fromkeys(observations))
        matching_result = self.matcher.match(observations)
        logger.info(
            "Matching completed with %d groups and %d unmatched observations",
            len(matching_result.groups),
            len(matching_result.unmatched),
        )
        before = self.repository.observation_count()
        self.repository.save_matching_result(matching_result)
        stored = self.repository.observation_count() - before
        logger.info("Persistence completed with %d new observation rows", stored)

        current_by_canonical: dict[str, tuple[CanonicalProduct, list[ProductObservation]]] = {}
        for group in matching_result.groups:
            persisted_product = self.repository.resolve_canonical_product(group.canonical_product)
            entry = current_by_canonical.setdefault(
                persisted_product.canonical_id, (persisted_product, [])
            )
            entry[1].extend(group.observations)
        eligible = []
        for canonical_product, current_observations in current_by_canonical.values():
            alert = self.alert_engine.evaluate_product(
                canonical_product, tuple(current_observations)
            )
            if alert is not None:
                eligible.append(alert)
        eligible.sort(key=alert_priority_key)
        logger.info("Alert evaluation produced %d eligible alerts", len(eligible))

        sent = 0
        failures: list[NotificationFailure] = []
        if not dry_run and self.notifier is not None:
            for alert in eligible[: self.max_notifications_per_run]:
                try:
                    await self.notifier.send_message(format_alert(alert), parse_mode="HTML")
                except Exception as exc:  # One failed notification must not block later alerts.
                    error_type = type(exc).__name__
                    logger.warning("Alert notification failed (%s)", error_type)
                    failures.append(NotificationFailure(alert.fingerprint, error_type))
                    continue
                self.alert_engine.mark_sent(alert)
                sent += 1
                logger.info("Alert notification delivered")

        confidence = Counter(group.match_confidence.value for group in matching_result.groups)
        duration = monotonic() - started
        summary = RunSummary(
            retailer_statuses=tuple(statuses),
            total_observations=len(observations),
            canonical_groups=len(matching_result.groups),
            unmatched_observations=len(matching_result.unmatched),
            match_confidence=tuple(sorted(confidence.items())),
            fuzzy_groups=tuple(
                group
                for group in matching_result.groups
                if group.match_confidence.value == "fuzzy_supported"
            ),
            observations_stored=stored,
            database_path=self.database_path,
            schema_version=self.repository.schema_version,
            eligible_alerts=tuple(eligible),
            alerts_sent=sent,
            notification_failures=tuple(failures),
            dry_run=dry_run,
            notifications_enabled=self.notifier is not None,
            notification_cap=self.max_notifications_per_run,
            duration_seconds=duration,
        )
        logger.info("Whisky Tracker run completed in %.2fs", duration)
        return summary


def _context_label(context: RetailerContext) -> str:
    if context.store_name:
        return context.store_name
    if context.context_resolution is ContextResolution.GENERIC:
        return "generic"
    return context.context_resolution.value


def _context_values(contexts: set[RetailerContext], attribute: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {value for context in contexts if (value := getattr(context, attribute)) is not None}
        )
    )
