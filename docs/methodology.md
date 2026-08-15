# Methodology

## The claim being tested

A discount is a comparison between two prices: what you pay, and what you would
otherwise have paid. Retailers control both numbers, and only the first is
verifiable by the shopper.

This study reconstructs the second number independently, by recording what
products actually cost over time, and then asks how often the advertised saving
matches the real one.

---

## Study design

**Unit of observation.** One listing (a product on a specific site) on one day.
Daily granularity is deliberate: the behaviour under study operates on a scale
of days, and sub-daily sampling would multiply storage without changing a
conclusion.

**Two samples running in parallel:**

| | Panel | Sweep |
|---|---|---|
| What | Fixed set of ~300 products, followed daily | Category listing pages, ~120 products/category/site/day |
| Purpose | Longitudinal — the inflation analysis | Cross-sectional — the MRP audit |
| Stability | Append-only; changing it mid-study breaks comparability | Refreshes naturally as listings rotate |

**Negative control.** Groceries are tracked alongside electronics and fashion.
Grocery pricing is more regulated and less discount-driven, so a detector that
flags groceries at the same rate as electronics is measuring noise, not
behaviour.

---

## Detection: manufactured discounts

### The signature

```
₹ ────────────────┐
                  └──────────┐
                             └──────
   |<-- baseline -->|<- rise ->|<- sale ->|
```

A flag requires **all** of:

1. A price drop of ≥10% (the "sale" moment)
2. Preceded, within 21 days, by a rise of ≥10% over a settled baseline
3. The elevated price held for ≥3 consecutive days
4. At least 2 observations *below* the elevated level, so a baseline exists
5. At least 10 observations for the listing overall

### The output metric

```
claimed_discount   = (peak     − sale) / peak     × 100
real_discount      = (baseline − sale) / baseline × 100
overstatement_pp   = claimed_discount − real_discount
```

Reported in **percentage points**, not as a ratio. "Advertised 60% off, real
discount 22%, overstated by 38 points" is unambiguous; "2.7× exaggeration"
invites misreading.

### Confidence tiers

Flags are scored on rise magnitude, hold duration, observation count,
overstatement size, and whether the rise fully reversed. High and medium tiers
carry the headline rate; low-confidence flags are reported separately and never
folded into the top-line number.

---

## Detection: MRP contradictions

MRP is manufacturer-declared and governed by the Legal Metrology (Packaged
Commodities) Rules. It is a property of the product. Where sellers disagree, at
least one quoted figure is not the MRP — and that figure is the denominator of
the advertised discount.

**Requires one day of data.** Method: group listings by normalised product,
take one quote per site, flag where `(max − min) / median > 2%` with ≥3 sellers.

**The hard part is not the statistics, it is the matching.** These are the same
SKU:

```
Flipkart : SAMSUNG Galaxy S24 5G (Onyx Black, 128 GB)  (8 GB RAM)
Croma    : Samsung Galaxy S24 5G (8GB RAM, 128GB, Onyx Black)
Amazon   : Samsung Galaxy S24 5G AI Smartphone (Marble Grey, 8GB, 128GB Storage)
```

All three reduce to `samsung|galaxy s24|8gb-128gb`. Colour is dropped (colour
variants share an MRP, so keeping it would fragment groups and hide genuine
contradictions); storage and RAM are kept (they genuinely price differently).
Products that cannot be parsed confidently are excluded from this analysis
rather than guessed at.

If normalisation is wrong, two different products get compared and the finding
is garbage — and it fails *silently*, producing plausible numbers. That is why
`tests/test_normalize.py` is the largest test file in the project.

---

## Detection: scarcity claims

A genuine stock counter depletes monotonically between restocks. Three tests:

1. **Persistence** — the same number repeated across many consecutive days
2. **Non-monotonicity** — a counter wandering up and down (3 → 7 → 2 → 5) with
   no intervening stock-out
3. **Timing** — whether urgency messaging intensifies during sale windows

Test 1 is the strongest: *"only 2 left"* unchanged for 30 consecutive days while
the item remains purchasable is not an inventory figure.

---

## Validation

### Precision — are the flags real?

A **seeded random sample** of flags is exported to CSV. The reviewer opens each
product's archived snapshots and price chart and returns
`confirmed` / `rejected` / `unclear`.

- Sampling is random, not sorted by severity. Reviewing only the most extreme
  flags would inflate precision, which is exactly why the sampler does not offer
  a sort option.
- `unclear` verdicts are excluded from the denominator and reported separately.
  Counting them either way biases the estimate; hiding them overstates how clean
  the labelling was.
- Reported with a **Wilson score interval**, because at n≈100 with p near 1.0
  the normal approximation both misbehaves and can exceed 1.

Sample size is an honest constraint: at p≈0.85, ±10pp needs ~49 reviews and
±5pp needs ~196. The study reviews ~100 and reports the interval it earns.

### Recall — what was missed?

Unanswerable on real data. So the synthetic generator plants a known number of
inflation events, mixed with honest sales as negative controls, and the detector
is scored against that ground truth.

**This is a lower bound on difficulty and must be described as such.** The
generator plants the pattern the detector searches for, so a perfect score
confirms the implementation matches its specification — it does not demonstrate
performance on real, messy data.

---

## Data provenance

Every observation carries a `source`:

| source | Meaning | In reported findings? |
|---|---|---|
| `live` | Scraped from the retailer | ✅ Yes |
| `wayback` | Reconstructed from Internet Archive snapshots | ⚠️ Illustration only |
| `synthetic` | Generated fixture | ❌ Never |

Analysis queries filter on `source = 'live'` by default; overriding requires an
explicit flag, and `pf status` prints a warning whenever synthetic rows exist in
the database.

**Why Wayback data is excluded from rate statistics:** archive coverage is not
random. Pages get snapshotted more when they are being linked to and shared,
which correlates with sale events. That biases any rate computed from it. It is
used to illustrate individual product histories, where the bias does not apply.

---

## Known limitations

**Observation gaps.** Scrapers fail. A missed week around a price transition
means the timing cannot be recovered, so those listings are skipped rather than
interpolated.

**Seller substitution.** On marketplaces, an apparent price change may be a
different seller winning the buy box. Tracking is per-listing-URL, which
mitigates but does not eliminate this.

**Genuine price rises exist.** Component costs and exchange rates move. The
requirement that the rise be recent, sharp, *and* reversed by the sale
distinguishes most of these, but not all — which is what the manual review
measures.

**Category coverage is narrow.** Electronics and fashion only. Findings should
not be generalised to categories that were not sampled.

**No causal claim.** The study measures a pattern in observed prices. It does
not establish intent, and nothing in the data can.
