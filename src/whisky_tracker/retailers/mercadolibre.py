"""Official Mercado Libre API adapter for the Argentina marketplace."""

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from whisky_tracker.models.context import ContextResolution, FulfillmentMode, RetailerContext
from whisky_tracker.models.product import ProductObservation
from whisky_tracker.retailers.base import RetailerError

_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
_GTIN_ATTRIBUTE_IDS = {"GTIN", "EAN", "BARCODE"}
_BRAND_ATTRIBUTE_IDS = {"BRAND"}
_VOLUME_ATTRIBUTE_IDS = {"VOLUME", "NET_VOLUME", "UNIT_VOLUME"}
_PACK_ATTRIBUTE_IDS = {"UNITS_PER_PACK", "SALE_FORMAT", "PACK_QUANTITY"}
_SIZE_PATTERN = re.compile(
    r"(?<!\d)(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>ml|cc|cl|l|lt|lts|litros?)\b",
    re.IGNORECASE,
)
_PACK_PATTERNS = (
    re.compile(r"\b(?:pack|combo|caja|case)\s*(?:de|x)?\s*(?P<count>\d+)\b", re.IGNORECASE),
    re.compile(r"\bx\s*(?P<count>\d+)\b", re.IGNORECASE),
    re.compile(r"\b(?P<count>\d+)\s*(?:unidades|botellas|u\.)\b", re.IGNORECASE),
)
_OBVIOUS_NON_WHISKY = re.compile(
    r"\b(?:vaso|vasos|copa|copas|caj[ao]\s+vac[ií]a|botella\s+vac[ií]a|"
    r"decantador|licorera|posavasos|cartel|poster|remera|perfume|esencia|"
    r"vela|llavero|funda|estuche\s+vac[ií]o)\b",
    re.IGNORECASE,
)


class MercadoLibreError(RetailerError):
    """Base error raised by Mercado Libre integration."""


class MercadoLibreCredentialsError(MercadoLibreError):
    """Required authentication credentials are missing."""


class MercadoLibreTokenExpiredError(MercadoLibreError):
    """The access token expired and cannot be refreshed."""


class MercadoLibreRefreshError(MercadoLibreError):
    """Mercado Libre rejected or malformed a token refresh."""


class MercadoLibreAuthorizationError(MercadoLibreError):
    """Mercado Libre denied the authenticated request."""


class MercadoLibreRateLimitError(MercadoLibreError):
    """Mercado Libre rate limiting remained active after retries."""


class MercadoLibreSchemaError(MercadoLibreError):
    """The API response no longer has the expected shape."""


@dataclass(slots=True)
class MercadoLibreAuth:
    """Mutable OAuth credentials owned by the caller/application configuration."""

    access_token: str | None
    refresh_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None

    def require_access_token(self) -> str:
        if not self.access_token:
            raise MercadoLibreCredentialsError("Mercado Libre access token is required")
        return self.access_token

    @property
    def can_refresh(self) -> bool:
        return bool(self.refresh_token and self.client_id and self.client_secret)

    async def refresh(self, client: httpx.AsyncClient, token_url: str) -> str:
        if not self.can_refresh:
            raise MercadoLibreTokenExpiredError(
                "Mercado Libre token expired and refresh credentials are incomplete"
            )
        try:
            response = await client.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise MercadoLibreRefreshError("Mercado Libre token refresh request failed") from exc
        if response.status_code >= 400:
            raise MercadoLibreRefreshError(
                f"Mercado Libre token refresh returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MercadoLibreRefreshError(
                "Mercado Libre token refresh returned invalid JSON"
            ) from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("access_token"), str):
            raise MercadoLibreRefreshError("Mercado Libre token refresh returned no access token")
        self.access_token = payload["access_token"]
        if isinstance(payload.get("refresh_token"), str) and payload["refresh_token"]:
            self.refresh_token = payload["refresh_token"]
        return self.access_token


@dataclass(frozen=True, slots=True)
class MercadoLibreConfig:
    """Replaceable official API paths and conservative request limits."""

    api_base_url: str = "https://api.mercadolibre.com"
    site_id: str = "MLA"
    search_path: str = "/sites/{site_id}/search"
    items_path: str = "/items"
    token_path: str = "/oauth/token"
    page_size: int = 25
    batch_size: int = 20
    max_results: int = 1000
    request_delay: float = 0.1


class MercadoLibreAdapter:
    """Retrieve and normalize authenticated Mercado Libre MLA listings."""

    def __init__(
        self,
        *,
        auth: MercadoLibreAuth,
        config: MercadoLibreConfig | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
        max_attempts: int = 3,
    ) -> None:
        self.auth = auth
        self.config = config or MercadoLibreConfig()
        if self.config.page_size < 1 or self.config.batch_size < 1:
            raise ValueError("Mercado Libre page and batch sizes must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.max_attempts = max_attempts
        self._client = client
        self._owns_client = client is None
        self._timeout = httpx.Timeout(timeout)
        self.last_raw_result_count = 0
        self.last_filtered_count = 0

    async def __aenter__(self) -> MercadoLibreAdapter:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def default_context() -> RetailerContext:
        return RetailerContext(
            fulfillment_mode=FulfillmentMode.GENERIC,
            store_name="Mercado Libre Argentina marketplace",
            context_resolution=ContextResolution.GENERIC,
        )

    async def search_products(
        self, query: str, *, context: RetailerContext | None = None
    ) -> list[ProductObservation]:
        if not query.strip():
            raise ValueError("query must not be blank")
        self.auth.require_access_token()
        active_context = context or self.default_context()
        observed_at = datetime.now(UTC)
        observations: list[ProductObservation] = []
        offset = 0
        self.last_raw_result_count = 0
        self.last_filtered_count = 0

        while offset < self.config.max_results:
            payload = await self._request_json(
                "GET",
                self.config.search_path.format(site_id=self.config.site_id),
                params={"q": query, "limit": self.config.page_size, "offset": offset},
            )
            results, total, window_limit = self._validate_search(payload)
            self.last_raw_result_count += len(results)
            candidates = [result for result in results if self._is_whisky_listing(result)]
            self.last_filtered_count += len(results) - len(candidates)
            details = await self._enrich(candidates)
            for detail in details:
                observation = self._parse_item(
                    detail, context=active_context, observed_at=observed_at
                )
                if observation is not None:
                    observations.append(observation)

            consumed = offset + len(results)
            effective_limit = min(total, window_limit, self.config.max_results)
            if not results or consumed >= effective_limit or len(results) < self.config.page_size:
                break
            offset += len(results)
            if self.config.request_delay:
                await asyncio.sleep(self.config.request_delay)
        return observations

    async def _enrich(self, results: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        if not results:
            return []
        by_id = {self._required_text(item, "id", context="search result"): item for item in results}
        enriched: list[Mapping[str, Any]] = []
        ids = list(by_id)
        for start in range(0, len(ids), self.config.batch_size):
            batch = ids[start : start + self.config.batch_size]
            payload = await self._request_json(
                "GET", self.config.items_path, params={"ids": ",".join(batch)}
            )
            if not isinstance(payload, list):
                raise MercadoLibreSchemaError("Mercado Libre batch items response must be an array")
            bodies: dict[str, Mapping[str, Any]] = {}
            for entry in payload:
                if not isinstance(entry, Mapping):
                    raise MercadoLibreSchemaError("Mercado Libre batch entry must be an object")
                body = entry.get("body")
                code = entry.get("code")
                if code == 200 and isinstance(body, Mapping) and isinstance(body.get("id"), str):
                    bodies[body["id"]] = body
            for item_id in batch:
                enriched.append(bodies.get(item_id, by_id[item_id]))
        return enriched

    async def _request_json(
        self, method: str, path: str, *, params: Mapping[str, Any] | None = None
    ) -> Any:
        refreshed = False
        for attempt in range(self.max_attempts):
            token = self.auth.require_access_token()
            try:
                response = await self._get_client().request(
                    method,
                    f"{self.config.api_base_url}{path}",
                    params=params,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "WhiskyTracker/0.1",
                    },
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.max_attempts - 1:
                    raise MercadoLibreError("Mercado Libre API request failed") from exc
                await asyncio.sleep(0.25 * (2**attempt))
                continue

            if response.status_code == 401:
                if not refreshed and self.auth.can_refresh:
                    await self.auth.refresh(
                        self._get_client(), f"{self.config.api_base_url}{self.config.token_path}"
                    )
                    refreshed = True
                    continue
                raise MercadoLibreTokenExpiredError(
                    "Mercado Libre rejected the access token and it could not be refreshed"
                )
            if response.status_code == 403:
                raise MercadoLibreAuthorizationError("Mercado Libre denied API access (HTTP 403)")
            if response.status_code in _TRANSIENT_STATUSES:
                if attempt == self.max_attempts - 1:
                    if response.status_code == 429:
                        raise MercadoLibreRateLimitError(
                            "Mercado Libre rate limit persisted after retries"
                        )
                    raise MercadoLibreError(
                        f"Mercado Libre API returned HTTP {response.status_code} after retries"
                    )
                await asyncio.sleep(0.25 * (2**attempt))
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise MercadoLibreError(
                    f"Mercado Libre API returned HTTP {response.status_code}"
                ) from exc
            try:
                return response.json()
            except ValueError as exc:
                raise MercadoLibreSchemaError("Mercado Libre API returned invalid JSON") from exc
        raise AssertionError("retry loop exited unexpectedly")

    @staticmethod
    def _validate_search(
        payload: Any,
    ) -> tuple[list[Mapping[str, Any]], int, int]:
        if not isinstance(payload, Mapping):
            raise MercadoLibreSchemaError("Mercado Libre search response must be an object")
        results = payload.get("results")
        paging = payload.get("paging")
        if not isinstance(results, list) or not all(isinstance(item, Mapping) for item in results):
            raise MercadoLibreSchemaError("Mercado Libre search results must be an object array")
        if not isinstance(paging, Mapping):
            raise MercadoLibreSchemaError("Mercado Libre search response has no paging object")
        total = paging.get("total")
        limit = paging.get("limit")
        if not isinstance(total, int) or not isinstance(limit, int) or total < 0 or limit < 1:
            raise MercadoLibreSchemaError("Mercado Libre paging total/limit are invalid")
        # A reported total is not permission to bypass the API's finite offset window.
        window_limit = min(total, 1000)
        return results, total, window_limit

    def _parse_item(
        self,
        item: Mapping[str, Any],
        *,
        context: RetailerContext,
        observed_at: datetime,
    ) -> ProductObservation | None:
        item_id = self._required_text(item, "id", context="item")
        title = self._required_text(item, "title", context=item_id)
        price = self._decimal(item.get("price"), field="price", item_id=item_id)
        if price <= 0:
            return None
        attributes = item.get("attributes") or []
        if not isinstance(attributes, list) or not all(
            isinstance(attribute, Mapping) for attribute in attributes
        ):
            raise MercadoLibreSchemaError(f"item {item_id!r} has invalid attributes")
        size_value, size_unit = self._extract_volume(attributes, title)
        seller = item.get("seller")
        seller_id = None
        if isinstance(seller, Mapping):
            seller_id = self._text(seller.get("id")) or None
        seller_id = seller_id or self._text(item.get("seller_id")) or None
        official_store_id = self._text(item.get("official_store_id")) or None
        item_context = RetailerContext(
            fulfillment_mode=context.fulfillment_mode,
            postal_code=context.postal_code,
            coordinates=context.coordinates,
            sales_channel=context.sales_channel,
            region_id=context.region_id,
            seller_id=seller_id,
            store_id=official_store_id,
            store_name=context.store_name,
            context_resolution=context.context_resolution,
        )
        available = item.get("available_quantity")
        status = self._text(item.get("status"))
        if available is not None and (
            not isinstance(available, int) or isinstance(available, bool)
        ):
            raise MercadoLibreSchemaError(f"item {item_id!r} has invalid available_quantity")
        permalink = self._required_text(item, "permalink", context=item_id)
        original_price = self._optional_decimal(
            item.get("original_price"), field="original_price", item_id=item_id
        )
        variation_id = self._variation_id(item)
        return ProductObservation(
            retailer="Mercado Libre Argentina",
            retailer_product_id=item_id,
            retailer_sku_id=variation_id or item_id,
            catalog_product_id=self._text(item.get("catalog_product_id")) or None,
            gtin=self._extract_gtin(attributes),
            title=title,
            brand=self._attribute_value(attributes, _BRAND_ATTRIBUTE_IDS),
            size_value=size_value,
            size_unit=size_unit,
            pack_count=self._extract_pack_count(attributes, title),
            currency=self._required_text(item, "currency_id", context=item_id),
            current_price=price,
            regular_price=original_price,
            promotions=(),
            condition=self._text(item.get("condition")) or None,
            available_quantity=available,
            in_stock=(available is None or available > 0) and status in {"", "active"},
            product_url=permalink,
            context=item_context,
            observed_at=observed_at,
        )

    @classmethod
    def _is_whisky_listing(cls, item: Mapping[str, Any]) -> bool:
        title = cls._text(item.get("title"))
        if not title or _OBVIOUS_NON_WHISKY.search(title):
            return False
        combined = " ".join(
            (title, cls._text(item.get("category_id")), cls._text(item.get("domain_id")))
        ).casefold()
        # Search is already whisky-scoped: retain uncertain listings unless clearly non-product.
        return "whisk" in combined or "bourbon" in combined or "scotch" in combined

    @classmethod
    def _extract_gtin(cls, attributes: Sequence[Mapping[str, Any]]) -> str | None:
        value = cls._attribute_value(attributes, _GTIN_ATTRIBUTE_IDS)
        if not value:
            return None
        digits = re.sub(r"\D", "", value)
        if len(digits) not in {8, 12, 13, 14} or not cls._valid_gtin_check_digit(digits):
            return None
        return digits

    @staticmethod
    def _valid_gtin_check_digit(digits: str) -> bool:
        total = sum(
            int(character) * (3 if index % 2 == 0 else 1)
            for index, character in enumerate(reversed(digits[:-1]))
        )
        return (10 - total % 10) % 10 == int(digits[-1])

    @classmethod
    def _extract_volume(
        cls, attributes: Sequence[Mapping[str, Any]], title: str
    ) -> tuple[Decimal | None, str | None]:
        raw = cls._attribute_value(attributes, _VOLUME_ATTRIBUTE_IDS) or title
        match = _SIZE_PATTERN.search(raw)
        if not match:
            return None, None
        unit = match.group("unit").casefold()
        normalized_unit = "ml" if unit in {"ml", "cc"} else "l"
        return Decimal(match.group("value").replace(",", ".")), normalized_unit

    @classmethod
    def _extract_pack_count(cls, attributes: Sequence[Mapping[str, Any]], title: str) -> int | None:
        raw = cls._attribute_value(attributes, _PACK_ATTRIBUTE_IDS)
        if raw:
            digits = re.search(r"\d+", raw)
            if digits and int(digits.group()) > 0:
                return int(digits.group())
        for pattern in _PACK_PATTERNS:
            match = pattern.search(title)
            if match and int(match.group("count")) > 0:
                return int(match.group("count"))
        return 1

    @classmethod
    def _attribute_value(
        cls, attributes: Sequence[Mapping[str, Any]], identifiers: set[str]
    ) -> str | None:
        for attribute in attributes:
            if cls._text(attribute.get("id")).upper() not in identifiers:
                continue
            value = cls._text(attribute.get("value_name"))
            if value:
                return value
            value_struct = attribute.get("value_struct")
            if isinstance(value_struct, Mapping):
                number = cls._text(value_struct.get("number"))
                unit = cls._text(value_struct.get("unit"))
                if number:
                    return " ".join(part for part in (number, unit) if part)
        return None

    @classmethod
    def _variation_id(cls, item: Mapping[str, Any]) -> str | None:
        variation_id = cls._text(item.get("variation_id"))
        if variation_id:
            return variation_id
        variations = item.get("variations")
        if (
            isinstance(variations, list)
            and len(variations) == 1
            and isinstance(variations[0], Mapping)
        ):
            return cls._text(variations[0].get("id")) or None
        return None

    @staticmethod
    def _text(value: Any) -> str:
        if value is None or isinstance(value, bool):
            return ""
        return str(value).strip()

    @classmethod
    def _required_text(cls, item: Mapping[str, Any], field: str, *, context: str) -> str:
        value = cls._text(item.get(field))
        if not value:
            raise MercadoLibreSchemaError(f"Mercado Libre {context} has no {field}")
        return value

    @staticmethod
    def _decimal(value: Any, *, field: str, item_id: str) -> Decimal:
        parsed = MercadoLibreAdapter._optional_decimal(value, field=field, item_id=item_id)
        if parsed is None:
            raise MercadoLibreSchemaError(f"item {item_id!r} has invalid {field}")
        return parsed

    @staticmethod
    def _optional_decimal(value: Any, *, field: str, item_id: str) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise MercadoLibreSchemaError(f"item {item_id!r} has invalid {field}")
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise MercadoLibreSchemaError(f"item {item_id!r} has invalid {field}") from exc

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client
