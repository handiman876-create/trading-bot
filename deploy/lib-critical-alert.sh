# Shared by the deploy/run-*.sh wrappers that must PAGE on failure. Source it:
#
#   . "$(dirname "$0")/lib-critical-alert.sh"
#   page_critical <source-name> <message>
#
# Appends one CRITICAL line to critical_alerts.log in the bots' own format
# ("<asctime> [CRITICAL] <name>: <msg>") and then flushes it to Discord at once.
# The flush is the point: the bots only push alerts from inside their
# market-hours poll loops, so an alert written by an off-hours timer would
# otherwise sit unread until the next session (Sunday 18:00 ET from a Saturday).
# It goes through discord_alerts.check_critical_alerts(), whose shared, flock'd
# watermark means the next bot cycle cannot re-send it.
#
# Overrides (for testing the failure path without paging anyone):
#   ALERT_FILE, SKIP_DISCORD=1
#
# Returns 0; the caller decides the exit code (and must exit non-zero — a
# timer that pages but stays green is the fail-safe-is-not-exit-0 trap).

ALERT_REPO="/root/trading-bot"
ALERT_FILE="${ALERT_FILE:-$ALERT_REPO/critical_alerts.log}"

page_critical() {
    local name="$1" msg="$2"
    echo "$(date -Is) $name: CRITICAL: $msg"
    echo "$(date '+%Y-%m-%d %H:%M:%S,000') [CRITICAL] $name: $msg" >> "$ALERT_FILE"
    if [[ "${SKIP_DISCORD:-0}" == "1" ]]; then
        echo "$(date -Is) $name: SKIP_DISCORD=1 — alert written to $ALERT_FILE, not flushed"
        return 0
    fi
    if (cd "$ALERT_REPO" && "$ALERT_REPO/.venv/bin/python" -c \
            "import discord_alerts; discord_alerts.check_critical_alerts()"); then
        echo "$(date -Is) $name: CRITICAL flushed to Discord (or no webhook configured)"
    else
        echo "$(date -Is) $name: Discord flush errored — alert stays in $ALERT_FILE for the next bot cycle"
    fi
    return 0
}
