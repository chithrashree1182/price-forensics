-- ---------------------------------------------------------------------------
-- Price Forensics — warehouse schema (SQLite)
--
-- Modelled as a star schema rather than one wide table. Two reasons:
--   1. Power BI's engine (VertiPaq) compresses and joins star schemas far more
--      efficiently than flat tables, and DAX filter propagation assumes them.
--   2. A price observation is genuinely a fact with several conformed
--      dimensions (what / where / when), so the model matches the domain.
--
--   dim_product   ─┐
--   dim_seller    ─┼─→ dim_listing ─┐
--                                   ├─→ fact_price_observation
--   dim_date      ─────────────────┘         │
--                                             └─→ fact_dark_pattern
-- ---------------------------------------------------------------------------

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- --------------------------------------------------------------------------
-- DIMENSIONS
-- --------------------------------------------------------------------------

-- A product in the abstract: "Samsung Galaxy S24 128GB", independent of who
-- sells it. Populated by src/priceforensics/normalize.py, which is the hard
-- part of this project — the same phone is titled six different ways.
CREATE TABLE IF NOT EXISTS dim_product (
    product_id      INTEGER PRIMARY KEY,
    match_key       TEXT NOT NULL UNIQUE,  -- normalised brand|model|variant
    brand           TEXT,
    model           TEXT,
    variant         TEXT,                  -- "128GB", "8GB/256GB", colour dropped
    category        TEXT NOT NULL,
    canonical_title TEXT,
    first_seen_date TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_product_category ON dim_product(category);
CREATE INDEX IF NOT EXISTS ix_product_brand    ON dim_product(brand);

-- The retailer (and, where exposed, the individual marketplace seller).
CREATE TABLE IF NOT EXISTS dim_seller (
    seller_id    INTEGER PRIMARY KEY,
    site_key     TEXT NOT NULL,            -- 'flipkart', 'croma'
    site_name    TEXT NOT NULL,
    seller_name  TEXT,                     -- marketplace sub-seller, may be NULL
    is_first_party INTEGER NOT NULL DEFAULT 1,
    UNIQUE (site_key, seller_name)
);

-- A specific product-on-a-specific-site: this is what actually has a URL and
-- a price. Separating it from dim_product is what makes cross-seller MRP
-- comparison possible.
CREATE TABLE IF NOT EXISTS dim_listing (
    listing_id      INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES dim_product(product_id),
    seller_id       INTEGER NOT NULL REFERENCES dim_seller(seller_id),
    site_key        TEXT NOT NULL,
    site_product_id TEXT,                  -- retailer's own SKU/ID if exposed
    url             TEXT NOT NULL,
    raw_title       TEXT NOT NULL,
    first_seen_date TEXT NOT NULL,
    last_seen_date  TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (site_key, url)
);
CREATE INDEX IF NOT EXISTS ix_listing_product ON dim_listing(product_id);
CREATE INDEX IF NOT EXISTS ix_listing_site    ON dim_listing(site_key);

-- Explicit date dimension. Power BI needs a marked date table for any time
-- intelligence DAX (YoY, rolling averages, period-over-period) to work.
CREATE TABLE IF NOT EXISTS dim_date (
    date_key          INTEGER PRIMARY KEY,   -- yyyymmdd
    date              TEXT NOT NULL UNIQUE,
    year              INTEGER NOT NULL,
    quarter           INTEGER NOT NULL,
    month             INTEGER NOT NULL,
    month_name        TEXT NOT NULL,
    day               INTEGER NOT NULL,
    day_of_week       INTEGER NOT NULL,
    day_name          TEXT NOT NULL,
    is_weekend        INTEGER NOT NULL,
    -- Study-specific flags, used heavily by the analysis:
    festival_window   TEXT,                  -- name of sale window, else NULL
    is_festival       INTEGER NOT NULL DEFAULT 0,
    days_to_festival  INTEGER                -- negative = before, 0 = during
);

-- --------------------------------------------------------------------------
-- FACTS
-- --------------------------------------------------------------------------

-- One row per listing per day. The grain is deliberately daily: sub-daily
-- sampling would multiply storage without changing any conclusion, since the
-- behaviour under study operates on a scale of days.
CREATE TABLE IF NOT EXISTS fact_price_observation (
    observation_id  INTEGER PRIMARY KEY,
    listing_id      INTEGER NOT NULL REFERENCES dim_listing(listing_id),
    date_key        INTEGER NOT NULL REFERENCES dim_date(date_key),
    product_id      INTEGER NOT NULL REFERENCES dim_product(product_id),
    seller_id       INTEGER NOT NULL REFERENCES dim_seller(seller_id),

    selling_price   REAL,                  -- what you actually pay
    mrp             REAL,                  -- the struck-through "original" price
    discount_pct    REAL,                  -- as claimed by the site, if shown
    computed_discount_pct REAL,            -- derived: (mrp-selling)/mrp*100

    in_stock        INTEGER,
    rating          REAL,
    rating_count    INTEGER,

    scraped_at      TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'live',  -- 'live' | 'wayback' | 'synthetic'
    raw_snapshot    TEXT,                  -- relative path to gzipped HTML
    UNIQUE (listing_id, date_key, source)
);
CREATE INDEX IF NOT EXISTS ix_obs_date     ON fact_price_observation(date_key);
CREATE INDEX IF NOT EXISTS ix_obs_listing  ON fact_price_observation(listing_id, date_key);
CREATE INDEX IF NOT EXISTS ix_obs_product  ON fact_price_observation(product_id, date_key);

-- Urgency / scarcity messaging observed on the page.
CREATE TABLE IF NOT EXISTS fact_dark_pattern (
    pattern_id    INTEGER PRIMARY KEY,
    listing_id    INTEGER NOT NULL REFERENCES dim_listing(listing_id),
    date_key      INTEGER NOT NULL REFERENCES dim_date(date_key),
    pattern_type  TEXT NOT NULL,           -- 'stock_claim' | 'viewer_claim' | 'countdown'
    raw_text      TEXT NOT NULL,
    numeric_value REAL,                    -- e.g. "only 3 left" -> 3
    scraped_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dp_listing ON fact_dark_pattern(listing_id, date_key);

-- --------------------------------------------------------------------------
-- ANALYSIS OUTPUTS
-- Written by the analysis modules; read by Power BI. Kept as tables rather
-- than views because the detection parameters are versioned with each run.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS analysis_run (
    run_id       INTEGER PRIMARY KEY,
    run_type     TEXT NOT NULL,            -- 'inflation' | 'mrp_audit' | ...
    run_at       TEXT NOT NULL DEFAULT (datetime('now')),
    params_json  TEXT NOT NULL,            -- exact thresholds used
    n_input      INTEGER,
    n_flagged    INTEGER,
    notes        TEXT
);

-- A suspected pre-sale price inflation event.
CREATE TABLE IF NOT EXISTS inflation_event (
    event_id          INTEGER PRIMARY KEY,
    run_id            INTEGER NOT NULL REFERENCES analysis_run(run_id),
    listing_id        INTEGER NOT NULL REFERENCES dim_listing(listing_id),
    product_id        INTEGER NOT NULL REFERENCES dim_product(product_id),

    baseline_price    REAL NOT NULL,       -- stable price before the rise
    peak_price        REAL NOT NULL,       -- inflated price
    sale_price        REAL NOT NULL,       -- price during the "discount"
    rise_pct          REAL NOT NULL,
    claimed_discount_pct REAL,             -- what the site advertised
    real_discount_pct REAL,                -- vs. the true baseline
    discount_overstatement_pp REAL,        -- percentage points of exaggeration

    rise_start_date   TEXT NOT NULL,
    sale_start_date   TEXT NOT NULL,
    days_between      INTEGER NOT NULL,
    confidence        TEXT NOT NULL,       -- 'high' | 'medium' | 'low'

    -- Manual validation (see analysis/validation.py). NULL until reviewed.
    manually_reviewed INTEGER NOT NULL DEFAULT 0,
    reviewer_verdict  TEXT,                -- 'confirmed' | 'rejected' | 'unclear'
    reviewer_note     TEXT
);
CREATE INDEX IF NOT EXISTS ix_infl_run ON inflation_event(run_id);

-- A product whose claimed MRP disagrees across sellers.
CREATE TABLE IF NOT EXISTS mrp_contradiction (
    contradiction_id INTEGER PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES analysis_run(run_id),
    product_id       INTEGER NOT NULL REFERENCES dim_product(product_id),
    date_key         INTEGER NOT NULL REFERENCES dim_date(date_key),

    n_sellers        INTEGER NOT NULL,
    min_mrp          REAL NOT NULL,
    max_mrp          REAL NOT NULL,
    median_mrp       REAL NOT NULL,
    spread_pct       REAL NOT NULL,        -- (max-min)/median * 100
    -- The listing quoting the highest MRP — i.e. claiming the biggest discount
    -- off a price nobody else recognises.
    worst_listing_id INTEGER REFERENCES dim_listing(listing_id),
    worst_site_key   TEXT,
    inflation_vs_median_pct REAL
);
CREATE INDEX IF NOT EXISTS ix_mrp_run ON mrp_contradiction(run_id);
