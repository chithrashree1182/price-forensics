-- ---------------------------------------------------------------------------
-- Per-listing price timeline with derived movement columns.
--
-- This is the workhorse query: it turns a bare table of daily prices into a
-- series that carries its own history, so downstream questions ("was today's
-- price a drop?", "how does it compare to the 30-day low?") become filters
-- rather than self-joins.
--
-- Uses window functions throughout — LAG for day-over-day movement, and framed
-- aggregates for trailing windows.
-- ---------------------------------------------------------------------------

WITH observations AS (
    SELECT
        o.listing_id,
        o.product_id,
        l.site_key,
        COALESCE(p.canonical_title, l.raw_title) AS title,
        p.category,
        d.date          AS obs_date,
        d.date_key,
        d.is_festival,
        d.festival_window,
        o.selling_price,
        o.mrp,
        o.computed_discount_pct,
        o.source
    FROM fact_price_observation o
    JOIN dim_listing l ON l.listing_id = o.listing_id
    JOIN dim_product p ON p.product_id = o.product_id
    JOIN dim_date    d ON d.date_key   = o.date_key
    WHERE o.selling_price IS NOT NULL
      AND o.source = 'live'
),

with_movement AS (
    SELECT
        *,
        LAG(selling_price) OVER w            AS prev_price,
        LAG(obs_date)      OVER w            AS prev_date,
        FIRST_VALUE(selling_price) OVER w    AS first_price,

        -- Trailing 30-observation window. Framed on rows rather than days
        -- because collection gaps would otherwise silently shrink the window.
        MIN(selling_price) OVER (
            PARTITION BY listing_id ORDER BY date_key
            ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING
        ) AS trailing_min_30,
        MAX(selling_price) OVER (
            PARTITION BY listing_id ORDER BY date_key
            ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING
        ) AS trailing_max_30,
        AVG(selling_price) OVER (
            PARTITION BY listing_id ORDER BY date_key
            ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING
        ) AS trailing_avg_30,

        COUNT(*) OVER (PARTITION BY listing_id) AS n_observations
    FROM observations
    WINDOW w AS (PARTITION BY listing_id ORDER BY date_key)
)

SELECT
    listing_id,
    product_id,
    site_key,
    title,
    category,
    obs_date,
    is_festival,
    festival_window,
    selling_price,
    mrp,
    computed_discount_pct,
    prev_price,

    -- Day-over-day movement
    ROUND(selling_price - prev_price, 2) AS price_delta,
    CASE
        WHEN prev_price IS NULL OR prev_price = 0 THEN NULL
        ELSE ROUND((selling_price - prev_price) / prev_price * 100, 2)
    END AS price_delta_pct,

    -- Gap detection: how many days since the previous observation. Anything
    -- above 1 means the scraper missed a day, which matters when interpreting
    -- a sudden apparent jump.
    CAST(julianday(obs_date) - julianday(prev_date) AS INTEGER) AS days_since_prev,

    ROUND(trailing_min_30, 2) AS trailing_min_30,
    ROUND(trailing_max_30, 2) AS trailing_max_30,
    ROUND(trailing_avg_30, 2) AS trailing_avg_30,

    -- The honest discount: today's price against the cheapest this listing has
    -- actually been in the trailing window. Compare with the advertised figure.
    CASE
        WHEN trailing_min_30 IS NULL OR trailing_min_30 = 0 THEN NULL
        ELSE ROUND((selling_price - trailing_min_30) / trailing_min_30 * 100, 2)
    END AS premium_over_trailing_min_pct,

    n_observations
FROM with_movement
ORDER BY listing_id, obs_date;
