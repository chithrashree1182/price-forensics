-- ---------------------------------------------------------------------------
-- Pre-sale inflation candidates, computed entirely in SQL.
--
-- The Python detector in analysis/inflation.py is authoritative — it handles
-- confidence tiering and irregular observation gaps properly. This query is an
-- independent implementation of the same idea, kept for two reasons:
--
--   1. Cross-validation. Two implementations disagreeing is the cheapest bug
--      detector available, and they were written from the same spec but
--      different mechanics.
--   2. Power BI reads SQL, not Python. This lets the dashboard recompute
--      candidates on refresh without a Python round-trip.
--
-- Method: for every observation, compare today's price against the trailing
-- 30-row window, then look for the signature — a run-up that is fully reversed
-- by a subsequent drop.
-- ---------------------------------------------------------------------------

WITH base AS (
    SELECT
        o.listing_id,
        o.product_id,
        l.site_key,
        COALESCE(p.canonical_title, l.raw_title) AS title,
        p.category,
        d.date     AS obs_date,
        d.date_key,
        d.is_festival,
        o.selling_price
    FROM fact_price_observation o
    JOIN dim_listing l ON l.listing_id = o.listing_id
    JOIN dim_product p ON p.product_id = o.product_id
    JOIN dim_date    d ON d.date_key   = o.date_key
    WHERE o.selling_price IS NOT NULL
      AND o.source = 'live'
),

windowed AS (
    SELECT
        *,
        LAG(selling_price) OVER w AS prev_price,

        -- The elevated price the "discount" would be measured from.
        MAX(selling_price) OVER (
            PARTITION BY listing_id ORDER BY date_key
            ROWS BETWEEN 21 PRECEDING AND 1 PRECEDING
        ) AS peak_21,

        -- The settled price before the run-up. Taken from further back so the
        -- run-up itself does not contaminate the baseline.
        MIN(selling_price) OVER (
            PARTITION BY listing_id ORDER BY date_key
            ROWS BETWEEN 45 PRECEDING AND 22 PRECEDING
        ) AS baseline_min,
        AVG(selling_price) OVER (
            PARTITION BY listing_id ORDER BY date_key
            ROWS BETWEEN 45 PRECEDING AND 22 PRECEDING
        ) AS baseline_avg,

        COUNT(*) OVER (
            PARTITION BY listing_id ORDER BY date_key
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS n_prior_observations
    FROM base
    WINDOW w AS (PARTITION BY listing_id ORDER BY date_key)
),

candidates AS (
    SELECT
        listing_id,
        product_id,
        site_key,
        title,
        category,
        obs_date        AS sale_date,
        is_festival,
        selling_price   AS sale_price,
        prev_price,
        ROUND(peak_21, 2)       AS peak_price,
        ROUND(baseline_avg, 2)  AS baseline_price,

        ROUND((peak_21 - baseline_avg) / NULLIF(baseline_avg, 0) * 100, 2) AS rise_pct,

        -- What the retailer can advertise, measured from the inflated peak.
        ROUND((peak_21 - selling_price) / NULLIF(peak_21, 0) * 100, 2)     AS claimed_discount_pct,

        -- What the shopper actually saves against the pre-run-up price.
        ROUND((baseline_avg - selling_price) / NULLIF(baseline_avg, 0) * 100, 2)
                                                                            AS real_discount_pct,
        n_prior_observations
    FROM windowed
    WHERE prev_price IS NOT NULL
      AND baseline_avg IS NOT NULL
      AND peak_21 IS NOT NULL
      AND n_prior_observations >= 25
      -- A drop of at least 8% today: the "sale" moment.
      AND (prev_price - selling_price) / NULLIF(prev_price, 0) * 100 >= 8
      -- ...preceded by a rise of at least 10% over the settled baseline.
      AND (peak_21 - baseline_avg) / NULLIF(baseline_avg, 0) * 100 >= 10
)

SELECT
    listing_id,
    site_key,
    title,
    category,
    sale_date,
    is_festival,
    baseline_price,
    peak_price,
    sale_price,
    rise_pct,
    claimed_discount_pct,
    real_discount_pct,
    ROUND(claimed_discount_pct - real_discount_pct, 2) AS overstatement_pp,
    n_prior_observations,

    -- One row per listing: a multi-day sale would otherwise report the same
    -- campaign once per day and inflate every count downstream.
    ROW_NUMBER() OVER (
        PARTITION BY listing_id
        ORDER BY (claimed_discount_pct - real_discount_pct) DESC
    ) AS rn
FROM candidates
WHERE claimed_discount_pct > real_discount_pct
ORDER BY overstatement_pp DESC;
