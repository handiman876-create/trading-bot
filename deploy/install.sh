#!/usr/bin/env bash
#
# Install the systemd units + logrotate config for the trading bots.
# Idempotent: safe to re-run after editing any file in this directory.
#
#   sudo ./deploy/install.sh
#
set -euo pipefail
cd "$(dirname "$0")"

echo "Installing systemd unit files..."
install -m 0644 trading-bot-equities.service /etc/systemd/system/trading-bot-equities.service
install -m 0644 trading-bot-futures.service  /etc/systemd/system/trading-bot-futures.service
install -m 0644 momentum-rotation.service    /etc/systemd/system/momentum-rotation.service
install -m 0644 momentum-rotation.timer      /etc/systemd/system/momentum-rotation.timer

echo "Installing logrotate config..."
install -m 0644 logrotate-trading-bot /etc/logrotate.d/trading-bot

echo "Reloading systemd and enabling services on boot..."
systemctl daemon-reload
systemctl enable trading-bot-equities.service trading-bot-futures.service

echo
echo "Done. Start (or restart) the bots with:"
echo "  systemctl start trading-bot-equities trading-bot-futures   # first time"
echo "  ./restart.sh equities   /   ./restart.sh futures           # thereafter"
echo
echo "Momentum rotation (twice-monthly watchlist screen) is installed but NOT"
echo "enabled. Add POLYGON_API_KEY to .env, then turn it on with:"
echo "  systemctl enable --now momentum-rotation.timer            # schedule 1st & 15th"
echo "  systemctl start momentum-rotation.service                 # run once now (optional)"
