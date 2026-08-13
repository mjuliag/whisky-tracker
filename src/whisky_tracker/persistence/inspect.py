"""Lightweight developer formatting for local price history."""

from decimal import Decimal

from whisky_tracker.persistence.models import HistoryFilter
from whisky_tracker.persistence.repository import SQLiteRepository


def print_price_history(repository: SQLiteRepository, filters: HistoryFilter) -> None:
    history = repository.get_price_history(filters)
    if not history:
        print("No price history found")
        return
    latest = history[-1]
    place = (
        latest.context.store_name
        or latest.context.store_id
        or latest.context.fulfillment_mode.value
    )
    print(latest.title)
    print(f"{latest.listing.retailer} — {place}")
    print()
    for item in history:
        print(f"{item.observed_at:%Y-%m-%d %H:%M}  {item.currency} {_money(item.current_price)}")
    minimum = repository.get_historical_minimum(filters)
    assert minimum is not None
    print(f"\nMinimum: {minimum.currency} {_money(minimum.current_price)}")
    change = repository.get_latest_price_change(filters)
    if change:
        percentage = "n/a" if change.percentage is None else f"{change.percentage:.2f}%"
        print(f"Latest change: {_signed_money(change.absolute)} ({percentage})")


def _money(value: Decimal) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _signed_money(value: Decimal) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{_money(value)}"
