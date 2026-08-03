#!/usr/bin/env bash
#
# Sentiment-analysis wrapper (invoked by sentiment-analysis.service, weekdays 08:00 ET).
#
#   - flock so a scheduled run can never overlap a manual one.
#   - Runs on the .venv python (system-site-packages) because the Anthropic SDK is
#     installed there, not in system python3 — the repo rule is a --system-site-packages
#     venv, never --break-system-packages. The venv still sees requests/dotenv.
#   - Exports ANTHROPIC_API_KEY from the sibling strategy-discovery/.env (no secret
#     duplication). POLYGON_API_KEY loads from this repo's .env via config.load_dotenv().
#   - sentiment_analyzer.py ALWAYS writes a report — a NEUTRAL fallback on any
#     failure — so a bad news/API day never leaves the bot without a valid one.
#     It exits 1 (not 0) when that fallback was used, and this wrapper propagates
#     the code, so the timer shows FAILED. Writing the report is the fail-safe;
#     the exit code is the alarm. Before 2026-08-03 it exited 0 either way, and a
#     week of dead-overlay runs looked green in `systemctl list-timers`.
#
set -uo pipefail

REPO="/root/trading-bot"
cd "$REPO"

LOCK="$REPO/sentiment.lock"
exec 9>"$LOCK" || { echo "$(date -Is) sentiment: cannot open lock $LOCK"; exit 1; }
if ! flock -n 9; then
    echo "$(date -Is) sentiment: another run holds the lock — skipping."
    exit 0
fi

# Anthropic key from the sibling repo's env (Polygon key loads from ./.env in config).
ANTHROPIC_ENV="$REPO/../strategy-discovery/.env"
if [ -f "$ANTHROPIC_ENV" ]; then
    key="$(grep -m1 '^ANTHROPIC_API_KEY=' "$ANTHROPIC_ENV" | cut -d= -f2-)"
    [ -n "$key" ] && export ANTHROPIC_API_KEY="$key"
fi

echo "===== $(date -Is) sentiment-analysis START ====="
"$REPO/.venv/bin/python" sentiment_analyzer.py
rc=$?
echo "===== $(date -Is) sentiment-analysis END (exit=$rc) ====="
exit "$rc"
