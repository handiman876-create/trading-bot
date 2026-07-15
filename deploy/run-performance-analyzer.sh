#!/usr/bin/env bash
#
# Performance-analyzer wrapper (invoked by performance-analyzer.service).
#
#   - flock so a scheduled run can never overlap a manual one. The analyzer's
#     ledger + report writes are already atomic (temp + rename); the lock just
#     keeps two runs from racing on the ledger.
#   - Runs the analyzer and propagates its exit code so systemd records failures.
#
set -uo pipefail

REPO="/root/trading-bot"
cd "$REPO"

LOCK="$REPO/performance.lock"

exec 9>"$LOCK" || { echo "$(date -Is) perf: cannot open lock $LOCK"; exit 1; }
if ! flock -n 9; then
    echo "$(date -Is) perf: another run holds the lock — skipping this cycle."
    exit 0
fi

echo "===== $(date -Is) performance-analyzer START ====="
/usr/bin/python3 performance_analyzer.py
rc=$?
echo "===== $(date -Is) performance-analyzer END (exit=$rc) ====="
exit "$rc"
