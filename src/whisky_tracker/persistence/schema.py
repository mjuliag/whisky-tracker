"""Small explicit SQLite migration set."""

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE schema_version (
        version INTEGER NOT NULL PRIMARY KEY,
        applied_at TEXT NOT NULL
    );

    CREATE TABLE canonical_products (
        id INTEGER PRIMARY KEY,
        canonical_id TEXT NOT NULL UNIQUE,
        brand TEXT,
        expression TEXT,
        age_statement INTEGER CHECK (age_statement IS NULL OR age_statement > 0),
        volume_ml INTEGER CHECK (volume_ml IS NULL OR volume_ml > 0),
        pack_count INTEGER CHECK (pack_count IS NULL OR pack_count > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE canonical_gtins (
        canonical_product_id INTEGER NOT NULL REFERENCES canonical_products(id) ON DELETE CASCADE,
        gtin TEXT NOT NULL,
        PRIMARY KEY (canonical_product_id, gtin),
        UNIQUE (gtin)
    );

    CREATE TABLE retailer_listings (
        id INTEGER PRIMARY KEY,
        retailer TEXT NOT NULL,
        retailer_product_id TEXT NOT NULL,
        retailer_sku_id TEXT NOT NULL,
        catalog_product_id TEXT,
        canonical_product_id INTEGER REFERENCES canonical_products(id),
        title TEXT NOT NULL,
        brand TEXT,
        gtin TEXT,
        product_url TEXT NOT NULL,
        condition TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (retailer, retailer_product_id, retailer_sku_id)
    );

    CREATE TABLE observations (
        id INTEGER PRIMARY KEY,
        listing_id INTEGER NOT NULL REFERENCES retailer_listings(id) ON DELETE CASCADE,
        canonical_product_id INTEGER REFERENCES canonical_products(id),
        observed_at TEXT NOT NULL,
        current_price TEXT NOT NULL,
        regular_price TEXT,
        currency TEXT NOT NULL,
        in_stock INTEGER NOT NULL CHECK (in_stock IN (0, 1)),
        available_quantity INTEGER,
        fulfillment_mode TEXT NOT NULL,
        context_resolution TEXT NOT NULL,
        postal_code TEXT,
        longitude TEXT,
        latitude TEXT,
        sales_channel TEXT,
        region_id TEXT,
        seller_id TEXT,
        store_id TEXT,
        store_name TEXT,
        context_key TEXT NOT NULL,
        snapshot_fingerprint TEXT NOT NULL UNIQUE
    );

    CREATE INDEX observations_listing_context_time
        ON observations(listing_id, context_key, observed_at);
    CREATE INDEX observations_canonical_time
        ON observations(canonical_product_id, observed_at);

    CREATE TABLE observation_promotions (
        id INTEGER PRIMARY KEY,
        observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        applied_to_current_price INTEGER NOT NULL CHECK (applied_to_current_price IN (0, 1)),
        discount_value TEXT,
        discount_type TEXT,
        minimum_quantity TEXT,
        UNIQUE (observation_id, ordinal)
    );

    CREATE TABLE promotion_conditions (
        promotion_id INTEGER NOT NULL REFERENCES observation_promotions(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        condition TEXT NOT NULL,
        PRIMARY KEY (promotion_id, ordinal)
    );
    """,
    """
    ALTER TABLE retailer_listings ADD COLUMN volume_ml INTEGER
        CHECK (volume_ml IS NULL OR volume_ml > 0);
    ALTER TABLE retailer_listings ADD COLUMN pack_count INTEGER
        CHECK (pack_count IS NULL OR pack_count > 0);

    CREATE TABLE alert_events (
        id INTEGER PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,
        canonical_product_id INTEGER REFERENCES canonical_products(id),
        listing_id INTEGER NOT NULL REFERENCES retailer_listings(id) ON DELETE CASCADE,
        observation_id INTEGER REFERENCES observations(id) ON DELETE SET NULL,
        context_key TEXT NOT NULL,
        alert_types TEXT NOT NULL,
        price TEXT NOT NULL,
        currency TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'sent')),
        generated_at TEXT NOT NULL,
        sent_at TEXT
    );

    CREATE INDEX alert_events_status ON alert_events(status, fingerprint);
    """,
)

LATEST_SCHEMA_VERSION = len(MIGRATIONS)
