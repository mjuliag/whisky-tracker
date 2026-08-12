"""Common retailer interface."""

from typing import Protocol

from whisky_tracker.models.context import RetailerContext
from whisky_tracker.models.product import ProductObservation


class RetailerError(RuntimeError):
    """Base error raised by retailer adapters."""


class RetailerAdapter(Protocol):
    """Minimal interface implemented by every retailer."""

    async def search_products(
        self, query: str, *, context: RetailerContext | None = None
    ) -> list[ProductObservation]:
        """Retrieve normalized product observations matching ``query``."""
        ...
