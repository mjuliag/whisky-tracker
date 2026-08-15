"""Commercial and fulfillment context used to retrieve retailer prices."""

from dataclasses import dataclass
from enum import StrEnum


class FulfillmentMode(StrEnum):
    """How the retailer is expected to fulfill an order."""

    GENERIC = "generic"
    DIGITAL = "digital"
    SCHEDULED_DELIVERY = "scheduled_delivery"
    DELIVERY = "delivery"
    PICKUP = "pickup"
    REPRESENTATIVE_STORE = "representative_store"


class ContextResolution(StrEnum):
    """How precisely a retailer context was resolved."""

    GENERIC = "generic"
    POSTCODE_RESOLVED = "postcode_resolved"
    ADDRESS_RESOLVED = "address_resolved"
    MANUALLY_SELECTED_STORE = "manually_selected_store"


@dataclass(frozen=True, slots=True)
class RetailerContext:
    """Location and commercial inputs that determine price and stock."""

    fulfillment_mode: FulfillmentMode = FulfillmentMode.GENERIC
    postal_code: str | None = None
    coordinates: tuple[float, float] | None = None
    sales_channel: str | None = None
    region_id: str | None = None
    seller_id: str | None = None
    store_id: str | None = None
    store_name: str | None = None
    context_resolution: ContextResolution = ContextResolution.GENERIC

    def __post_init__(self) -> None:
        if self.coordinates is not None and len(self.coordinates) != 2:
            raise ValueError("coordinates must contain longitude and latitude")
        if (
            self.context_resolution is ContextResolution.ADDRESS_RESOLVED
            and self.coordinates is not None
        ):
            raise ValueError("address-resolved commercial contexts must not retain coordinates")
        if self.context_resolution is ContextResolution.POSTCODE_RESOLVED:
            if not self.postal_code:
                raise ValueError("postcode-resolved contexts require postal_code")
            if not self.region_id or not self.seller_id:
                raise ValueError("postcode-resolved contexts require region_id and seller_id")
