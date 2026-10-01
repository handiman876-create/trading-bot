#!/usr/bin/env bash
#
# Nightly memory-repo backup (invoked by memory-backup.service).
#
#   - flock so a scheduled run can never overlap a manual one.
#   - git add -A; commit ONLY if something is staged ("nightly memory backup
#     YYYY-MM-DD"), always as the noreply address — GitHub's email-privacy
#     setting rejects pushes carrying the real one.
#   - git push origin main, every night, so a commit left behind by a failed
#     night is retried even when nothing new changed.
#   - Any failure appends a CRITICAL line to critical_alerts.log AND flushes it
#     to Discord immediately, then exits non-zero. The flush matters: the bots
#     only push alerts from inside their market-hours poll loops, so without it
#     a 02:17 failure on a Saturday would sit unread until Sunday 18:00 ET.
#     The flush goes through discord_alerts' shared, flock'd watermark, so the
#     next bot cycle cannot re-send it.
#
# Overrides (for testing the failure path without paging anyone):
#   MEMORY_DIR, ALERT_FILE, SKIP_DISCORD=1
#
set -uo pipefail

REPO="/root/trading-bot"
MEMORY_DIR="${MEMORY_DIR:-/root/.claude/projects/-root/memory}"
ALERT_FILE="${ALERT_FILE:-$REPO/critical_alerts.log}"
GIT_EMAIL="236492174+handiman876-create@users.noreply.github.com"
GIT_NAME="handiman876-create"
LOCK="$REPO/memory-backup.lock"

log() { echo "$(date -Is) memory-backup: $*"; }

fail() {
    local msg="MEMORY BACKUP FAILED — $1. Memory repo $MEMORY_DIR is NOT backed up to GitHub; see logs/memory-backup.log"
    log "CRITICAL: $msg"
    # Same shape as the bots' CRITICAL lines: "<asctime> [CRITICAL] <name>: <msg>"
    echo "$(date '+%Y-%m-%d %H:%M:%S,000') [CRITICAL] memory-backup: $msg" >> "$ALERT_FILE"
    if [[ "${SKIP_DISCORD:-0}" != "1" ]]; then
        if (cd "$REPO" && "$REPO/.venv/bin/python" -c \
                "import discord_alerts; discord_alerts.check_critical_alerts()"); then
            log "CRITICAL flushed to Discord (or no webhook configured)"
        else
            log "Discord flush errored — alert stays in $ALERT_FILE for the next bot cycle"
        fi
    fi
    log "END (exit=1)"
    exit 1
}

exec 9>"$LOCK" || { log "cannot open lock $LOCK"; exit 1; }
if ! flock -n 9; then
    log "another run holds the lock — skipping this cycle."
    exit 0
fi

log "START"
cd "$MEMORY_DIR" || fail "cannot cd to $MEMORY_DIR"

git add -A || fail "git add -A exited $?"

if git diff --cached --quiet; then
    log "no changes — nothing to commit"
else
    log "staged: $(git diff --cached --shortstat)"
    git -c user.email="$GIT_EMAIL" -c user.name="$GIT_NAME" \
        commit -q -m "nightly memory backup $(date +%F)" \
        || fail "git commit exited $?"
    log "committed $(git log -1 --format='%h %s')"
fi

push_out=$(git push origin main 2>&1)
rc=$?
echo "$push_out"
# Quote git's first fatal/error line — the last line is boilerplate advice
# ("...and the repository exists.") that says nothing in a page.
push_err=$(echo "$push_out" | grep -m1 -E '^(fatal|error|remote): ' || echo "$push_out" | tail -1)
[[ $rc -eq 0 ]] || fail "git push origin main exited $rc ($push_err)"

local_head=$(git rev-parse HEAD)
remote_head=$(git ls-remote origin refs/heads/main | cut -f1)
[[ "$local_head" == "$remote_head" ]] \
    || fail "push reported success but origin/main is ${remote_head:-unreadable}, local is $local_head"

log "pushed — origin/main == local HEAD ${local_head:0:7}"
log "END (exit=0)"
exit 0
