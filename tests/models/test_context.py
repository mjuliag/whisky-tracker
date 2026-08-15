"""Tests for immutable retailer retrieval contexts."""

import pytest

from whisky_tracker.models.context import ContextResolution, FulfillmentMode, RetailerContext


def test_generic_context_defaults_are_explicit() -> None:
    context = RetailerContext()

    assert context.fulfillment_mode is FulfillmentMode.GENERIC
    assert context.context_resolution is ContextResolution.GENERIC
    assert context.postal_code is None


def test_all_context_resolution_values_are_available() -> None:
    assert {value.value for value in ContextResolution} == {
        "generic",
        "postcode_resolved",
        "address_resolved",
        "manually_selected_store",
    }


def test_postcode_resolved_context_requires_commercial_identifiers() -> None:
    with pytest.raises(ValueError, match="region_id and seller_id"):
        RetailerContext(
            postal_code="1428",
            context_resolution=ContextResolution.POSTCODE_RESOLVED,
        )


def test_address_resolved_commercial_context_cannot_retain_transient_coordinates() -> None:
    with pytest.raises(ValueError, match="must not retain coordinates"):
        RetailerContext(
            fulfillment_mode=FulfillmentMode.DELIVERY,
            coordinates=(12.345678, -45.678912),
            store_id="resolved-store",
            context_resolution=ContextResolution.ADDRESS_RESOLVED,
        )
