-- ---------------------------------------------------------------------------
-- Views consumed by the Power BI model.
--
-- Power BI performs best when the heavy shaping happens upstream and the model
-- receives a clean star schema. These views do the joins and derivations once,
-- so DAX measures stay simple aggregations rather than row-by-row gymnastics.
--
-- Run once after `pf init`:
--     sqlite3 data/prices.db < sql/04_powerbi_marts.sql
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS vw_daily_prices;
DROP VIEW IF EXISTS vw_discount_integrity;
DROP VIEW IF EXISTS vw_scarcity_claims;
DROP VIEW IF EXISTS vw_collection_health;
DROP VIEW IF EXISTS vw_category_summary;

-- ---------------------------------------------------------------------------
-- The main fact view. One row per listing per day, pre-joined to its
-- dimensions so the report's fact table needs no further shaping.
CREATE VIEW vw_daily_prices AS
SELECT
    o.observation_id,
    o.listing_id,
    o.product_id,
    o.seller_id,
    o.date_key,
    l.site_key,
    p.category,
    p.brand,
    o.selling_price,
    o.mrp,
    o.computed_discount_pct,
    o.in_stock,
    o.source,

    -- Trailing minimum: the basis for an honest "is this actually cheap?"
    MIN(o.selling_price) OVER (
        PARTITION BY o.listing_id ORDER BY o.date_key
        ROWS BETWEEN 30 PRECEDING AND CURRENT ROW
    ) AS price_min_30d,

    MAX(o.selling_price) OVER (
        PARTITION BY o.listing_id ORDER BY o.date_key
        ROWS BETWEEN 30 PRECEDING AND CURRENT ROW
    ) AS price_max_30d
FROM fact_price_observation o
JOIN dim_listing l ON l.listing_id = o.listing_id
JOIN dim_product p ON p.product_id = o.product_id
WHERE o.source = 'live';

-- ---------------------------------------------------------------------------
-- The headline table behind the dashboard's main page: every confirmed or
-- suspected manufactured discount, with the gap between claim and reality.
CREATE VIEW vw_discount_integrity AS
SELECT
    e.event_id,
    e.run_id,
    e.listing_id,
    e.product_id,
    l.site_key,
    l.url,
    p.canonical_title,
    p.brand,
    p.category,
    e.baseline_price,
    e.peak_price,
    e.sale_price,
    e.rise_pct,
    e.claimed_discount_pct,
    e.real_discount_pct,
    e.discount_overstatement_pp,
    e.rise_start_date,
    e.sale_start_date,
    e.days_between,
    e.confidence,
    e.manually_reviewed,
    e.reviewer_verdict,

    -- Rupee value of the exaggeration, for revenue-framed cards.
    ROUND(e.peak_price - e.baseline_price, 2) AS rupees_of_inflation,

    CASE
        WHEN e.confidence = 'high'   THEN 3
        WHEN e.confidence = 'medium' THEN 2
        ELSE 1
    END AS confidence_rank
FROM inflation_event e
JOIN dim_listing l ON l.listing_id = e.listing_id
JOIN dim_product p ON p.product_id = e.product_id;

-- ---------------------------------------------------------------------------
-- Scarcity messaging, aggregated per listing so the report can show what
-- fraction of "only N left" counters never actually move.
CREATE VIEW vw_scarcity_claims AS
SELECT
    dp.listing_id,
    l.site_key,
    p.canonical_title,
    p.category,
    dp.pattern_type,
    COUNT(*)                          AS days_observed,
    COUNT(DISTINCT dp.numeric_value)  AS distinct_values,
    MIN(dp.numeric_value)             AS min_value,
    MAX(dp.numeric_value)             AS max_value,
    CASE
        WHEN COUNT(*) < 7 THEN 'insufficient'
        WHEN COUNT(DISTINCT dp.numeric_value) <= 1 THEN 'static'
        ELSE 'moves'
    END AS verdict
FROM fact_dark_pattern dp
JOIN dim_listing l ON l.listing_id = dp.listing_id
JOIN dim_product p ON p.product_id = l.product_id
GROUP BY dp.listing_id, dp.pattern_type;

-- ---------------------------------------------------------------------------
-- Collection health. A longitudinal study lives or dies on whether the
-- scraper actually ran, so this is surfaced on its own dashboard page rather
-- than buried — silent collection failure is the project's biggest risk.
CREATE VIEW vw_collection_health AS
SELECT
    d.date,
    d.date_key,
    d.is_festival,
    l.site_key,
    COUNT(*)                                   AS observations,
    COUNT(DISTINCT o.listing_id)               AS listings_seen,
    SUM(CASE WHEN o.selling_price IS NULL THEN 1 ELSE 0 END) AS missing_price,
    SUM(CASE WHEN o.mrp IS NULL THEN 1 ELSE 0 END)           AS missing_mrp,
    ROUND(AVG(o.selling_price), 2)             AS avg_price
FROM fact_price_observation o
JOIN dim_date    d ON d.date_key   = o.date_key
JOIN dim_listing l ON l.listing_id = o.listing_id
WHERE o.source = 'live'
GROUP BY d.date_key, l.site_key;

-- ---------------------------------------------------------------------------
-- Category-level rollup for the dashboard's summary cards.
CREATE VIEW vw_category_summary AS
SELECT
    p.category,
    l.site_key,
    COUNT(DISTINCT o.listing_id)              AS listings_tracked,
    COUNT(DISTINCT o.product_id)              AS products_tracked,
    ROUND(AVG(o.computed_discount_pct), 2)    AS avg_advertised_discount_pct,
    COUNT(DISTINCT e.event_id)                AS flagged_events,
    ROUND(
        CAST(COUNT(DISTINCT e.listing_id) AS REAL)
        / NULLIF(COUNT(DISTINCT o.listing_id), 0) * 100, 2
    )                                          AS pct_listings_flagged
FROM fact_price_observation o
JOIN dim_listing  l ON l.listing_id = o.listing_id
JOIN dim_product  p ON p.product_id = o.product_id
LEFT JOIN inflation_event e ON e.listing_id = o.listing_id
WHERE o.source = 'live'
GROUP BY p.category, l.site_key;
