"""Command-line interface.

    pf init                       create the warehouse
    pf doctor                     check whether site selectors still work
    pf collect sweep              daily category sweep
    pf collect panel              daily detail-page scrape
    pf backfill --url URL         pull historical prices from the Internet Archive
    pf analyse all                run every analysis and store results
    pf analyse credibility        do a retailer's MRPs behave like real ones?
    pf ml benchmark               rule-based vs Isolation Forest vs changepoint
    pf ml sensitivity             how much IF depends on its contamination prior
    pf validate sample --run 3    draw a review sample
    pf validate report --run 3    precision + confidence interval
    pf synth generate             build a synthetic fixture for development
    pf synth evaluate             measure detector recall against ground truth
    pf export                     write Power BI tables
    pf status                     row counts and data health
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from . import collect, db, export, snapshot, synthetic
from .analysis import (dark_patterns, inflation, ml_detector, mrp_audit,
                       mrp_credibility, validation)
from .config import DB_PATH, load_sites
from .scrapers import get_scraper


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, default=str))


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    db.init_db()
    print(f"warehouse ready at {DB_PATH}")
    _print(db.table_counts())
    return 0


def cmd_doctor(args) -> int:
    """Check whether the configured selectors still match the live sites."""
    sites = load_sites()
    keys = args.site or [k for k, s in sites.items() if s.strategy != "wayback"]
    failures = 0

    for key in keys:
        site = sites.get(key)
        if site is None:
            print(f"unknown site: {key}")
            failures += 1
            continue
        with get_scraper(site) as scraper:
            if not hasattr(scraper, "health_check"):
                continue
            report = scraper.health_check()
        _print(report)
        for cat, result in report.get("categories", {}).items():
            if not result.get("ok"):
                failures += 1
                print(f"  BROKEN: {key}/{cat} — update selectors in config/sites.yaml",
                      file=sys.stderr)

    if failures:
        print(f"\n{failures} selector group(s) failing", file=sys.stderr)
        return 1
    print("\nall selectors healthy")
    return 0


def cmd_collect(args) -> int:
    obs_date = _parse_date(args.date)
    if args.what == "sweep":
        report = collect.run_sweep(
            site_keys=args.site,
            category_ids=args.category,
            obs_date=obs_date,
            pages=args.pages,
            dry_run=args.dry_run,
        )
    else:
        report = collect.run_panel(
            obs_date=obs_date, limit=args.limit, dry_run=args.dry_run
        )
    _print(report)
    return 0


def cmd_backfill(args) -> int:
    sites = load_sites()
    wayback_site = sites["wayback"]
    # Reuse the target retailer's detail selectors for old snapshots.
    target = sites.get(args.site) if args.site else None
    if target:
        object.__setattr__(wayback_site, "detail", target.detail)

    scraper = get_scraper(wayback_site)
    stored = 0
    try:
        for obs_date, item in scraper.backfill_url(
            args.url,
            from_date=_parse_date(args.since),
            to_date=_parse_date(args.until),
            max_snapshots=args.max_snapshots,
        ):
            item.site_key = args.site or "wayback"
            item.category = args.category or "unknown"
            collect.store_items([item], obs_date)
            stored += 1
            print(f"  {obs_date}  ₹{item.selling_price:,.0f}  {item.raw_title[:60]}")
    finally:
        scraper.close()

    print(f"\nbackfilled {stored} historical observations")
    return 0


def cmd_analyse(args) -> int:
    which = args.what
    out = {}

    if which in ("mrp", "all"):
        run_id, findings = mrp_audit.run_and_store(
            obs_date=_parse_date(args.date),
            include_synthetic=args.include_synthetic,
        )
        out["mrp_audit"] = {"run_id": run_id, **mrp_audit.summarise(findings)}
        for f in findings[: args.top]:
            print(f"  [MRP] {f.headline}")

    if which in ("inflation", "all"):
        run_id, events = inflation.run_and_store(
            include_backfill=args.include_backfill or args.include_synthetic
        )
        counts = db.table_counts()
        out["inflation"] = {
            "run_id": run_id,
            **inflation.summarise(events, counts.get("dim_listing")),
        }
        for e in events[: args.top]:
            print(f"  [INFL] {e.headline}")

    if which in ("credibility", "all"):
        profiles = mrp_credibility.profile_retailers(
            include_synthetic=args.include_synthetic)
        for p_ in profiles:
            print(f"  [MRP-CRED] {p_.headline}")
            for v, n in p_.most_repeated[:3]:
                print(f"             Rs {v:>9,.0f} claimed by {n} different products")
        cmp_ = mrp_credibility.compare(profiles)
        out["mrp_credibility"] = mrp_credibility.summarise(profiles)
        if "finding" in cmp_:
            print(f"\n  {cmp_['finding']}")
            out["mrp_credibility"]["finding"] = cmp_["finding"]

    if which in ("patterns", "all"):
        findings = dark_patterns.analyse()
        out["dark_patterns"] = dark_patterns.summarise(findings)
        for f in findings[: args.top]:
            if f.verdict in ("static", "erratic"):
                print(f"  [DARK] {f.headline}")

    print()
    _print(out)

    if args.write_findings:
        path = export.export_findings_markdown()
        print(f"\nfindings written to {path}")
    return 0


def cmd_ml(args) -> int:
    if args.action == "detect":
        flags = ml_detector.isolation_forest(
            contamination=args.contamination,
            include_synthetic=args.include_synthetic,
        )
        for f in flags[: args.top]:
            print(f"  [IF] {f.headline}")
        print(f"\n{len(flags)} listings flagged")
    elif args.action == "changepoint":
        flags = ml_detector.changepoint(include_synthetic=args.include_synthetic)
        for f in flags[: args.top]:
            print(f"  [CP] {f.headline}")
        print(f"\n{len(flags)} listings flagged")
    elif args.action == "benchmark":
        r = ml_detector.benchmark(contamination=args.contamination)
        hdr = ["detector", "flagged", "TP", "FP", "FN", "prec", "recall", "F1"]
        print(f"{hdr[0]:<18}{hdr[1]:>8}{hdr[2]:>5}{hdr[3]:>5}{hdr[4]:>5}"
              f"{hdr[5]:>7}{hdr[6]:>8}{hdr[7]:>7}")
        print("-" * 63)
        for row in r["table"]:
            print(f"{row[0]:<18}{row[1]:>8}{row[2]:>5}{row[3]:>5}{row[4]:>5}"
                  f"{row[5]:>7}{row[6]:>8}{row[7]:>7}")
        print(f"\n{r['interpretation']}")
    else:
        r = ml_detector.contamination_sensitivity()
        print(f"true anomaly rate: {r['true_anomaly_rate']:.3f}\n")
        print(f"{'contamination':>14}{'flagged':>9}{'prec':>7}{'recall':>8}{'F1':>7}")
        print("-" * 45)
        for s_ in r["sweep"]:
            print(f"{s_['contamination']:>14.2f}{s_['n_flagged']:>9}"
                  f"{s_['precision']:>7.2f}{s_['recall']:>8.2f}{s_['f1']:>7.2f}")
        print(f"\n{r['finding']}")
    return 0


def cmd_validate(args) -> int:
    if args.action == "sample":
        path = validation.draw_sample(run_id=args.run, n=args.n, seed=args.seed)
        needed = validation.required_sample_size()
        print(f"sample written to {path}")
        print(f"fill in the 'verdict' column with: confirmed | rejected | unclear")
        print(f"(~{needed} reviews gives a ±10pp margin of error)")
    elif args.action == "load":
        n = validation.load_verdicts(Path(args.csv))
        print(f"loaded {n} verdicts")
    else:
        est = validation.precision_report(run_id=args.run)
        print(est.headline)
        _print(est.__dict__)
    return 0


def cmd_synth(args) -> int:
    if args.action == "generate":
        report = synthetic.generate(days=args.days, seed=args.seed)
        _print(report.to_json() | {"planted": f"{len(report.planted)} events (see exports)"})
        print("\nNOTE: these rows are source='synthetic' and are excluded from")
        print("      exports and reported findings by default.")
    else:
        _print(synthetic.evaluate_detector())
    return 0


def cmd_export(args) -> int:
    counts = export.export_all(
        out_dir=Path(args.out) if args.out else None,
        fmt=args.format,
        include_synthetic=args.include_synthetic,
    )
    _print(counts)
    return 0


def cmd_snapshot(args) -> int:
    path = snapshot.write_snapshot(obs_date=_parse_date(args.date))
    if path is None:
        print("nothing to snapshot")
        return 1
    print(f"wrote {path}")
    return 0


def cmd_rebuild(args) -> int:
    db.init_db()
    stats = snapshot.rebuild(since=_parse_date(args.since))
    _print(stats)
    return 0


def cmd_status(args) -> int:
    counts = db.table_counts()
    _print(counts)

    rows = db.query(
        """
        SELECT source, COUNT(*) AS n, MIN(d.date) AS lo, MAX(d.date) AS hi
        FROM fact_price_observation o JOIN dim_date d ON d.date_key = o.date_key
        GROUP BY source
        """
    )
    print("\nobservations by source:")
    for r in rows:
        print(f"  {r['source']:<10} {r['n']:>8,}   {r['lo']} .. {r['hi']}")
        if r["source"] == "synthetic":
            print("  ^ WARNING: synthetic rows present — never report these as findings")

    gaps = db.query(
        """
        SELECT d.date, COUNT(*) AS n
        FROM fact_price_observation o JOIN dim_date d ON d.date_key = o.date_key
        WHERE o.source = 'live'
        GROUP BY d.date ORDER BY d.date DESC LIMIT 14
        """
    )
    if gaps:
        print("\nlast 14 collection days (live):")
        for r in gaps:
            print(f"  {r['date']}  {r['n']:>6,} observations")
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pf", description="Price Forensics")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the warehouse").set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="check site selectors still work")
    doctor.add_argument("--site", action="append")
    doctor.set_defaults(func=cmd_doctor)

    c = sub.add_parser("collect", help="scrape prices")
    c.add_argument("what", choices=["sweep", "panel"])
    c.add_argument("--site", action="append")
    c.add_argument("--category", action="append")
    c.add_argument("--date")
    c.add_argument("--pages", type=int)
    c.add_argument("--limit", type=int)
    c.add_argument("--dry-run", action="store_true")
    c.set_defaults(func=cmd_collect)

    b = sub.add_parser("backfill", help="historical prices from the Internet Archive")
    b.add_argument("--url", required=True)
    b.add_argument("--site")
    b.add_argument("--category")
    b.add_argument("--since")
    b.add_argument("--until")
    b.add_argument("--max-snapshots", type=int, default=60)
    b.set_defaults(func=cmd_backfill)

    a = sub.add_parser("analyse", help="run analyses")
    a.add_argument("what",
                   choices=["mrp", "credibility", "inflation", "patterns", "all"],
                   default="all",
                   nargs="?")
    a.add_argument("--date")
    a.add_argument("--top", type=int, default=10)
    a.add_argument("--include-backfill", action="store_true",
                   help="include Internet Archive observations")
    a.add_argument("--include-synthetic", action="store_true",
                   help="DEV ONLY: include synthetic fixture rows — never for reported figures")
    a.add_argument("--write-findings", action="store_true")
    a.set_defaults(func=cmd_analyse)

    m = sub.add_parser("ml", help="learned anomaly detectors and benchmark")
    m.add_argument("action", choices=["detect", "changepoint", "benchmark", "sensitivity"])
    m.add_argument("--contamination", type=float, default=0.25)
    m.add_argument("--top", type=int, default=10)
    m.add_argument("--include-synthetic", action="store_true")
    m.set_defaults(func=cmd_ml)

    v = sub.add_parser("validate", help="manual validation workflow")
    v.add_argument("action", choices=["sample", "load", "report"])
    v.add_argument("--run", type=int)
    v.add_argument("--n", type=int, default=100)
    v.add_argument("--seed", type=int, default=20260816)
    v.add_argument("--csv")
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("synth", help="synthetic fixtures for development")
    s.add_argument("action", choices=["generate", "evaluate"])
    s.add_argument("--days", type=int, default=90)
    s.add_argument("--seed", type=int, default=42)
    s.set_defaults(func=cmd_synth)

    e = sub.add_parser("export", help="write Power BI tables")
    e.add_argument("--out")
    e.add_argument("--format", choices=["parquet", "csv"], default="parquet")
    e.add_argument("--include-synthetic", action="store_true")
    e.set_defaults(func=cmd_export)

    sn = sub.add_parser("snapshot", help="write one day's rows to a committed CSV")
    sn.add_argument("--date")
    sn.set_defaults(func=cmd_snapshot)

    rb = sub.add_parser("rebuild", help="rebuild the warehouse from daily snapshots")
    rb.add_argument("--since")
    rb.set_defaults(func=cmd_rebuild)

    sub.add_parser("status", help="row counts and data health").set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
