#!/usr/bin/env bash
#
# Momentum-screen wrapper (invoked by momentum-rotation.service).
#
#   - flock so a scheduled run can never overlap a manual one. The screen's file
#     write is already atomic (temp + rename); the lock additionally keeps two
#     Polygon runs from racing / double-spending the free-tier call budget.
#   - Runs the screen and propagates its exit code so systemd records failures.
#
set -uo pipefail

REPO="/root/trading-bot"
cd "$REPO"

LOCK="$REPO/momentum.lock"

# Non-blocking lock: if a run is still going, skip cleanly rather than piling on.
exec 9>"$LOCK" || { echo "$(date -Is) momentum: cannot open lock $LOCK"; exit 1; }
if ! flock -n 9; then
    echo "$(date -Is) momentum: another run holds the lock — skipping this cycle."
    exit 0
fi

echo "===== $(date -Is) momentum-screen START ====="
/usr/bin/python3 momentum_screen.py
rc=$?
echo "===== $(date -Is) momentum-screen END (exit=$rc) ====="
exit "$rc"
