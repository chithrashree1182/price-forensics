-- ---------------------------------------------------------------------------
-- Cross-seller MRP dispersion.
--
-- MRP is declared by the manufacturer and printed on the pack, so it is a
-- property of the product, not of the shop. Where retailers disagree, at least
-- one of them is quoting a number that is not the MRP — and that number is the
-- denominator of the advertised discount.
--
-- Needs a single day of data. This is the week-one finding.
-- ---------------------------------------------------------------------------

WITH latest_day AS (
    SELECT MAX(date_key) AS date_key
    FROM fact_price_observation
    WHERE source = 'live'
),

-- One quote per site. A marketplace listing the same item through four
-- resellers should not outvote a retailer that lists it once.
site_quotes AS (
    SELECT
        o.product_id,
        l.site_key,
        MIN(o.listing_id)    AS listing_id,
        -- Where a site lists a product more than once the quotes are
        -- near-identical; the minimum is a stable representative.
        MIN(o.mrp)           AS mrp,
        MIN(o.selling_price) AS selling_price
    FROM fact_price_observation o
    JOIN dim_listing l ON l.listing_id = o.listing_id
    JOIN latest_day ld ON ld.date_key = o.date_key
    WHERE o.mrp IS NOT NULL AND o.mrp > 0 AND o.source = 'live'
    GROUP BY o.product_id, l.site_key
),

ranked AS (
    SELECT
        q.*,
        COUNT(*)      OVER (PARTITION BY product_id) AS n_sellers,
        MIN(mrp)      OVER (PARTITION BY product_id) AS min_mrp,
        MAX(mrp)      OVER (PARTITION BY product_id) AS max_mrp,
        AVG(mrp)      OVER (PARTITION BY product_id) AS mean_mrp,
        ROW_NUMBER()  OVER (PARTITION BY product_id ORDER BY mrp DESC) AS rn_highest
    FROM site_quotes q
),

-- SQLite lacks PERCENTILE_CONT, so the median is taken as the middle row(s)
-- of the ordered quotes per product.
medians AS (
    SELECT
        product_id,
        AVG(mrp) AS median_mrp
    FROM (
        SELECT
            product_id,
            mrp,
            ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY mrp) AS rn,
            COUNT(*)     OVER (PARTITION BY product_id)              AS cnt
        FROM site_quotes
    )
    WHERE rn IN ((cnt + 1) / 2, (cnt + 2) / 2)
    GROUP BY product_id
)

SELECT
    p.canonical_title,
    p.brand,
    p.category,
    r.n_sellers,
    ROUND(r.min_mrp, 2)     AS min_mrp,
    ROUND(m.median_mrp, 2)  AS median_mrp,
    ROUND(r.max_mrp, 2)     AS max_mrp,
    ROUND((r.max_mrp - r.min_mrp) / NULLIF(m.median_mrp, 0) * 100, 2) AS spread_pct,

    r.site_key              AS highest_quoting_site,
    ROUND((r.mrp - m.median_mrp) / NULLIF(m.median_mrp, 0) * 100, 2)
                            AS overstatement_vs_median_pct,

    -- What the shopper is told they are saving, versus what they would save
    -- against the price the rest of the market calls the MRP.
    ROUND((r.mrp - r.selling_price) / NULLIF(r.mrp, 0) * 100, 2)
                            AS claimed_discount_pct,
    ROUND((m.median_mrp - r.selling_price) / NULLIF(m.median_mrp, 0) * 100, 2)
                            AS discount_vs_median_mrp_pct

FROM ranked r
JOIN medians    m ON m.product_id = r.product_id
JOIN dim_product p ON p.product_id = r.product_id
WHERE r.rn_highest = 1                    -- the seller claiming the biggest MRP
  AND r.n_sellers >= 3
  AND (r.max_mrp - r.min_mrp) / NULLIF(m.median_mrp, 0) * 100 > 2.0
ORDER BY spread_pct DESC;
