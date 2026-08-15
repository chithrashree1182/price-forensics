#!/usr/bin/env bash
# Daily collection — runs from a residential connection via launchd.
#
# Why not GitHub Actions: the retailers' CDN returns HTTP 403 to datacenter
# IPs, including for robots.txt itself. Python's robotparser treats an
# unreadable-by-403 robots.txt as "disallow everything" (fail-closed), so a
# GitHub-hosted runner makes zero requests by design. Run #1 in the Actions
# history documents this. Collection therefore runs from a network where
# robots.txt is actually served and actually permits these paths.
#
# Installed by scripts/install_launchd.sh; logs to ~/Library/Logs/priceforensics.log

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"
PF="$PY -m priceforensics.cli"
export PYTHONPATH="$REPO/src"
cd "$REPO"

echo "==================================================================="
echo "daily collection: $(date '+%Y-%m-%d %H:%M:%S %Z')"

# Stay in sync with the remote before producing today's commit.
git pull --rebase --quiet origin main || echo "WARN: git pull failed, continuing"

# The database is derived; make sure it exists and reflects all snapshots.
$PF init >/dev/null 2>&1 || true
$PF rebuild >/dev/null 2>&1 || true

# Selector canary first: if the sites changed markup, say so loudly today,
# not after three silent weeks.
if ! $PF doctor; then
    echo "ERROR: selector check failed — fix config/sites.yaml"
    # Continue anyway: partial collection beats none, and doctor's output is logged.
fi

$PF collect sweep --pages 3
$PF collect panel --limit 400
$PF analyse all --write-findings || echo "WARN: analyse failed (fine while history is short)"

if ! $PF snapshot; then
    echo "ERROR: nothing collected today — NOT committing. Check log."
    exit 1
fi

TODAY="$(date '+%Y-%m-%d')"
ROWS="$(gunzip -c "data/daily/${TODAY}.csv.gz" 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')"

git add data/daily/ FINDINGS.md
if git diff --staged --quiet; then
    echo "no new data to commit"
    exit 0
fi

git -c user.name="Chithrashree C" -c user.email="chithrashree1182@gmail.com" \
    commit -m "data: collection ${TODAY} (${ROWS} observations)"
git push origin main && echo "pushed: collection ${TODAY} (${ROWS} observations)"
