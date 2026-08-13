"""Structured one-shot run results."""

from dataclasses import dataclass
from enum import StrEnum

from whisky_tracker.alerts import Alert
from whisky_tracker.matching import ProductMatchGroup


class RetailerRunStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class RetailerStatus:
    retailer: str
    status: RetailerRunStatus
    observations: int = 0
    in_stock: int = 0
    contexts: tuple[str, ...] = ()
    region_ids: tuple[str, ...] = ()
    seller_ids: tuple[str, ...] = ()
    store_names: tuple[str, ...] = ()
    sales_channels: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationFailure:
    fingerprint: str
    error_type: str


@dataclass(frozen=True, slots=True)
class RunSummary:
    retailer_statuses: tuple[RetailerStatus, ...]
    total_observations: int
    canonical_groups: int
    unmatched_observations: int
    match_confidence: tuple[tuple[str, int], ...]
    fuzzy_groups: tuple[ProductMatchGroup, ...]
    observations_stored: int
    database_path: str
    schema_version: int
    eligible_alerts: tuple[Alert, ...]
    alerts_sent: int
    notification_failures: tuple[NotificationFailure, ...]
    dry_run: bool
    notifications_enabled: bool
    notification_cap: int
    duration_seconds: float

    @property
    def alerts_pending(self) -> int:
        return len(self.eligible_alerts) - self.alerts_sent

    @property
    def alerts_deferred_by_cap(self) -> int:
        return max(0, len(self.eligible_alerts) - self.notification_cap)

    @property
    def alerts_selected_for_delivery(self) -> tuple[Alert, ...]:
        return self.eligible_alerts[: self.notification_cap]
