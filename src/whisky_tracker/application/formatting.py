"""Readable manual-run summaries."""

from whisky_tracker.alerts import format_alert
from whisky_tracker.application.models import RunSummary


def format_run_summary(summary: RunSummary, *, include_alert_messages: bool = False) -> str:
    lines = ["Whisky Tracker run complete"]
    if summary.dry_run:
        lines.append("Mode: dry run (persisted; notifications not sent or marked sent)")
    lines.append("")
    for status in summary.retailer_statuses:
        lines.extend((status.retailer, f"  status: {status.status.value}"))
        if status.status.value == "ok":
            lines.append(f"  observations: {status.observations}")
            lines.append(f"  in stock: {status.in_stock}")
            if status.contexts:
                lines.append(f"  contexts: {', '.join(status.contexts)}")
            if status.region_ids:
                lines.append(f"  region IDs: {', '.join(status.region_ids)}")
            if status.seller_ids:
                lines.append(f"  seller IDs: {', '.join(status.seller_ids)}")
            if status.sales_channels:
                lines.append(f"  sales channels: {', '.join(status.sales_channels)}")
        elif status.reason:
            lines.append(f"  reason: {status.reason}")
        lines.append("")
    lines.extend(
        (
            "Matching",
            f"  total observations: {summary.total_observations}",
            f"  canonical groups: {summary.canonical_groups}",
            f"  unmatched: {summary.unmatched_observations}",
            f"  confidence: {dict(summary.match_confidence)}",
            "",
            "Persistence",
            f"  observations stored: {summary.observations_stored}",
            f"  database: {summary.database_path}",
            f"  schema version: {summary.schema_version}",
            "",
            "Alerts",
            f"  eligible: {len(summary.eligible_alerts)}",
            f"  sent: {summary.alerts_sent}",
            f"  pending: {summary.alerts_pending}",
            f"  delivery cap: {summary.notification_cap}",
            f"  deferred by cap: {summary.alerts_deferred_by_cap}",
            f"  notification failures: {len(summary.notification_failures)}",
            "",
            f"Duration: {summary.duration_seconds:.2f}s",
        )
    )
    if include_alert_messages and summary.eligible_alerts:
        lines.extend(("", "Messages that would be sent"))
        for alert in summary.alerts_selected_for_delivery:
            lines.extend(("", format_alert(alert)))
    return "\n".join(lines)
