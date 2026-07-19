#!/usr/bin/env bash
#
# A/B screen tracker wrapper (invoked by screen-ab-tracker.service).
#
# OBSERVATION ONLY: this runs screen_ab_tracker.py, which records the A/B
# experiment to data/screen_ab_tracking.json and NEVER writes the live watchlist.
# Its own flock (separate from the live screen's) keeps a scheduled run from
# overlapping a manual one. It is timed an hour after the live momentum screen so
# the two never hit the shared free-tier Polygon key at the same time (which would
# blow the 5-calls/min budget and trip 429s).
#
set -uo pipefail

REPO="/root/trading-bot"
cd "$REPO"

LOCK="$REPO/screen_ab.lock"

exec 9>"$LOCK" || { echo "$(date -Is) screen-ab: cannot open lock $LOCK"; exit 1; }
if ! flock -n 9; then
    echo "$(date -Is) screen-ab: another run holds the lock — skipping this cycle."
    exit 0
fi

echo "===== $(date -Is) screen-ab-tracker START ====="
/usr/bin/python3 screen_ab_tracker.py
rc=$?
echo "===== $(date -Is) screen-ab-tracker END (exit=$rc) ====="
exit "$rc"
