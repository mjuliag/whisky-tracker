"""Shared adapter for retailers using VTEX's storefront search API."""

import asyncio
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from whisky_tracker.models.context import RetailerContext
from whisky_tracker.models.product import ProductObservation
from whisky_tracker.models.promotion import DiscountType, Promotion, PromotionKind
from whisky_tracker.retailers.base import RetailerError

_RESOURCES_PATTERN = re.compile(r"^(?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+)$")
_SIZE_PATTERN = re.compile(
    r"(?<!\d)(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>ml|cc|cl|l|lt|lts|litros?)\b",
    re.IGNORECASE,
)
_PACK_PATTERN = re.compile(r"\b(?:pack|caja|estuche)\s*(?:x|de)?\s*(?P<count>\d+)\b", re.IGNORECASE)
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
_PERCENT_PATTERN = re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*%")


class VtexSchemaError(RetailerError):
    """The VTEX response did not have the expected storefront schema."""


class VtexAdapter:
    """Retrieve and normalize products from a VTEX catalog search endpoint."""

    def __init__(
        self,
        *,
        retailer: str,
        endpoint: str,
        client: httpx.AsyncClient | None = None,
        page_size: int = 50,
        timeout: float = 15.0,
        max_attempts: int = 3,
    ) -> None:
        if not 1 <= page_size <= 50:
            raise ValueError("VTEX page_size must be between 1 and 50")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.retailer = retailer
        self.endpoint = endpoint
        self.page_size = page_size
        self.max_attempts = max_attempts
        self._client = client
        self._owns_client = client is None
        self._timeout = httpx.Timeout(timeout)

    async def __aenter__(self) -> VtexAdapter:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally managed HTTP client, if one was created."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search_products(
        self, query: str, *, context: RetailerContext | None = None
    ) -> list[ProductObservation]:
        """Return all SKU observations found by a VTEX text search."""
        if not query.strip():
            raise ValueError("query must not be blank")

        resolved_context = context or RetailerContext()
        observations: list[ProductObservation] = []
        offset = 0
        known_total: int | None = None
        observed_at = datetime.now(UTC)

        while True:
            end = offset + self.page_size - 1
            if known_total is not None:
                end = min(end, known_total - 1)
            response = await self._request_page(
                query=query,
                start=offset,
                end=end,
                context=resolved_context,
            )
            payload = self._validate_page(response)
            for product in payload:
                observations.extend(
                    self._parse_product(
                        product,
                        observed_at=observed_at,
                        context=resolved_context,
                    )
                )

            total = self.parse_resources_header(response.headers.get("resources"))
            if total is not None:
                known_total = total
            if total is None:
                if len(payload) < self.page_size:
                    break
            elif offset + len(payload) >= total:
                break
            if not payload:
                raise VtexSchemaError("VTEX pagination ended before the advertised total")
            offset += self.page_size

        return observations

    async def _request_page(
        self,
        *,
        query: str,
        start: int,
        end: int,
        context: RetailerContext,
    ) -> httpx.Response:
        client = self._get_client()
        for attempt in range(self.max_attempts):
            try:
                response = await client.get(
                    self.endpoint,
                    params={
                        "ft": query,
                        "_from": start,
                        "_to": end,
                        **({"sc": context.sales_channel} if context.sales_channel else {}),
                    },
                    headers={"Accept": "application/json", "User-Agent": "WhiskyTracker/0.1"},
                )
                if response.status_code not in _TRANSIENT_STATUS_CODES:
                    response.raise_for_status()
                    return response
                if attempt == self.max_attempts - 1:
                    response.raise_for_status()
            except httpx.TimeoutException, httpx.NetworkError:
                if attempt == self.max_attempts - 1:
                    raise
            await asyncio.sleep(0.25 * (2**attempt))
        raise AssertionError("retry loop exited unexpectedly")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client

    @staticmethod
    def parse_resources_header(value: str | None) -> int | None:
        """Return the total from VTEX's ``start-end/total`` response header."""
        if value is None:
            return None
        match = _RESOURCES_PATTERN.fullmatch(value.strip())
        if not match:
            raise VtexSchemaError(f"invalid VTEX resources header: {value!r}")
        if int(match["end"]) < int(match["start"]):
            raise VtexSchemaError(f"invalid VTEX resources range: {value!r}")
        return int(match["total"])

    @staticmethod
    def _validate_page(response: httpx.Response) -> list[Mapping[str, Any]]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise VtexSchemaError("VTEX response was not valid JSON") from exc
        if not isinstance(payload, list):
            raise VtexSchemaError("VTEX search response must be a JSON array")
        if not all(isinstance(product, Mapping) for product in payload):
            raise VtexSchemaError("VTEX search response contains a non-object product")
        return payload

    def _parse_product(
        self,
        product: Mapping[str, Any],
        *,
        observed_at: datetime,
        context: RetailerContext,
    ) -> list[ProductObservation]:
        product_id = self._required_string(product, "productId", context="product")
        product_name = self._required_string(product, "productName", context=product_id)
        items = product.get("items")
        if not isinstance(items, list):
            raise VtexSchemaError(f"product {product_id!r} has no valid items array")

        observations: list[ProductObservation] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise VtexSchemaError(f"product {product_id!r} contains an invalid SKU")
            observation = self._parse_sku(
                product,
                item,
                product_id=product_id,
                product_name=product_name,
                observed_at=observed_at,
                context=context,
            )
            if observation is not None:
                observations.append(observation)
        return observations

    def _parse_sku(
        self,
        product: Mapping[str, Any],
        item: Mapping[str, Any],
        *,
        product_id: str,
        product_name: str,
        observed_at: datetime,
        context: RetailerContext,
    ) -> ProductObservation | None:
        sku_id = self._required_string(item, "itemId", context=f"product {product_id}")
        sellers = item.get("sellers")
        if not isinstance(sellers, list):
            raise VtexSchemaError(f"SKU {sku_id!r} has no valid sellers array")
        seller = self._select_seller(sellers, sku_id=sku_id)
        if seller is None:
            return None
        offer = seller["commertialOffer"]

        current_price = self._decimal(offer.get("Price"), field="Price", sku_id=sku_id)
        if current_price <= 0:
            return None
        list_price = self._optional_decimal(
            offer.get("ListPrice"), field="ListPrice", sku_id=sku_id
        )
        title = self._first_string(item.get("nameComplete"), item.get("name"), product_name)
        size_value, size_unit = self._extract_size(title)
        promotions = self._extract_promotions(offer)
        product_url = self._first_string(product.get("link"))
        if not product_url:
            link_text = self._first_string(product.get("linkText"))
            if not link_text:
                raise VtexSchemaError(f"product {product_id!r} has no URL")
            origin = str(httpx.URL(self.endpoint).copy_with(path="", query=None))
            product_url = f"{origin.rstrip('/')}/{link_text}/p"

        quantity = offer.get("AvailableQuantity")
        if not isinstance(quantity, int | float):
            raise VtexSchemaError(f"SKU {sku_id!r} has invalid AvailableQuantity")

        return ProductObservation(
            retailer=self.retailer,
            retailer_product_id=product_id,
            retailer_sku_id=sku_id,
            catalog_product_id=None,
            gtin=self._first_string(item.get("ean")) or None,
            title=title,
            brand=self._first_string(product.get("brand")) or None,
            size_value=size_value,
            size_unit=size_unit,
            pack_count=self._extract_pack_count(title),
            currency="ARS",
            current_price=current_price,
            regular_price=list_price,
            promotions=tuple(promotions),
            condition=None,
            available_quantity=int(quantity),
            in_stock=quantity > 0,
            product_url=product_url,
            context=context,
            observed_at=observed_at,
        )

    @classmethod
    def _select_seller(cls, sellers: Sequence[Any], *, sku_id: str) -> Mapping[str, Any] | None:
        candidates: list[Mapping[str, Any]] = []
        for seller in sellers:
            if not isinstance(seller, Mapping):
                raise VtexSchemaError(f"SKU {sku_id!r} contains an invalid seller")
            offer = seller.get("commertialOffer")
            if not isinstance(offer, Mapping):
                raise VtexSchemaError(f"SKU {sku_id!r} seller has no valid commertialOffer")
            price = cls._optional_decimal(offer.get("Price"), field="Price", sku_id=sku_id)
            if price is not None and price > 0:
                candidates.append(seller)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda seller: (
                float(seller["commertialOffer"].get("AvailableQuantity") or 0) > 0,
                bool(seller.get("sellerDefault")),
            ),
        )

    @classmethod
    def _extract_promotions(cls, offer: Mapping[str, Any]) -> list[Promotion]:
        promotions: list[Promotion] = []
        groups = (
            (("discountHighlights", "DiscountHighLight"), True),
            (("teasers", "Teasers", "PromotionTeasers"), False),
        )
        for aliases, applied in groups:
            entries = cls._field(offer, *aliases) or []
            if not isinstance(entries, list):
                raise VtexSchemaError(f"commertialOffer.{aliases[0]} must be an array")
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                promotion = cls._parse_promotion(entry, applied=applied)
                if promotion and promotion not in promotions:
                    promotions.append(promotion)
        return promotions

    @classmethod
    def _parse_promotion(cls, entry: Mapping[str, Any], *, applied: bool) -> Promotion | None:
        name = cls._string_field(entry, "name", "Name", "<Name>k__BackingField")
        if not name:
            return None
        conditions_data = cls._field(
            entry, "conditions", "Conditions", "<Conditions>k__BackingField"
        )
        effects_data = cls._field(entry, "effects", "Effects", "<Effects>k__BackingField")
        conditions, condition_params = cls._parameters(conditions_data)
        _, effect_params = cls._parameters(effects_data)
        minimum = cls._mapping_decimal(
            conditions_data,
            "minimumQuantity",
            "MinimumQuantity",
            "<MinimumQuantity>k__BackingField",
        )
        percentage = cls._parameter_decimal(effect_params, "PercentualDiscount")
        if percentage is None:
            match = _PERCENT_PATTERN.search(name)
            percentage = Decimal(match["value"].replace(",", ".")) if match else None

        normalized = name.casefold()
        if "mi crf" in normalized or "mi carrefour" in normalized:
            kind = PromotionKind.LOYALTY
        elif not applied and (
            "tarjeta" in normalized
            or "restrictionsbins" in {key.casefold() for key in condition_params}
        ):
            kind = PromotionKind.PAYMENT_METHOD
        elif minimum is not None and minimum > 1:
            kind = PromotionKind.QUANTITY
        else:
            kind = PromotionKind.GENERAL

        return Promotion(
            name=name,
            kind=kind,
            applied_to_current_price=applied,
            discount_value=percentage,
            discount_type=DiscountType.PERCENTAGE if percentage is not None else None,
            minimum_quantity=minimum,
            conditions=conditions,
        )

    @classmethod
    def _parameters(cls, container: Any) -> tuple[tuple[str, ...], dict[str, str]]:
        if not isinstance(container, Mapping):
            return (), {}
        values = cls._field(container, "parameters", "Parameters", "<Parameters>k__BackingField")
        if not isinstance(values, list):
            return (), {}
        result: dict[str, str] = {}
        for parameter in values:
            if not isinstance(parameter, Mapping):
                continue
            key = cls._string_field(parameter, "name", "Name", "<Name>k__BackingField")
            value = cls._string_field(parameter, "value", "Value", "<Value>k__BackingField")
            if key:
                result[key] = value
        return tuple(f"{key}={value}" for key, value in result.items()), result

    @staticmethod
    def _field(data: Mapping[str, Any], *aliases: str) -> Any:
        return next((data[alias] for alias in aliases if alias in data), None)

    @classmethod
    def _string_field(cls, data: Mapping[str, Any], *aliases: str) -> str:
        return cls._first_string(*(data.get(alias) for alias in aliases))

    @classmethod
    def _mapping_decimal(cls, data: Any, *aliases: str) -> Decimal | None:
        if not isinstance(data, Mapping):
            return None
        value = cls._field(data, *aliases)
        if value is None or isinstance(value, bool):
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation, ValueError:
            return None

    @staticmethod
    def _parameter_decimal(parameters: Mapping[str, str], key: str) -> Decimal | None:
        value = next(
            (value for name, value in parameters.items() if name.casefold() == key.casefold()), None
        )
        if value is None:
            return None
        try:
            return Decimal(value.replace(",", "."))
        except InvalidOperation:
            return None

    @staticmethod
    def _extract_size(title: str) -> tuple[Decimal | None, str | None]:
        match = _SIZE_PATTERN.search(title)
        if not match:
            return None, None
        value = Decimal(match["value"].replace(",", "."))
        unit = match["unit"].lower()
        normalized_unit = "ml" if unit in {"ml", "cc", "cl"} else "l"
        if unit == "cl":
            value *= 10
        return value, normalized_unit

    @staticmethod
    def _extract_pack_count(title: str) -> int | None:
        match = _PACK_PATTERN.search(title)
        return int(match["count"]) if match else None

    @staticmethod
    def _required_string(data: Mapping[str, Any], key: str, *, context: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise VtexSchemaError(f"{context} has no valid {key}")
        return value.strip()

    @staticmethod
    def _first_string(*values: Any) -> str:
        return next(
            (value.strip() for value in values if isinstance(value, str) and value.strip()), ""
        )

    @staticmethod
    def _decimal(value: Any, *, field: str, sku_id: str) -> Decimal:
        result = VtexAdapter._optional_decimal(value, field=field, sku_id=sku_id)
        if result is None:
            raise VtexSchemaError(f"SKU {sku_id!r} has no valid {field}")
        return result

    @staticmethod
    def _optional_decimal(value: Any, *, field: str, sku_id: str) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            raise VtexSchemaError(f"SKU {sku_id!r} has invalid {field}")
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise VtexSchemaError(f"SKU {sku_id!r} has invalid {field}") from exc
