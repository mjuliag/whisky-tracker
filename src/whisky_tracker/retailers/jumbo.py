"""Jumbo Argentina retailer adapter."""

import base64
from collections.abc import Mapping
from typing import Any

import httpx

from whisky_tracker.models.context import ContextResolution, FulfillmentMode, RetailerContext
from whisky_tracker.models.product import ProductObservation
from whisky_tracker.retailers.base import RetailerError
from whisky_tracker.retailers.vtex import VtexAdapter


class JumboLocationError(RetailerError):
    """Jumbo could not resolve a delivery seller for the requested coordinates."""


class JumboAdapter(VtexAdapter):
    """Retrieve Jumbo Argentina products through its VTEX storefront API."""

    def __init__(self, *, client: httpx.AsyncClient | None = None, page_size: int = 50) -> None:
        super().__init__(
            retailer="jumbo",
            endpoint="https://www.jumbo.com.ar/api/catalog_system/pub/products/search",
            client=client,
            page_size=page_size,
        )
        self.last_resolved_context: RetailerContext | None = None

    async def search_products(
        self, query: str, *, context: RetailerContext | None = None
    ) -> list[ProductObservation]:
        requested = context or RetailerContext()
        if requested.coordinates is None:
            self.last_resolved_context = requested
            return await super().search_products(query, context=requested)
        resolved = await self.resolve_delivery_context(requested)
        self.last_resolved_context = resolved
        return await super().search_products(query, context=resolved)

    async def resolve_delivery_context(self, context: RetailerContext) -> RetailerContext:
        if context.coordinates is None:
            raise JumboLocationError("Jumbo delivery resolution requires coordinates")
        candidates = await self._json_request(
            "GET",
            "https://www.jumbo.com.ar/api/dataentities/NT/search"
            "?_fields=SellerName,name,PurchaseMessage,storeid"
            "&_where=hasDelivery=true&isActive=true",
            headers={"REST-Range": "resources=0-100"},
        )
        if not isinstance(candidates, list):
            raise JumboLocationError("Jumbo delivery-store response is invalid")

        selected: Mapping[str, Any] | None = None
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            seller = _text(candidate.get("SellerName"))
            if not seller:
                continue
            simulation = await self._json_request(
                "POST",
                "https://www.jumbo.com.ar/api/checkout/pub/orderForms/simulation",
                params={"sc": "32"},
                json={
                    "items": [{"id": 253158, "quantity": 1, "seller": seller}],
                    "geoCoordinates": list(context.coordinates),
                    "country": "ARG",
                },
            )
            if _has_delivery_sla(simulation):
                selected = candidate
                break
        if selected is None:
            raise JumboLocationError("Jumbo returned no valid delivery SLA for the location")

        seller = _text(selected.get("SellerName"))
        assert seller is not None
        region_id = base64.b64encode(f"SW#{seller}".encode()).decode()
        longitude, latitude = context.coordinates
        public = {
            "buyselectMethod": {"value": "delivery"},
            "regionId": {"value": region_id},
            "selectedSeller": {"value": seller},
            "geoCoordinates": {"value": f"{longitude},{latitude}"},
        }
        if context.postal_code:
            public["postalCode"] = {"value": context.postal_code}
        session = await self._json_request(
            "POST",
            "https://www.jumbo.com.ar/api/sessions",
            params={"sc": "32"},
            json={"public": public},
        )
        if not isinstance(session, Mapping) or not all(
            _text(session.get(key)) for key in ("sessionToken", "segmentToken")
        ):
            raise JumboLocationError("Jumbo session returned no usable tokens")
        return RetailerContext(
            fulfillment_mode=FulfillmentMode.DELIVERY,
            postal_code=context.postal_code,
            sales_channel="32",
            seller_id=seller,
            store_id=_text(selected.get("storeid")) or seller,
            store_name=_text(selected.get("name")),
            region_id=region_id,
            context_resolution=ContextResolution.ADDRESS_RESOLVED,
        )

    async def _json_request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            response = await self._get_client().request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JumboLocationError("Jumbo delivery resolution request failed") from exc


def _has_delivery_sla(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    logistics = payload.get("logisticsInfo")
    if not isinstance(logistics, list):
        return False
    for item in logistics:
        if not isinstance(item, Mapping):
            continue
        slas = item.get("slas")
        if isinstance(slas, list) and any(
            isinstance(sla, Mapping) and "envio" in (_text(sla.get("name"), normalize=True) or "")
            for sla in slas
        ):
            return True
    return False


def _text(value: Any, *, normalize: bool = False) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    result = value.strip()
    if normalize:
        result = result.casefold().replace("í", "i")
    return result
