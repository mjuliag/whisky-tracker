"""Carrefour Argentina retailer adapter."""

from dataclasses import replace

import httpx

from whisky_tracker.identifiers import normalize_gtin
from whisky_tracker.matching.normalization import volume_from_observation
from whisky_tracker.models.context import ContextResolution, FulfillmentMode, RetailerContext
from whisky_tracker.models.product import ProductObservation
from whisky_tracker.retailers.base import RetailerError
from whisky_tracker.retailers.vtex import VtexAdapter


class CarrefourLocationError(RetailerError):
    """Carrefour could not resolve a usable fulfillment region."""


class CarrefourAdapter(VtexAdapter):
    """Retrieve Carrefour Argentina products through its VTEX storefront API."""

    def __init__(self, *, client: httpx.AsyncClient | None = None, page_size: int = 50) -> None:
        super().__init__(
            retailer="carrefour",
            endpoint="https://www.carrefour.com.ar/api/catalog_system/pub/products/search",
            client=client,
            page_size=page_size,
        )
        self.last_resolved_context: RetailerContext | None = None

    async def search_products(
        self, query: str, *, context: RetailerContext | None = None
    ) -> list[ProductObservation]:
        requested = context or RetailerContext()
        if requested.context_resolution is ContextResolution.GENERIC and not requested.postal_code:
            self.last_resolved_context = requested
            products = await super().search_products(query, context=requested)
            return [self._sanitize_product_identity(product) for product in products]
        if not requested.postal_code:
            raise CarrefourLocationError("Carrefour postcode retrieval requires a postal code")
        if requested.fulfillment_mode is not FulfillmentMode.SCHEDULED_DELIVERY:
            raise CarrefourLocationError(
                "Carrefour postcode resolution currently supports scheduled delivery only"
            )

        resolved = await self.resolve_postcode_context(requested)
        self.last_resolved_context = resolved
        products = await super().search_products(query, context=resolved)
        return [self._sanitize_product_identity(product) for product in products]

    @staticmethod
    def _sanitize_product_identity(product: ProductObservation) -> ProductObservation:
        """Discard two Carrefour EAN assignments whose titles contradict the real bottle size."""
        known_volume_by_gtin = {
            "5000299611104": 700,  # Chivas Regal Extra 13
            "5099873017623": 700,  # Jack Daniel's Tennessee Apple
        }
        gtin = normalize_gtin(product.gtin)
        expected_volume = known_volume_by_gtin.get(gtin or "")
        if expected_volume is None or volume_from_observation(product) == expected_volume:
            return product
        return replace(product, gtin=None)

    async def resolve_postcode_context(self, context: RetailerContext) -> RetailerContext:
        """Resolve a postcode and establish the anonymous VTEX commercial session."""
        sales_channel = context.sales_channel or "3"
        client = self._get_client()
        try:
            response = await client.get(
                "https://www.carrefour.com.ar/api/checkout/pub/regions",
                params={
                    "country": "ARG",
                    "postalCode": context.postal_code,
                    "sc": sales_channel,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CarrefourLocationError(
                f"failed to resolve Carrefour postcode {context.postal_code!r}"
            ) from exc

        if not isinstance(payload, list) or not payload:
            raise CarrefourLocationError(
                f"Carrefour postcode {context.postal_code!r} has no delivery region"
            )
        region = payload[0]
        if not isinstance(region, dict) or not isinstance(region.get("id"), str):
            raise CarrefourLocationError("Carrefour region response has an invalid region")
        sellers = region.get("sellers")
        if not isinstance(sellers, list) or not sellers or not isinstance(sellers[0], dict):
            raise CarrefourLocationError(
                f"Carrefour postcode {context.postal_code!r} has no usable seller"
            )
        seller = sellers[0]
        seller_id = seller.get("id")
        store_name = seller.get("name")
        if not isinstance(seller_id, str) or not seller_id:
            raise CarrefourLocationError("Carrefour region seller has no valid identifier")

        try:
            session = await client.post(
                "https://www.carrefour.com.ar/api/sessions",
                params={"sc": sales_channel},
                json={
                    "public": {
                        "country": {"value": "ARG"},
                        "regionId": {"value": region["id"]},
                    }
                },
            )
            session.raise_for_status()
            session_payload = session.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CarrefourLocationError("failed to establish Carrefour VTEX session") from exc
        if not isinstance(session_payload, dict) or not all(
            isinstance(session_payload.get(key), str) and session_payload[key]
            for key in ("sessionToken", "segmentToken")
        ):
            raise CarrefourLocationError("Carrefour VTEX session returned no usable tokens")

        return RetailerContext(
            fulfillment_mode=context.fulfillment_mode,
            postal_code=context.postal_code,
            coordinates=context.coordinates,
            sales_channel=sales_channel,
            region_id=region["id"],
            seller_id=seller_id,
            store_id=context.store_id,
            store_name=store_name if isinstance(store_name, str) else None,
            context_resolution=ContextResolution.POSTCODE_RESOLVED,
        )
