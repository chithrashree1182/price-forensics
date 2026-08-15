"""Synthetic price-history generator.

Two legitimate uses, and one forbidden one.

USE 1 — pipeline testing.
    A longitudinal study is unusable for weeks. Synthetic history lets the
    warehouse, the SQL, the analysis and the Power BI model be built and tested
    on day one, so that when real data lands the pipeline is already correct.

USE 2 — detector validation against known ground truth.
    This is the more interesting one. We *plant* a known number of inflation
    events, then run the detector and measure how many it recovers. Manual
    review of real flags gives precision; this gives recall, which cannot be
    measured on real data at all (you never know what you missed).

    Reporting both is what separates a measured detector from an asserted one.

FORBIDDEN — synthetic rows must never appear in a reported finding. Every row
is written with source='synthetic', every analysis query filters on
source='live' by default, and `pf status` prints a loud warning whenever
synthetic rows are present in the database.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .config import EXPORT_DIR, ensure_dirs
from .db import connect, insert_dark_pattern, insert_observation, upsert_listing, upsert_product, upsert_seller
from .normalize import parse_title

log = logging.getLogger(__name__)

SITES = [("flipkart", "Flipkart"), ("croma", "Croma"), ("reliance", "Reliance Digital")]

CATALOGUE = [
    # (brand, model, variant, category, true_mrp, street_price)
    ("Samsung", "Galaxy S24", "8gb-128gb", "mobiles", 79999, 62999),
    ("Samsung", "Galaxy M35", "6gb-128gb", "mobiles", 24999, 16999),
    ("OnePlus", "Nord CE4", "8gb-128gb", "mobiles", 26999, 21999),
    ("Xiaomi", "Redmi Note 13", "6gb-128gb", "mobiles", 18999, 14499),
    ("Apple", "iPhone 15", "128gb", "mobiles", 79900, 66999),
    ("Realme", "Narzo 70", "8gb-256gb", "mobiles", 20999, 15999),
    ("Motorola", "Edge 50", "8gb-256gb", "mobiles", 27999, 22999),
    ("Vivo", "V30", "8gb-128gb", "mobiles", 33999, 27999),
    ("HP", "Pavilion 14", "16gb-512gb", "laptops", 78999, 61990),
    ("Lenovo", "IdeaPad Slim 5", "16gb-512gb", "laptops", 82999, 58990),
    ("Asus", "Vivobook 15", "8gb-512gb", "laptops", 59990, 44990),
    ("Acer", "Aspire Lite", "8gb-512gb", "laptops", 49999, 33990),
    ("Dell", "Inspiron 15", "16gb-512gb", "laptops", 71999, 54990),
    ("Sony", "WH-1000XM5", "", "headphones", 34990, 26990),
    ("Boat", "Rockerz 550", "", "headphones", 4499, 1799),
    ("JBL", "Tune 770NC", "", "headphones", 12999, 7999),
    ("Noise", "Buds VS104", "", "headphones", 3999, 1199),
    ("Sennheiser", "HD 450BT", "", "headphones", 19990, 9990),
]


@dataclass
class PlantedEvent:
    """Ground truth for a deliberately planted inflation."""

    listing_id: int
    site_key: str
    title: str
    baseline: float
    peak: float
    sale: float
    rise_start: str
    sale_start: str
    true_overstatement_pp: float


@dataclass
class GenerationReport:
    n_listings: int
    n_observations: int
    planted: list[PlantedEvent] = field(default_factory=list)
    honest_sales: int = 0
    seed: int = 0
    start_date: str = ""
    end_date: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "n_listings": self.n_listings,
            "n_observations": self.n_observations,
            "n_planted_inflations": len(self.planted),
            "n_honest_sales": self.honest_sales,
            "planted": [
                {
                    "listing_id": p.listing_id,
                    "site_key": p.site_key,
                    "title": p.title,
                    "baseline": p.baseline,
                    "peak": p.peak,
                    "sale": p.sale,
                    "rise_start": p.rise_start,
                    "sale_start": p.sale_start,
                    "true_overstatement_pp": p.true_overstatement_pp,
                }
                for p in self.planted
            ],
        }


def generate(
    days: int = 90,
    end_date: date | None = None,
    seed: int = 42,
    inflation_rate: float = 0.22,
    honest_sale_rate: float = 0.30,
    mrp_inflation_rate: float = 0.25,
    db_path=None,
) -> GenerationReport:
    """Populate the warehouse with a synthetic but realistic price history.

    Parameters mirror plausible market behaviour: roughly a fifth of listings
    run a manufactured discount, a third run an honest one, and a quarter of
    listings quote an inflated MRP.
    """
    rng = random.Random(seed)
    end_date = end_date or date.today()
    start_date = end_date - timedelta(days=days - 1)

    # A sale window in the last third of the period, so there is enough
    # pre-sale history for the detector to establish a baseline.
    sale_start = start_date + timedelta(days=int(days * 0.72))
    sale_end = sale_start + timedelta(days=7)

    report = GenerationReport(
        n_listings=0, n_observations=0, seed=seed,
        start_date=start_date.isoformat(), end_date=end_date.isoformat(),
    )

    with connect(db_path) as conn:
        for brand, model, variant, category, true_mrp, street in CATALOGUE:
            # Not every product is on every site.
            listed_on = [s for s in SITES if rng.random() < 0.8] or [SITES[0]]

            for site_key, site_name in listed_on:
                title = f"{brand} {model}" + (f" ({variant.upper()})" if variant else "")
                parsed = parse_title(title, category)

                product_id = upsert_product(
                    conn,
                    match_key=parsed.match_key,
                    brand=parsed.brand,
                    model=parsed.model,
                    variant=parsed.variant,
                    category=category,
                    canonical_title=parsed.canonical_title,
                    seen_date=start_date.isoformat(),
                )
                seller_id = upsert_seller(
                    conn, site_key=site_key, site_name=site_name, seller_name=None
                )
                url = f"https://www.{site_key}.com/{brand}-{model}-{variant}".lower().replace(" ", "-")
                listing_id = upsert_listing(
                    conn,
                    product_id=product_id,
                    seller_id=seller_id,
                    site_key=site_key,
                    url=url,
                    raw_title=title,
                    site_product_id=f"{site_key.upper()}{listing_id_hash(url)}",
                    seen_date=start_date.isoformat(),
                )
                report.n_listings += 1

                # --- MRP: usually the true value, sometimes inflated --------
                if rng.random() < mrp_inflation_rate:
                    quoted_mrp = round(true_mrp * rng.uniform(1.12, 1.55), -1)
                else:
                    quoted_mrp = float(true_mrp)

                # --- decide this listing's storyline ------------------------
                does_inflate = rng.random() < inflation_rate
                does_honest_sale = (not does_inflate) and rng.random() < honest_sale_rate
                if does_honest_sale:
                    report.honest_sales += 1

                base_price = street * rng.uniform(0.96, 1.04)
                rise_start_day: date | None = None
                peak_price = base_price

                if does_inflate:
                    # Raise 10–35% somewhere in the 8–20 days before the sale.
                    lead_days = rng.randint(8, 20)
                    rise_start_day = sale_start - timedelta(days=lead_days)
                    peak_price = base_price * rng.uniform(1.10, 1.35)

                sale_price = base_price * rng.uniform(0.80, 0.93) if (does_inflate or does_honest_sale) else None

                # --- emit the daily series ----------------------------------
                observed_dates = []
                cur = start_date
                while cur <= end_date:
                    # ~4% of days are missed: outages, blocks, timeouts. The
                    # detector must cope with gaps, so the fixture has them.
                    if rng.random() < 0.04:
                        cur += timedelta(days=1)
                        continue

                    if sale_start <= cur <= sale_end and sale_price:
                        price = sale_price
                    elif rise_start_day and rise_start_day <= cur < sale_start:
                        price = peak_price
                    else:
                        # Slow drift: consumer electronics depreciate.
                        age_factor = 1 - 0.0004 * (cur - start_date).days
                        price = base_price * age_factor

                    price = round(price * rng.uniform(0.995, 1.005), 0)
                    if quoted_mrp < price:
                        quoted_mrp = round(price * 1.05, -1)

                    insert_observation(
                        conn,
                        listing_id=listing_id,
                        product_id=product_id,
                        seller_id=seller_id,
                        obs_date=cur.isoformat(),
                        selling_price=price,
                        mrp=quoted_mrp,
                        in_stock=1,
                        source="synthetic",
                    )
                    observed_dates.append(cur)
                    report.n_observations += 1

                    # --- scarcity messaging ---------------------------------
                    # A third of listings show a *static* counter (fake), the
                    # rest show one that actually decrements.
                    if listing_id % 3 == 0:
                        insert_dark_pattern(
                            conn, listing_id=listing_id, obs_date=cur.isoformat(),
                            pattern_type="stock_claim",
                            raw_text="only 3 left in stock - order soon",
                            numeric_value=3.0,
                        )
                    elif listing_id % 3 == 1:
                        remaining = max(1, 40 - (cur - start_date).days // 2)
                        insert_dark_pattern(
                            conn, listing_id=listing_id, obs_date=cur.isoformat(),
                            pattern_type="stock_claim",
                            raw_text=f"only {remaining} left in stock",
                            numeric_value=float(remaining),
                        )

                    cur += timedelta(days=1)

                if does_inflate and sale_price and rise_start_day:
                    claimed = (peak_price - sale_price) / peak_price * 100
                    real = (base_price - sale_price) / base_price * 100
                    report.planted.append(PlantedEvent(
                        listing_id=listing_id,
                        site_key=site_key,
                        title=title,
                        baseline=round(base_price, 2),
                        peak=round(peak_price, 2),
                        sale=round(sale_price, 2),
                        rise_start=rise_start_day.isoformat(),
                        sale_start=sale_start.isoformat(),
                        true_overstatement_pp=round(claimed - real, 2),
                    ))

    ensure_dirs()
    truth_path = EXPORT_DIR / "synthetic_ground_truth.json"
    truth_path.write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")

    log.info(
        "generated %s listings, %s observations, %s planted inflations "
        "(ground truth -> %s)",
        report.n_listings, report.n_observations, len(report.planted), truth_path,
    )
    return report


def listing_id_hash(url: str) -> str:
    """Deterministic pseudo-SKU.

    Python's built-in hash() is salted per process, which would make the whole
    fixture irreproducible between runs. md5 is not a security choice here, just
    a stable one.
    """
    import hashlib

    return hashlib.md5(url.encode("utf-8")).hexdigest()[:7].upper()


def evaluate_detector(
    ground_truth_path: Path | None = None,
    db_path=None,
) -> dict[str, Any]:
    """Measure detector recall against planted events.

    Precision from manual review answers "are the flags real?".
    This answers "what did it miss?" — the question real data cannot.
    """
    from .analysis import inflation

    truth_path = ground_truth_path or (EXPORT_DIR / "synthetic_ground_truth.json")
    truth = json.loads(Path(truth_path).read_text(encoding="utf-8"))
    planted_ids = {p["listing_id"] for p in truth["planted"]}

    events = inflation.detect(include_backfill=True, db_path=db_path)
    detected_ids = {e.listing_id for e in events}

    true_positives = planted_ids & detected_ids
    false_negatives = planted_ids - detected_ids
    false_positives = detected_ids - planted_ids

    recall = len(true_positives) / len(planted_ids) if planted_ids else 0.0
    precision = len(true_positives) / len(detected_ids) if detected_ids else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    # How close were the recovered magnitudes to the planted ones?
    truth_by_listing = {p["listing_id"]: p for p in truth["planted"]}
    errors = [
        abs(e.overstatement_pp - truth_by_listing[e.listing_id]["true_overstatement_pp"])
        for e in events if e.listing_id in truth_by_listing
    ]

    return {
        "n_planted": len(planted_ids),
        "n_detected": len(detected_ids),
        "true_positives": len(true_positives),
        "false_negatives": len(false_negatives),
        "false_positives": len(false_positives),
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "f1": round(f1, 4),
        "mean_abs_error_overstatement_pp": (
            round(sum(errors) / len(errors), 2) if errors else None
        ),
    }
