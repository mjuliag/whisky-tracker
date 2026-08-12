"""Jumbo Argentina retailer adapter."""

import httpx

from whisky_tracker.retailers.vtex import VtexAdapter


class JumboAdapter(VtexAdapter):
    """Retrieve Jumbo Argentina products through its VTEX storefront API."""

    def __init__(self, *, client: httpx.AsyncClient | None = None, page_size: int = 50) -> None:
        super().__init__(
            retailer="jumbo",
            endpoint="https://www.jumbo.com.ar/api/catalog_system/pub/products/search",
            client=client,
            page_size=page_size,
        )
