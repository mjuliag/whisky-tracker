"""Coto Digital frontend-BFF adapter."""

import asyncio
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin

import httpx

from whisky_tracker.models.context import ContextResolution, FulfillmentMode, RetailerContext
from whisky_tracker.models.product import ProductObservation
from whisky_tracker.models.promotion import DiscountType, Promotion, PromotionKind
from whisky_tracker.product_filter import is_obvious_non_whisky_title
from whisky_tracker.retailers.base import RetailerError

_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
_PERCENT_PATTERN = re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*%")
_SIZE_PATTERN = re.compile(
    r"(?<!\d)(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>ml|cc|cl|l|lt|lts|litros?)\b",
    re.IGNORECASE,
)
_PACK_PATTERN = re.compile(r"\b(?:pack|caja|estuche)?\s*x\s*(?P<count>\d+)\b", re.IGNORECASE)


class CotoError(RetailerError):
    """Base error raised by the Coto adapter."""


class CotoSchemaError(CotoError):
    """The Coto BFF response no longer has the expected shape."""


class CotoContextError(CotoError):
    """The requested Coto context is unsupported or incomplete."""


@dataclass(frozen=True, slots=True)
class CotoConfig:
    """Replaceable values used by Coto's deployed public frontend."""

    endpoint: str = (
        "https://api.coto.com.ar/api/v1/ms-digital-sitio-bff-web/api/v1/products/search/{query}"
    )
    storefront_url: str = "https://www.coto.com.ar/productos/"
    frontend_key: str = "key_r6xzz4IAoTWcipni"
    branch_id: str = "200"
    store_name: str = "Coto Digital default"
    page_size: int = 24
    request_delay: float = 0.15


class CotoAdapter:
    """Retrieve Coto products for an explicitly selected digital branch."""

    def __init__(
        self,
        *,
        config: CotoConfig | None = None,
        client: httpx.AsyncClient | None = None,
        client_uuid: str | None = None,
        timeout: float = 15.0,
        max_attempts: int = 3,
    ) -> None:
        self.config = config or CotoConfig()
        if self.config.page_size < 1:
            raise ValueError("Coto page_size must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.client_uuid = client_uuid or str(uuid.uuid4())
        self.max_attempts = max_attempts
        self._client = client
        self._owns_client = client is None
        self._timeout = httpx.Timeout(timeout)

    async def __aenter__(self) -> CotoAdapter:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def default_context(self) -> RetailerContext:
        """Return the generic Coto Digital context; it is not location-resolved."""
        return RetailerContext(
            fulfillment_mode=FulfillmentMode.DIGITAL,
            store_id=self.config.branch_id,
            store_name=self.config.store_name,
            context_resolution=ContextResolution.GENERIC,
        )

    async def search_products(
        self, query: str, *, context: RetailerContext | None = None
    ) -> list[ProductObservation]:
        if not query.strip():
            raise ValueError("query must not be blank")
        active_context = context or self.default_context()
        if not active_context.store_id:
            raise CotoContextError("Coto retrieval requires a store/branch ID")

        observations: list[ProductObservation] = []
        page = 1
        processed = 0
        observed_at = datetime.now(UTC)
        while True:
            payload = await self._request_page(query, page, active_context.store_id)
            results, total = self._validate_page(payload)
            for result in results:
                observation = self._parse_product(
                    result, context=active_context, observed_at=observed_at
                )
                if observation is not None:
                    observations.append(observation)
            processed += len(results)
            if processed >= total:
                break
            if not results:
                raise CotoSchemaError("Coto pagination ended before the advertised total")
            page += 1
            if self.config.request_delay:
                await asyncio.sleep(self.config.request_delay)
        return observations

    async def _request_page(self, query: str, page: int, branch_id: str) -> Any:
        url = self.config.endpoint.format(query=httpx.URL(query).raw_path.decode())
        params = [
            ("key", self.config.frontend_key),
            ("num_results_per_page", str(self.config.page_size)),
            ("page", str(page)),
            ("pre_filter_expression", f'{{"name":"store_availability","value":"{branch_id}"}}'),
            ("c", self.client_uuid),
            ("us", branch_id),
        ]
        for attempt in range(self.max_attempts):
            try:
                response = await self._get_client().get(
                    url,
                    params=params,
                    headers={"Accept": "application/json", "User-Agent": "WhiskyTracker/0.1"},
                )
                if response.status_code not in _TRANSIENT_STATUSES:
                    response.raise_for_status()
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise CotoSchemaError("Coto response was not valid JSON") from exc
                if attempt == self.max_attempts - 1:
                    response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise CotoError(
                    f"Coto BFF returned HTTP {exc.response.status_code} for page {page}"
                ) from exc
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.max_attempts - 1:
                    raise CotoError(f"Coto BFF request failed for page {page}") from exc
            await asyncio.sleep(0.25 * (2**attempt))
        raise AssertionError("retry loop exited unexpectedly")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client

    @staticmethod
    def _validate_page(payload: Any) -> tuple[list[Mapping[str, Any]], int]:
        if not isinstance(payload, Mapping):
            raise CotoSchemaError("Coto search response must be an object")
        response = payload.get("response")
        if not isinstance(response, Mapping):
            raise CotoSchemaError("Coto search response has no response object")
        results = response.get("results")
        total = response.get("total_num_results")
        if not isinstance(results, list) or not all(isinstance(item, Mapping) for item in results):
            raise CotoSchemaError("Coto response.results must be an array of objects")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise CotoSchemaError("Coto response.total_num_results must be a non-negative integer")
        return results, total

    def _parse_product(
        self,
        result: Mapping[str, Any],
        *,
        context: RetailerContext,
        observed_at: datetime,
    ) -> ProductObservation | None:
        data = result.get("data")
        if not isinstance(data, Mapping):
            raise CotoSchemaError("Coto result has no data object")
        product_id = self._required_text(data, "id")
        sku_id = self._required_text(data, "sku_id")
        title = self._text(data.get("sku_display_name")) or self._text(result.get("value"))
        if not title:
            raise CotoSchemaError(f"Coto product {product_id!r} has no title")
        if is_obvious_non_whisky_title(title):
            return None

        branch_id = context.store_id
        prices = data.get("price")
        if not isinstance(prices, list):
            raise CotoSchemaError(f"Coto product {product_id!r} has no valid price array")
        branch_price = next(
            (
                price
                for price in prices
                if isinstance(price, Mapping) and self._text(price.get("store")) == branch_id
            ),
            None,
        )
        if branch_price is None:
            return None
        regular_price = self._money(branch_price.get("listPrice"))
        if regular_price is None or regular_price <= 0:
            return None

        promotions, explicit_general_price = self._promotions(data, branch_id=branch_id or "")
        current_price = explicit_general_price or regular_price
        availability = data.get("store_availability")
        if not isinstance(availability, list):
            raise CotoSchemaError(f"Coto product {product_id!r} has invalid store_availability")
        in_stock = any(self._text(store) == branch_id for store in availability)
        size_value, size_unit = self._size(title)

        return ProductObservation(
            retailer="Coto",
            retailer_product_id=product_id,
            retailer_sku_id=sku_id,
            catalog_product_id=None,
            gtin=self._text(data.get("product_main_ean")) or None,
            title=title,
            brand=self._text(data.get("product_brand")) or None,
            size_value=size_value,
            size_unit=size_unit,
            pack_count=self._pack_count(title),
            currency="ARS",
            current_price=current_price,
            regular_price=regular_price,
            promotions=tuple(promotions),
            condition=None,
            available_quantity=None,
            in_stock=in_stock,
            product_url=self._product_url(data.get("url")),
            context=context,
            observed_at=observed_at,
        )

    def _promotions(
        self, data: Mapping[str, Any], *, branch_id: str
    ) -> tuple[list[Promotion], Decimal | None]:
        promotions: list[Promotion] = []
        applied_prices: list[Decimal] = []
        discounts = data.get("discounts") or []
        payment_methods = data.get("discounts_payment_methods") or []
        if not isinstance(discounts, list) or not isinstance(payment_methods, list):
            raise CotoSchemaError("Coto promotion fields must be arrays")

        for entry in discounts:
            if not isinstance(entry, Mapping):
                continue
            pieces = [
                self._text(entry.get(key))
                for key in ("discountText", "comments", "takingText", "regularPriceText")
            ]
            combined = " ".join(piece for piece in pieces if piece)
            kind = self._promotion_kind(combined)
            discounted = self._money(entry.get("discountPrice"))
            applied = kind is PromotionKind.GENERAL and discounted is not None
            if applied and discounted is not None:
                applied_prices.append(discounted)
            percentage = _PERCENT_PATTERN.search(combined)
            conditions = self._conditions(entry, branch_id=branch_id)
            promotions.append(
                Promotion(
                    name=pieces[0] or pieces[1] or "Descuento Coto",
                    kind=kind,
                    applied_to_current_price=applied,
                    discount_value=(
                        Decimal(percentage.group("value").replace(",", ".")) if percentage else None
                    ),
                    discount_type=DiscountType.PERCENTAGE if percentage else None,
                    conditions=conditions,
                )
            )

        for entry in payment_methods:
            if not isinstance(entry, Mapping):
                continue
            comments = self._text(entry.get("comentarios"))
            installments = self._text(entry.get("cantidadCuotas"))
            installment_price = self._text(entry.get("precioCuota"))
            image = self._text(entry.get("imagenDescuento"))
            details = tuple(
                value
                for value in (
                    f"installments={installments}" if installments else "",
                    f"installment_price={installment_price}" if installment_price else "",
                    f"image={image}" if image else "",
                    f"store={branch_id}" if branch_id else "",
                )
                if value
            )
            promotions.append(
                Promotion(
                    name=comments or "Promoción con medio de pago",
                    kind=PromotionKind.PAYMENT_METHOD,
                    applied_to_current_price=False,
                    conditions=details,
                )
            )
        return promotions, min(applied_prices) if applied_prices else None

    @staticmethod
    def _promotion_kind(text: str) -> PromotionKind:
        lowered = text.casefold()
        if any(marker in lowered for marker in ("comunidad coto", "comunidad", "socio")):
            return PromotionKind.LOYALTY
        return PromotionKind.GENERAL

    @classmethod
    def _conditions(cls, entry: Mapping[str, Any], *, branch_id: str) -> tuple[str, ...]:
        values = []
        for key in ("id", "comments", "takingText", "regularPriceText"):
            value = cls._text(entry.get(key))
            if value:
                values.append(f"{key}={value}")
        if branch_id:
            values.append(f"store={branch_id}")
        return tuple(values)

    def _product_url(self, raw: Any) -> str:
        value = self._text(raw)
        if not value:
            raise CotoSchemaError("Coto product has no URL")
        if value.startswith(("http://", "https://")):
            return value
        if value.startswith("/productos/"):
            return urljoin(self.config.storefront_url, value)
        if value.startswith("productos/"):
            value = value.removeprefix("productos/")
        return urljoin(self.config.storefront_url, value.lstrip("/"))

    @staticmethod
    def _money(value: Any) -> Decimal | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        cleaned = str(value).strip().replace("$", "").replace(" ", "")
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None

    @classmethod
    def _required_text(cls, data: Mapping[str, Any], key: str) -> str:
        value = cls._text(data.get(key))
        if not value:
            raise CotoSchemaError(f"Coto product has no {key}")
        return value

    @staticmethod
    def _text(value: Any) -> str:
        if value is None or isinstance(value, bool):
            return ""
        return str(value).strip()

    @staticmethod
    def _size(title: str) -> tuple[Decimal | None, str | None]:
        match = _SIZE_PATTERN.search(title)
        if not match:
            return None, None
        unit = match.group("unit").lower()
        unit = "ml" if unit in {"ml", "cc"} else "l"
        return Decimal(match.group("value").replace(",", ".")), unit

    @staticmethod
    def _pack_count(title: str) -> int | None:
        match = _PACK_PATTERN.search(title)
        return int(match.group("count")) if match else None
