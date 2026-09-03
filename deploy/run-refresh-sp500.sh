#!/usr/bin/env bash
#
# S&P 500 constituent refresh wrapper (invoked by sp500-refresh.service).
#
#   - flock on the SAME momentum.lock the screen uses. This is deliberate: the
#     refresh rewrites data/sp500.json, which is exactly the universe file
#     momentum_screen.py reads. Sharing one lock is what guarantees the screen
#     can never read a half-swapped universe, and is why this runs 30 min ahead
#     of momentum-rotation rather than alongside it.
#   - Propagates the exit code so a failed fetch shows up as a failed unit
#     instead of a silently stale file. Per the fail-safe rule, degrading here
#     still exits non-zero: the old file staying in place is the graceful part,
#     but the timer must NOT go green on it.
#
# Source is GitHub (constituents.csv), NOT Polygon — so this does not spend any
# of the shared 5-calls/min free-tier budget.
#
set -uo pipefail

REPO="/root/trading-bot"
cd "$REPO"

LOCK="$REPO/momentum.lock"

# Non-blocking lock: if the screen (or a manual refresh) is running, skip
# cleanly. Exit 0 here is correct — nothing is stale, we simply deferred.
exec 9>"$LOCK" || { echo "$(date -Is) sp500-refresh: cannot open lock $LOCK"; exit 1; }
if ! flock -n 9; then
    echo "$(date -Is) sp500-refresh: another run holds the lock — skipping this cycle."
    exit 0
fi

echo "===== $(date -Is) sp500-refresh START ====="
/usr/bin/python3 refresh_sp500.py
rc=$?
echo "===== $(date -Is) sp500-refresh END (exit=$rc) ====="
exit "$rc"
