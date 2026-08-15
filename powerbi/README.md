# Power BI model

The dashboard is the demo surface: it is what someone clicks before they read
any code. This document is the build spec.

## Loading the data

```bash
pf export --format parquet          # writes data/exports/powerbi/
```

Power BI reads the folder directly — **Get Data → Folder → Combine**. Parquet
avoids the SQLite ODBC driver dance on Windows, which makes the report portable
to any machine.

Each exported file becomes one table in the model. Do not merge them in Power
Query: the star schema *is* the design.

---

## The model

```
              ┌──────────────┐
              │  dim_date    │  ← mark as date table
              └──────┬───────┘
                     │ 1
                     │
                     ▼ *
┌────────────┐  ┌──────────────────────────┐  ┌──────────────┐
│dim_product │─▶│  fact_price_observation  │◀─│  dim_seller  │
└─────┬──────┘ 1└──────────────────────────┘* └──────────────┘
      │ 1                    ▲ *
      │                      │
      ▼ *                    │ 1
┌────────────┐               │
│dim_listing │───────────────┘
└─────┬──────┘
      │ 1
      ▼ *
┌──────────────────┐   ┌────────────────────┐
│ inflation_event  │   │ fact_dark_pattern  │
└──────────────────┘   └────────────────────┘
```

**Relationships** — all single-direction, one-to-many, from dimension to fact:

| From | To | Cardinality | Direction |
|---|---|---|---|
| `dim_date[date_key]` | `fact_price_observation[date_key]` | 1:* | Single |
| `dim_product[product_id]` | `fact_price_observation[product_id]` | 1:* | Single |
| `dim_seller[seller_id]` | `fact_price_observation[seller_id]` | 1:* | Single |
| `dim_listing[listing_id]` | `fact_price_observation[listing_id]` | 1:* | Single |
| `dim_listing[listing_id]` | `inflation_event[listing_id]` | 1:* | Single |
| `dim_listing[listing_id]` | `fact_dark_pattern[listing_id]` | 1:* | Single |

**Keep filter direction single.** Bidirectional relationships here would create
ambiguous filter paths between `dim_product` and `dim_listing` and produce
wrong totals silently.

**Mark `dim_date` as a date table** (Table tools → Mark as date table →
`date`). Without this, every time-intelligence measure below returns blank. The
export writes a gap-free contiguous range specifically so this works.

---

## Measures

### Core

```dax
Listings Tracked = DISTINCTCOUNT(fact_price_observation[listing_id])

Observations = COUNTROWS(fact_price_observation)

Avg Selling Price = AVERAGE(fact_price_observation[selling_price])

Advertised Discount % =
AVERAGE(fact_price_observation[computed_discount_pct])
```

### The headline measures

```dax
Flagged Listings =
CALCULATE(
    DISTINCTCOUNT(inflation_event[listing_id]),
    inflation_event[confidence] IN { "high", "medium" }
)

Flagged Rate % =
DIVIDE([Flagged Listings], [Listings Tracked], 0) * 100

Median Overstatement (pp) =
MEDIANX(
    FILTER(inflation_event, inflation_event[confidence] IN { "high", "medium" }),
    inflation_event[discount_overstatement_pp]
)

-- The one-line story of the whole project:
Claimed vs Real Discount =
VAR Claimed = AVERAGE(inflation_event[claimed_discount_pct])
VAR Real    = AVERAGE(inflation_event[real_discount_pct])
RETURN Claimed - Real
```

### Time intelligence

Requires `dim_date` marked as a date table.

```dax
Price 30D Ago =
CALCULATE([Avg Selling Price], DATEADD(dim_date[date], -30, DAY))

Price Change 30D % =
DIVIDE([Avg Selling Price] - [Price 30D Ago], [Price 30D Ago], BLANK()) * 100

Rolling 7D Avg Price =
AVERAGEX(
    DATESINPERIOD(dim_date[date], MAX(dim_date[date]), -7, DAY),
    [Avg Selling Price]
)

-- Trailing minimum: the honest reference price.
Trailing 30D Min =
CALCULATE(
    MIN(fact_price_observation[selling_price]),
    DATESINPERIOD(dim_date[date], MAX(dim_date[date]), -30, DAY)
)

Premium Over 30D Low % =
DIVIDE([Avg Selling Price] - [Trailing 30D Min], [Trailing 30D Min], BLANK()) * 100
```

### Festival-window comparison

```dax
Festival Discount % =
CALCULATE([Advertised Discount %], dim_date[is_festival] = 1)

Non-Festival Discount % =
CALCULATE([Advertised Discount %], dim_date[is_festival] = 0)

Festival Uplift (pp) = [Festival Discount %] - [Non-Festival Discount %]
```

### Scarcity

```dax
Static Stock Claims =
CALCULATE(
    DISTINCTCOUNT(fact_dark_pattern[listing_id]),
    FILTER(
        SUMMARIZE(
            fact_dark_pattern,
            fact_dark_pattern[listing_id],
            "Distinct", DISTINCTCOUNT(fact_dark_pattern[numeric_value]),
            "Days", COUNTROWS(fact_dark_pattern)
        ),
        [Distinct] <= 1 && [Days] >= 7
    )
)
```

---

## What-if parameter: the credibility slider

Modelling → New parameter → Numeric range, `0` to `40`, step `1`,
name **Min Overstatement (pp)**.

```dax
Listings Above Threshold =
VAR Threshold = SELECTEDVALUE('Min Overstatement (pp)'[Value], 0)
RETURN
CALCULATE(
    DISTINCTCOUNT(inflation_event[listing_id]),
    inflation_event[discount_overstatement_pp] >= Threshold
)
```

This turns the dashboard from a claim into an argument. A viewer who thinks the
threshold is too lenient can drag it and watch the finding survive — or not.
That is a much stronger position than asserting one number.

---

## Row-Level Security

Rare in portfolio projects and immediately enterprise-relevant. Manage roles →
Create:

**Role `Croma Analyst`** on `dim_seller`:
```dax
[site_key] = "croma"
```

**Role `Category Manager`** on `dim_product` — driven by the signed-in user:
```dax
[category] = LOOKUPVALUE(
    user_category_map[category],
    user_category_map[email], USERPRINCIPALNAME()
)
```

Test with View As → Role. Because relationships propagate one-way from
dimensions to facts, filtering `dim_seller` correctly restricts the fact table
without any extra configuration — which is the payoff for getting the schema
right.

---

## Pages

**1 · Overview** — headline cards (listings tracked, flagged rate, median
overstatement), a claimed-vs-real discount bar comparison, and the flagged rate
by category.

**2 · Product timeline** — the money shot. Line chart of `selling_price` over
`dim_date[date]` for a selected listing, with:
- a shaded band over the inflation window (`rise_start_date` → `sale_start_date`)
- a constant line at `baseline_price`
- the trailing-30-day minimum as a second series

This is the visual that makes the whole argument in one glance.

**3 · Wall of shame** — flagged listings sorted by overstatement, with the
what-if slider, filterable by site, category and confidence tier. Drill-through
to page 2 for any row.

**4 · MRP contradictions** — products where sellers disagree, showing the spread
and each site's quote. Needs only one day of data, so this page is live first.

**5 · Scarcity claims** — percentage of stock counters that never move, by site.

**6 · Collection health** — observations per day per site from
`vw_collection_health`. Not decorative: silent scraper failure is the project's
single biggest risk, and a gap in this chart is the alarm.

---

## Publishing

**Publish to web** (File → Publish to web) produces a public URL that anyone can
open without a Power BI account — which is what makes the dashboard a clickable
demo rather than a `.pbix` file nobody can view.

Two caveats: the tenant admin must permit it, and the published page is genuinely
public. Neither matters here — the data is public retail pricing.

Also commit **PNG screenshots** of each page to `powerbi/screenshots/` and embed
them in the root README. GitHub cannot render `.pbix`, so without screenshots
the dashboard is invisible to anyone browsing the repository.
