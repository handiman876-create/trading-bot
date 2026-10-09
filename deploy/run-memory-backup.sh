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
#   - Any failure pages via lib-critical-alert.sh (CRITICAL line + immediate
#     Discord flush — see that file for why the flush matters), then exits
#     non-zero.
#   - Stray-memory check: Claude Code keeps one memory dir per launch directory
#     (~/.claude/projects/<slug>/memory). Any that isn't MEMORY_DIR or a symlink
#     to it is invisible to this backup, so each one pages (with its path). The
#     backup still runs, but the job then exits 1 so the timer goes red.
#   - GitHub PAT expiry: pages every night from PAT_WARN_DAYS before
#     GITHUB_PAT_EXPIRES (YYYY-MM-DD in .env). On 2026-10-08 the token died with
#     no warning, and git's store helper erased the rejected token. A missing
#     or invalid date pages too, or the check would switch itself off without
#     anyone noticing. Same as strays: the backup still runs, then exit 1.
#
# Overrides (for testing the failure path without paging anyone):
#   MEMORY_DIR, PROJECTS_DIR, ALERT_FILE, SKIP_DISCORD=1, ENV_FILE, TODAY
#
set -uo pipefail

. "$(dirname "$0")/lib-critical-alert.sh"

REPO="/root/trading-bot"
MEMORY_DIR="${MEMORY_DIR:-/root/.claude/projects/-root/memory}"
PROJECTS_DIR="${PROJECTS_DIR:-/root/.claude/projects}"
GIT_EMAIL="236492174+handiman876-create@users.noreply.github.com"
GIT_NAME="handiman876-create"
LOCK="$REPO/memory-backup.lock"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
TODAY="${TODAY:-$(date -u +%F)}"
PAT_WARN_DAYS=7

log() { echo "$(date -Is) memory-backup: $*"; }

fail() {
    page_critical memory-backup "MEMORY BACKUP FAILED — $1. Memory repo $MEMORY_DIR is NOT backed up to GitHub; see logs/memory-backup.log"
    log "END (exit=1)"
    exit 1
}

exec 9>"$LOCK" || { log "cannot open lock $LOCK"; exit 1; }
if ! flock -n 9; then
    log "another run holds the lock — skipping this cycle."
    exit 0
fi

log "START"

# Before the backup, so a failed push can't skip it; exit code applied at the end.
strays=0
canonical=$(readlink -f "$MEMORY_DIR")
for d in "$PROJECTS_DIR"/*/memory; do
    [[ -e "$d" || -L "$d" ]] || continue          # unmatched glob
    [[ "$(readlink -f "$d")" == "$canonical" ]] && continue
    strays=$((strays + 1))
    page_critical memory-backup "STRAY MEMORY DIR — $d is not $MEMORY_DIR or a symlink to it; memories written there are NOT backed up to GitHub. Merge its files into the repo, then replace it with a symlink"
done
log "stray memory dirs: $strays"

# Also before the push: an expired token makes the push fail(), and the
# expiry page then says why. Only the date is read from .env, never a secret.
pat_paged=0
pat_exp=$(grep -m1 -E '^GITHUB_PAT_EXPIRES=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d "\"' \r")
# GNU date rejects impossible dates (2026-02-30); the round-trip catches anything it normalises.
if [[ ! "$pat_exp" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
        || ! exp_s=$(date -u -d "$pat_exp" +%s 2>/dev/null) \
        || [[ "$(date -u -d "$pat_exp" +%F)" != "$pat_exp" ]]; then
    pat_paged=1
    page_critical memory-backup "GITHUB TOKEN EXPIRY UNKNOWN — GITHUB_PAT_EXPIRES in $ENV_FILE is ${pat_exp:-missing} (want YYYY-MM-DD). The expiry warning is OFF until it is set"
else
    days_left=$(( (exp_s - $(date -u -d "$TODAY" +%s)) / 86400 ))
    if (( days_left < 0 )); then
        pat_paged=1
        page_critical memory-backup "GITHUB TOKEN EXPIRED $(( -days_left )) day(s) ago ($pat_exp). Pushes from every repo will fail. Create a new PAT, store it, and update GITHUB_PAT_EXPIRES in $ENV_FILE"
    elif (( days_left <= PAT_WARN_DAYS )); then
        pat_paged=1
        page_critical memory-backup "GITHUB TOKEN EXPIRES in $days_left day(s) on $pat_exp. Create a new PAT, store it, and update GITHUB_PAT_EXPIRES in $ENV_FILE"
    fi
    log "github token expires $pat_exp ($days_left day(s) left, pages at <= $PAT_WARN_DAYS)"
fi

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
push_err="${push_err%"${push_err##*[![:space:]]}"}"   # strip trailing padding git adds to remote: lines
[[ $rc -eq 0 ]] || fail "git push origin main exited $rc ($push_err)"

local_head=$(git rev-parse HEAD)
remote_head=$(git ls-remote origin refs/heads/main | cut -f1)
[[ "$local_head" == "$remote_head" ]] \
    || fail "push reported success but origin/main is ${remote_head:-unreadable}, local is $local_head"

log "pushed — origin/main == local HEAD ${local_head:0:7}"
if (( strays > 0 || pat_paged )); then
    log "END (exit=1) — backup OK but paged: $strays stray memory dir(s), github token check=$pat_paged"
    exit 1
fi
log "END (exit=0)"
exit 0
