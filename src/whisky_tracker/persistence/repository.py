"""Explicit SQLite repository for products, listings, and price history."""

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from whisky_tracker.matching.models import CanonicalProduct, MatchingResult
from whisky_tracker.matching.normalization import (
    extract_known_expression,
    pack_from_observation,
    volume_from_observation,
)
from whisky_tracker.models.context import ContextResolution, FulfillmentMode, RetailerContext
from whisky_tracker.models.product import ProductObservation
from whisky_tracker.models.promotion import DiscountType, Promotion, PromotionKind
from whisky_tracker.persistence.models import (
    HistoryFilter,
    ListingKey,
    PriceChange,
    StoredObservation,
    calculate_price_change,
)
from whisky_tracker.persistence.schema import LATEST_SCHEMA_VERSION, MIGRATIONS

DEFAULT_DATABASE_PATH = Path("data/whisky_tracker.db")


class PersistenceError(RuntimeError):
    """A persistence operation failed and its transaction was rolled back."""


class SQLiteRepository:
    """Own a SQLite connection and expose domain-oriented persistence operations."""

    def __init__(self, path: str | Path = DEFAULT_DATABASE_PATH) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> SQLiteRepository:
        self.connect()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self.connect()
        assert self._connection is not None
        return self._connection

    def connect(self) -> None:
        if self._connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        self._connection = connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def initialize(self) -> None:
        """Apply every unapplied schema migration; safe to call repeatedly."""
        connection = self.connection
        has_version_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        current = 0
        if has_version_table:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_version"
            ).fetchone()
            current = int(row["version"] or 0)
        try:
            for version, sql in enumerate(MIGRATIONS, start=1):
                if version <= current:
                    continue
                applied_at = _timestamp(datetime.now(UTC))
                connection.executescript(
                    f"BEGIN IMMEDIATE;\n{sql}\n"
                    f"INSERT INTO schema_version(version, applied_at) "
                    f"VALUES ({version}, '{applied_at}');\nCOMMIT;"
                )
        except sqlite3.Error as exc:
            connection.rollback()
            raise PersistenceError(f"schema migration failed: {exc}") from exc

    @property
    def schema_version(self) -> int:
        row = self.connection.execute(
            "SELECT MAX(version) AS version FROM schema_version"
        ).fetchone()
        return int(row["version"] or 0)

    def observation_count(self) -> int:
        self._require_schema()
        row = self.connection.execute("SELECT COUNT(*) AS count FROM observations").fetchone()
        return int(row["count"])

    def save_matching_result(self, result: MatchingResult) -> None:
        """Atomically persist matched groups and every unmatched observation."""
        self._require_schema()
        try:
            with self.connection:
                for group in result.groups:
                    canonical_pk = self._upsert_canonical(group.canonical_product)
                    for observation in group.observations:
                        self._save_observation(observation, canonical_pk)
                for observation in result.unmatched:
                    self._save_observation(observation, None)
        except (sqlite3.Error, ValueError) as exc:
            raise PersistenceError(f"matching result was not saved: {exc}") from exc

    def save_observations(self, observations: Iterable[ProductObservation]) -> None:
        """Atomically persist observations without assigning canonical identities."""
        self._require_schema()
        try:
            with self.connection:
                for observation in observations:
                    self._save_observation(observation, None)
        except (sqlite3.Error, ValueError) as exc:
            raise PersistenceError(f"observation batch was not saved: {exc}") from exc

    def upsert_canonical_product(self, product: CanonicalProduct) -> int:
        self._require_schema()
        try:
            with self.connection:
                return self._upsert_canonical(product)
        except sqlite3.Error as exc:
            raise PersistenceError(f"canonical product was not saved: {exc}") from exc

    def resolve_canonical_product(self, product: CanonicalProduct) -> CanonicalProduct:
        """Return the persisted identity, accounting for an already-known unique GTIN."""
        row = self._canonical_row(product)
        if row is None:
            raise PersistenceError("canonical product does not exist")
        gtins = self.connection.execute(
            "SELECT gtin FROM canonical_gtins WHERE canonical_product_id = ? ORDER BY gtin",
            (row["id"],),
        ).fetchall()
        return CanonicalProduct(
            canonical_id=row["canonical_id"],
            brand=row["brand"],
            expression=row["expression"],
            age_statement=row["age_statement"],
            volume_ml=row["volume_ml"],
            pack_count=row["pack_count"],
            gtins=frozenset(item["gtin"] for item in gtins),
        )

    def get_price_history(self, filters: HistoryFilter) -> tuple[StoredObservation, ...]:
        self._require_schema()
        clauses: list[str] = []
        parameters: list[object] = []
        if filters.listing:
            clauses.extend(
                (
                    "l.retailer = ?",
                    "l.retailer_product_id = ?",
                    "l.retailer_sku_id = ?",
                )
            )
            parameters.extend(
                (
                    filters.listing.retailer,
                    filters.listing.retailer_product_id,
                    filters.listing.retailer_sku_id,
                )
            )
        if filters.retailer:
            clauses.append("l.retailer = ?")
            parameters.append(filters.retailer)
        if filters.canonical_id:
            clauses.append("COALESCE(oc.canonical_id, lc.canonical_id) = ?")
            parameters.append(filters.canonical_id)
        if filters.context:
            clauses.append("o.context_key = ?")
            parameters.append(_context_key(filters.context))
        if filters.seller_id:
            clauses.append("o.seller_id = ?")
            parameters.append(filters.seller_id)
        if filters.store_id:
            clauses.append("o.store_id = ?")
            parameters.append(filters.store_id)
        if filters.sales_channel:
            clauses.append("o.sales_channel = ?")
            parameters.append(filters.sales_channel)
        if filters.start:
            clauses.append("o.observed_at >= ?")
            parameters.append(_timestamp(filters.start))
        if filters.end:
            clauses.append("o.observed_at <= ?")
            parameters.append(_timestamp(filters.end))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT o.*, l.retailer, l.retailer_product_id, l.retailer_sku_id, l.title,
                   l.product_url, l.volume_ml, l.pack_count,
                   COALESCE(oc.canonical_id, lc.canonical_id) AS effective_canonical_id
            FROM observations o
            JOIN retailer_listings l ON l.id = o.listing_id
            LEFT JOIN canonical_products oc ON oc.id = o.canonical_product_id
            LEFT JOIN canonical_products lc ON lc.id = l.canonical_product_id
            {where}
            ORDER BY o.observed_at, o.id
            """,
            parameters,
        ).fetchall()
        return tuple(self._stored_observation(row) for row in rows)

    def get_latest_price(self, filters: HistoryFilter) -> StoredObservation | None:
        history = self.get_price_history(filters)
        return history[-1] if history else None

    def get_previous_observation(self, filters: HistoryFilter) -> StoredObservation | None:
        history = self.get_price_history(filters)
        return history[-2] if len(history) >= 2 else None

    def get_historical_minimum(self, filters: HistoryFilter) -> StoredObservation | None:
        history = self.get_price_history(filters)
        return (
            min(history, key=lambda item: (item.current_price, item.observed_at))
            if history
            else None
        )

    def get_latest_price_change(self, filters: HistoryFilter) -> PriceChange | None:
        history = self.get_price_history(filters)
        if len(history) < 2:
            return None
        return calculate_price_change(history[-1], history[-2])

    def get_latest_canonical_observations(self, canonical_id: str) -> tuple[StoredObservation, ...]:
        """Return the latest row for every listing and exact commercial context."""
        history = self.get_price_history(HistoryFilter(canonical_id=canonical_id))
        latest: dict[tuple[int, tuple[object, ...]], StoredObservation] = {}
        for item in history:
            latest[(item.listing_id, _context_payload(item.context))] = item
        return tuple(latest.values())

    def record_alert_candidate(
        self,
        *,
        fingerprint: str,
        listing: ListingKey,
        observed_at: datetime,
        context: RetailerContext,
        canonical_id: str | None,
        alert_types: tuple[str, ...],
        price: Decimal,
        currency: str,
    ) -> None:
        """Record an eligible candidate without marking notification delivery successful."""
        self._require_schema()
        listing_row = self._listing_row(listing)
        observation_row = self.connection.execute(
            """SELECT id FROM observations
               WHERE listing_id = ? AND observed_at = ? AND context_key = ?
               ORDER BY id DESC LIMIT 1""",
            (listing_row["id"], _timestamp(observed_at), _context_key(context)),
        ).fetchone()
        canonical_pk = None
        if canonical_id:
            row = self.connection.execute(
                "SELECT id FROM canonical_products WHERE canonical_id = ?", (canonical_id,)
            ).fetchone()
            canonical_pk = row["id"] if row else None
        try:
            with self.connection:
                self.connection.execute(
                    """INSERT INTO alert_events(
                           fingerprint, canonical_product_id, listing_id, observation_id,
                           context_key, alert_types, price, currency, status, generated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                       ON CONFLICT(fingerprint) DO NOTHING""",
                    (
                        fingerprint,
                        canonical_pk,
                        listing_row["id"],
                        observation_row["id"] if observation_row else None,
                        _context_key(context),
                        json.dumps(alert_types, separators=(",", ":")),
                        str(price),
                        currency,
                        _timestamp(datetime.now(UTC)),
                    ),
                )
        except sqlite3.Error as exc:
            raise PersistenceError(f"alert candidate was not recorded: {exc}") from exc

    def is_alert_sent(self, fingerprint: str) -> bool:
        self._require_schema()
        row = self.connection.execute(
            "SELECT status FROM alert_events WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return bool(row and row["status"] == "sent")

    def mark_alert_sent(self, fingerprint: str, *, sent_at: datetime | None = None) -> None:
        """Mark a recorded candidate sent only after transport reports success."""
        self._require_schema()
        try:
            with self.connection:
                cursor = self.connection.execute(
                    """UPDATE alert_events SET status = 'sent', sent_at = ?
                       WHERE fingerprint = ?""",
                    (_timestamp(sent_at or datetime.now(UTC)), fingerprint),
                )
                if cursor.rowcount != 1:
                    raise ValueError("alert candidate does not exist")
        except (sqlite3.Error, ValueError) as exc:
            raise PersistenceError(f"alert was not marked sent: {exc}") from exc

    def _require_schema(self) -> None:
        try:
            if self.schema_version != LATEST_SCHEMA_VERSION:
                raise PersistenceError("database schema is not initialized or is out of date")
        except sqlite3.Error as exc:
            raise PersistenceError("database schema is not initialized") from exc

    def _upsert_canonical(self, product: CanonicalProduct) -> int:
        now = _timestamp(datetime.now(UTC))
        existing = self._canonical_row(product)
        canonical_id = existing["canonical_id"] if existing else product.canonical_id
        if existing:
            for attribute in ("age_statement", "volume_ml", "pack_count"):
                old_value = existing[attribute]
                new_value = getattr(product, attribute)
                if old_value is not None and new_value is not None and old_value != new_value:
                    raise ValueError(f"known GTIN conflicts with persisted canonical {attribute}")
            old_expression = extract_known_expression(existing["expression"] or "")
            new_expression = extract_known_expression(product.expression or "")
            if old_expression and new_expression and old_expression != new_expression:
                raise ValueError("known GTIN conflicts with persisted canonical expression")
        self.connection.execute(
            """
            INSERT INTO canonical_products(
                canonical_id, brand, expression, age_statement, volume_ml, pack_count,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_id) DO UPDATE SET
                brand = COALESCE(canonical_products.brand, excluded.brand),
                expression = COALESCE(canonical_products.expression, excluded.expression),
                age_statement = COALESCE(canonical_products.age_statement, excluded.age_statement),
                volume_ml = COALESCE(canonical_products.volume_ml, excluded.volume_ml),
                pack_count = COALESCE(canonical_products.pack_count, excluded.pack_count),
                updated_at = excluded.updated_at
            """,
            (
                canonical_id,
                product.brand,
                product.expression,
                product.age_statement,
                product.volume_ml,
                product.pack_count,
                now,
                now,
            ),
        )
        row = self.connection.execute(
            "SELECT id FROM canonical_products WHERE canonical_id = ?", (canonical_id,)
        ).fetchone()
        assert row is not None
        canonical_pk = int(row["id"])
        for gtin in product.gtins:
            self.connection.execute(
                """INSERT INTO canonical_gtins(canonical_product_id, gtin) VALUES (?, ?)
                   ON CONFLICT(canonical_product_id, gtin) DO NOTHING""",
                (canonical_pk, gtin),
            )
        return canonical_pk

    def _canonical_row(self, product: CanonicalProduct) -> sqlite3.Row | None:
        if product.gtins:
            placeholders = ",".join("?" for _ in product.gtins)
            rows = self.connection.execute(
                f"""SELECT DISTINCT c.* FROM canonical_products c
                      JOIN canonical_gtins g ON g.canonical_product_id = c.id
                      WHERE g.gtin IN ({placeholders})""",
                tuple(sorted(product.gtins)),
            ).fetchall()
            if len(rows) > 1:
                raise PersistenceError("GTINs resolve to multiple canonical products")
            if rows:
                return rows[0]
        return self.connection.execute(
            "SELECT * FROM canonical_products WHERE canonical_id = ?",
            (product.canonical_id,),
        ).fetchone()

    def _save_observation(self, observation: ProductObservation, canonical_pk: int | None) -> int:
        listing_pk, associated_canonical = self._upsert_listing(observation, canonical_pk)
        effective_canonical = canonical_pk or associated_canonical
        fingerprint = _snapshot_fingerprint(observation)
        context = observation.context
        longitude, latitude = context.coordinates or (None, None)
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO observations(
                listing_id, canonical_product_id, observed_at, current_price, regular_price,
                currency, in_stock, available_quantity, fulfillment_mode, context_resolution,
                postal_code, longitude, latitude, sales_channel, region_id, seller_id, store_id,
                store_name, context_key, snapshot_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_pk,
                effective_canonical,
                _timestamp(observation.observed_at),
                str(observation.current_price),
                _decimal_text(observation.regular_price),
                observation.currency,
                int(observation.in_stock),
                observation.available_quantity,
                context.fulfillment_mode.value,
                context.context_resolution.value,
                context.postal_code,
                _coordinate_text(longitude),
                _coordinate_text(latitude),
                context.sales_channel,
                context.region_id,
                context.seller_id,
                context.store_id,
                context.store_name,
                _context_key(context),
                fingerprint,
            ),
        )
        if cursor.rowcount == 0:
            row = self.connection.execute(
                "SELECT id FROM observations WHERE snapshot_fingerprint = ?", (fingerprint,)
            ).fetchone()
            assert row is not None
            return int(row["id"])
        observation_pk = int(cursor.lastrowid)
        self._insert_promotions(observation_pk, observation.promotions)
        return observation_pk

    def _upsert_listing(
        self, observation: ProductObservation, canonical_pk: int | None
    ) -> tuple[int, int | None]:
        now = _timestamp(datetime.now(UTC))
        key = (
            observation.retailer,
            observation.retailer_product_id,
            observation.retailer_sku_id,
        )
        existing = self.connection.execute(
            """SELECT id, canonical_product_id FROM retailer_listings
               WHERE retailer = ? AND retailer_product_id = ? AND retailer_sku_id = ?""",
            key,
        ).fetchone()
        if (
            existing
            and canonical_pk
            and existing["canonical_product_id"] not in {None, canonical_pk}
        ):
            raise ValueError("retailer listing is already assigned to another canonical product")
        self.connection.execute(
            """
            INSERT INTO retailer_listings(
                retailer, retailer_product_id, retailer_sku_id, catalog_product_id,
                canonical_product_id, title, brand, gtin, product_url, condition,
                created_at, updated_at, volume_ml, pack_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(retailer, retailer_product_id, retailer_sku_id) DO UPDATE SET
                catalog_product_id = COALESCE(
                    excluded.catalog_product_id, retailer_listings.catalog_product_id
                ),
                canonical_product_id = COALESCE(
                    excluded.canonical_product_id, retailer_listings.canonical_product_id
                ),
                title = excluded.title,
                brand = COALESCE(excluded.brand, retailer_listings.brand),
                gtin = COALESCE(excluded.gtin, retailer_listings.gtin),
                product_url = excluded.product_url,
                condition = COALESCE(excluded.condition, retailer_listings.condition),
                volume_ml = COALESCE(excluded.volume_ml, retailer_listings.volume_ml),
                pack_count = COALESCE(excluded.pack_count, retailer_listings.pack_count),
                updated_at = excluded.updated_at
            """,
            (
                *key,
                observation.catalog_product_id,
                canonical_pk,
                observation.title,
                observation.brand,
                observation.gtin,
                observation.product_url,
                observation.condition,
                now,
                now,
                volume_from_observation(observation),
                pack_from_observation(observation),
            ),
        )
        row = self.connection.execute(
            """SELECT id, canonical_product_id FROM retailer_listings
               WHERE retailer = ? AND retailer_product_id = ? AND retailer_sku_id = ?""",
            key,
        ).fetchone()
        assert row is not None
        return int(row["id"]), row["canonical_product_id"]

    def _insert_promotions(self, observation_pk: int, promotions: tuple[Promotion, ...]) -> None:
        for ordinal, promotion in enumerate(promotions):
            cursor = self.connection.execute(
                """INSERT INTO observation_promotions(
                       observation_id, ordinal, kind, name, applied_to_current_price,
                       discount_value, discount_type, minimum_quantity
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation_pk,
                    ordinal,
                    promotion.kind.value,
                    promotion.name,
                    int(promotion.applied_to_current_price),
                    _decimal_text(promotion.discount_value),
                    promotion.discount_type.value if promotion.discount_type else None,
                    _decimal_text(promotion.minimum_quantity),
                ),
            )
            promotion_pk = int(cursor.lastrowid)
            self.connection.executemany(
                "INSERT INTO promotion_conditions(promotion_id, ordinal, condition) "
                "VALUES (?, ?, ?)",
                (
                    (promotion_pk, index, condition)
                    for index, condition in enumerate(promotion.conditions)
                ),
            )

    def _stored_observation(self, row: sqlite3.Row) -> StoredObservation:
        coordinates = None
        if row["longitude"] is not None and row["latitude"] is not None:
            coordinates = (float(row["longitude"]), float(row["latitude"]))
        context = RetailerContext(
            fulfillment_mode=FulfillmentMode(row["fulfillment_mode"]),
            postal_code=row["postal_code"],
            coordinates=coordinates,
            sales_channel=row["sales_channel"],
            region_id=row["region_id"],
            seller_id=row["seller_id"],
            store_id=row["store_id"],
            store_name=row["store_name"],
            context_resolution=ContextResolution(row["context_resolution"]),
        )
        return StoredObservation(
            observation_id=int(row["id"]),
            listing_id=int(row["listing_id"]),
            canonical_id=row["effective_canonical_id"],
            listing=ListingKey(row["retailer"], row["retailer_product_id"], row["retailer_sku_id"]),
            title=row["title"],
            product_url=row["product_url"],
            volume_ml=row["volume_ml"],
            pack_count=row["pack_count"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            current_price=Decimal(row["current_price"]),
            regular_price=Decimal(row["regular_price"]) if row["regular_price"] else None,
            currency=row["currency"],
            in_stock=bool(row["in_stock"]),
            available_quantity=row["available_quantity"],
            context=context,
            promotions=self._load_promotions(int(row["id"])),
        )

    def _load_promotions(self, observation_pk: int) -> tuple[Promotion, ...]:
        rows = self.connection.execute(
            "SELECT * FROM observation_promotions WHERE observation_id = ? ORDER BY ordinal",
            (observation_pk,),
        ).fetchall()
        result = []
        for row in rows:
            conditions = self.connection.execute(
                "SELECT condition FROM promotion_conditions "
                "WHERE promotion_id = ? ORDER BY ordinal",
                (row["id"],),
            ).fetchall()
            result.append(
                Promotion(
                    name=row["name"],
                    kind=PromotionKind(row["kind"]),
                    applied_to_current_price=bool(row["applied_to_current_price"]),
                    discount_value=Decimal(row["discount_value"])
                    if row["discount_value"]
                    else None,
                    discount_type=DiscountType(row["discount_type"])
                    if row["discount_type"]
                    else None,
                    minimum_quantity=Decimal(row["minimum_quantity"])
                    if row["minimum_quantity"]
                    else None,
                    conditions=tuple(condition["condition"] for condition in conditions),
                )
            )
        return tuple(result)

    def _listing_row(self, listing: ListingKey) -> sqlite3.Row:
        row = self.connection.execute(
            """SELECT id, canonical_product_id FROM retailer_listings
               WHERE retailer = ? AND retailer_product_id = ? AND retailer_sku_id = ?""",
            (listing.retailer, listing.retailer_product_id, listing.retailer_sku_id),
        ).fetchone()
        if row is None:
            raise PersistenceError("retailer listing does not exist")
        return row


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observation timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _coordinate_text(value: float | None) -> str | None:
    return repr(value) if value is not None else None


def _context_payload(context: RetailerContext) -> tuple[object, ...]:
    longitude, latitude = context.coordinates or (None, None)
    return (
        context.fulfillment_mode.value,
        context.context_resolution.value,
        context.postal_code,
        _coordinate_text(longitude),
        _coordinate_text(latitude),
        context.sales_channel,
        context.region_id,
        context.seller_id,
        context.store_id,
        context.store_name,
    )


def _context_key(context: RetailerContext) -> str:
    encoded = json.dumps(_context_payload(context), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _snapshot_fingerprint(observation: ProductObservation) -> str:
    promotions = [
        (
            item.kind.value,
            item.name,
            item.applied_to_current_price,
            _decimal_text(item.discount_value),
            item.discount_type.value if item.discount_type else None,
            _decimal_text(item.minimum_quantity),
            item.conditions,
        )
        for item in observation.promotions
    ]
    payload = (
        observation.retailer,
        observation.retailer_product_id,
        observation.retailer_sku_id,
        _timestamp(observation.observed_at),
        str(observation.current_price),
        _decimal_text(observation.regular_price),
        observation.currency,
        observation.in_stock,
        observation.available_quantity,
        _context_payload(observation.context),
        promotions,
    )
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
