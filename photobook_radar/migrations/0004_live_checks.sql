CREATE TABLE live_checks(
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    observation_id INTEGER NOT NULL REFERENCES observations(id),
    provider TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    availability TEXT NOT NULL,
    price_minor INTEGER,
    currency TEXT,
    shipping_minor INTEGER,
    shipping_currency TEXT,
    seller_url TEXT,
    result_json TEXT NOT NULL,
    UNIQUE(listing_id,observation_id,provider)
);
CREATE INDEX live_checks_listing_idx ON live_checks(listing_id,checked_at DESC);
