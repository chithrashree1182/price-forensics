# Building the dashboard — click-by-click

Companion to [README.md](README.md), which holds the model diagram and every
DAX measure. This file is the ordered checklist: follow it top to bottom and
you have a working dashboard in roughly half a day, most of it on page design.

**Requirements:** Power BI Desktop (free, Windows only —
aka.ms/pbidesktopstore). On a Mac, use any Windows machine or VM; the `.pbix`
file itself is portable.

---

## 0. Get the data (5 min)

On the machine with the repo:

```bash
pf rebuild            # rebuild warehouse from committed daily snapshots
pf analyse all        # populate analysis tables
pf export --format csv
```

Copy the whole `data/exports/powerbi/` folder to the Windows machine (or clone
the repo there and run the same commands).

Files you should see: `dim_product`, `dim_seller`, `dim_listing`, `dim_date`,
`fact_price_observation`, `fact_dark_pattern`, `inflation_event`,
`mrp_contradiction`, `analysis_run` (CSV or Parquet).

## 1. Load (10 min)

1. Power BI Desktop → **Get data** → **Text/CSV** → load each file
   (or **Folder** and combine, if using Parquet).
2. In Power Query, check column types: every `*_id` and `date_key` = Whole
   Number, prices = Decimal, `date` = Date.
3. **Close & Apply**.

## 2. Model (15 min) — this is the part that matters

Model view → drag to create relationships, ALL single-direction, 1-to-many
(dimension → fact):

| From (1) | To (*) |
|---|---|
| dim_date[date_key] | fact_price_observation[date_key] |
| dim_product[product_id] | fact_price_observation[product_id] |
| dim_seller[seller_id] | fact_price_observation[seller_id] |
| dim_listing[listing_id] | fact_price_observation[listing_id] |
| dim_listing[listing_id] | inflation_event[listing_id] |
| dim_listing[listing_id] | fact_dark_pattern[listing_id] |

Then, **critically**: select `dim_date` → Table tools → **Mark as date table**
→ column `date`. Every time-intelligence measure is blank without this.

Do NOT set any relationship to bidirectional. If a visual shows blank or wrong
totals later, the cause is almost always a missing relationship here — fix the
model, don't patch it with DAX.

## 3. Measures (20 min)

New measure → paste each definition from [README.md](README.md#measures), in
this order (later ones reference earlier ones):

1. Core: `Listings Tracked`, `Observations`, `Avg Selling Price`,
   `Advertised Discount %`
2. Headline: `Flagged Listings`, `Flagged Rate %`,
   `Median Overstatement (pp)`, `Claimed vs Real Discount`
3. Time intelligence: `Rolling 7D Avg Price`, `Trailing 30D Min`,
   `Premium Over 30D Low %`
4. Festival: `Festival Discount %`, `Non-Festival Discount %`,
   `Festival Uplift (pp)`
5. Scarcity: `Static Stock Claims`

Put them all in a dedicated measure table: Home → Enter data → name it
`_Measures`, delete its column after moving measures in. Keeps the field list
clean.

## 4. What-if parameter (5 min)

Modeling → **New parameter** → Numeric range: name `Min Overstatement (pp)`,
0 to 40, increment 1. Add the `Listings Above Threshold` measure from the
README. This is the credibility slider — it belongs on the wall-of-shame page.

## 5. Pages (2–3 hours, the fun part)

Build in this order — page 4 works with day-one data, the others improve as
history accumulates:

1. **Overview** — cards: Listings Tracked, Observations, Flagged Rate %,
   Median Overstatement. Bar: Advertised Discount % by category. Line:
   Observations by date (collection health at a glance).
2. **MRP credibility** — the first-finding page. Table: distinct MRPs vs
   products by site (from `mrp_contradiction` / credibility export). Bar:
   median discount by seller type. This page has real numbers from day one.
3. **Product timeline** — line chart of selling_price by date, listing slicer.
   Add `Trailing 30D Min` as a second line. Once inflation events exist, add
   a constant line at `baseline_price`. This becomes the money shot.
4. **Wall of shame** — table from `inflation_event` sorted by overstatement,
   with the what-if slicer and confidence filter. Empty until the detector
   fires — put a text box saying so honestly ("no events detected yet;
   detector needs N weeks of history").
5. **Collection health** — line of observations/day by site. A dip = scraper
   broke. Not decorative: this page is the alarm.

Theme: View → Themes → pick one, or keep default. Spend design time on page 3,
not on colours.

## 6. Row-Level Security (10 min, optional but interview gold)

Modeling → **Manage roles** → New role `Seller Analyst` on `dim_seller`:
`[site_key] = "snapdeal"`. Test with Modeling → **View as**. Because
relationships flow dimension → fact, filtering dim_seller correctly restricts
every fact table with zero extra work — that is the star schema paying off,
and worth saying exactly that way in an interview.

## 7. Publish (10 min)

1. Save as `powerbi/price-forensics.pbix` in the repo (it is small; commit it).
2. Screenshot each page → `powerbi/screenshots/` → commit. GitHub cannot
   render .pbix; screenshots are how the dashboard exists for repo visitors.
3. Optional public link: File → **Publish to web** needs a Power BI account
   whose tenant allows it (personal/free accounts via app.powerbi.com usually
   do). The published URL goes in the README header and the repo's About
   field.

## Refresh routine

Weekly: `git pull && pf rebuild && pf analyse all && pf export --format csv`,
copy the folder over, then Power BI → **Refresh**. Every visual updates; no
rebuild needed.
