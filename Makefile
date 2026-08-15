.PHONY: help install dev init doctor collect analyse test lint export demo ml clean

help:
	@echo "Price Forensics"
	@echo ""
	@echo "  make install    install package + browser deps"
	@echo "  make init       create the warehouse"
	@echo "  make doctor     check site selectors against live markup"
	@echo "  make collect    run today's sweep + panel"
	@echo "  make analyse    run all analyses, write FINDINGS.md"
	@echo "  make export     write Power BI tables"
	@echo "  make test       run the test suite"
	@echo "  make demo       synthetic fixture end-to-end (no network)"
	@echo "  make ml         rules vs Isolation Forest vs changepoint"
	@echo "  make clean      remove derived artefacts (keeps data/daily/)"

install:
	pip install -e ".[browser,export,ml,dev]"
	playwright install chromium

init:
	pf init

doctor:
	pf doctor

collect:
	pf collect sweep
	pf collect panel --limit 400
	pf snapshot

analyse:
	pf analyse all --top 10 --write-findings

export:
	pf export --format parquet

test:
	pytest -q

# Full pipeline on synthetic data — no network, no waiting. This is what to run
# on a fresh clone to see the project work end to end.
demo:
	rm -f data/prices.db
	pf init
	pf synth generate --days 90
	pf synth evaluate
	pf analyse all --top 5 --include-synthetic
	pf ml benchmark
	pf ml sensitivity
	@echo ""
	@echo "Synthetic rows are excluded from exports and reported findings."

ml:
	pf ml benchmark
	pf ml sensitivity

# Removes only derived artefacts. data/daily/ is the durable record and is
# never touched here.
clean:
	rm -f data/prices.db data/prices.db-wal data/prices.db-shm
	rm -rf data/exports data/raw
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache
