#!/usr/bin/env bash
#
# Restart a trading bot.
#
#   ./restart.sh            # equities mode (default)
#   ./restart.sh futures    # futures mode
#
# The bots are now managed by systemd (unit files in deploy/, installed to
# /etc/systemd/system/). This wrapper preserves the old `./restart.sh [mode]`
# muscle-memory by delegating to `systemctl restart`, which stops the running
# instance and starts a fresh one under the same supervision (auto-restart on
# crash, start on boot). Do NOT launch main.py directly anymore — a second
# instance just dies on the singleton lock the systemd one already holds.
#
set -uo pipefail

MODE="${1:-equities}"
case "$MODE" in
    equities) SERVICE="trading-bot-equities"; LOG_FILE="bot.log" ;;
    futures)  SERVICE="trading-bot-futures";  LOG_FILE="bot.futures.log" ;;
    *) echo "Usage: $0 [equities|futures]" >&2; exit 1 ;;
esac

echo "Restarting $SERVICE via systemd..."
systemctl restart "$SERVICE"

# Give it a moment to acquire the lock and clear the startup gates.
sleep 3
if systemctl is-active --quiet "$SERVICE"; then
    echo "$SERVICE is active."
    systemctl --no-pager --lines=0 status "$SERVICE" | grep -E "Active:|Main PID:" || true
else
    echo "ERROR: $SERVICE failed to start. Recent unit journal + log:" >&2
    journalctl -u "$SERVICE" -n 10 --no-pager >&2 || true
    tail -n 20 "$(dirname "$0")/$LOG_FILE" >&2 || true
    exit 1
fi
