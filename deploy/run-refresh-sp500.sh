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
#   - On a successful refresh, commits data/sp500.json — and ONLY that file —
#     if it changed, then pushes. Git history is the only record of which
#     universe a given Monday rotation screened, and before this the timer
#     rewrote the file monthly without ever committing it (10-01 sat dirty).
#     `git commit --only -- data/sp500.json` commits that path alone even when
#     other changes are staged; they stay staged, untouched.
#   - Refuses to push anything but its own commit: if local main already holds
#     other unpushed commits, it commits, does NOT push, and pages — a timer
#     must not publish someone's unreviewed work.
#   - Any git failure pages via lib-critical-alert.sh and exits non-zero. The
#     refreshed file is already on disk either way, so the screen is unaffected.
#
# Source is GitHub (constituents.csv), NOT Polygon — so this does not spend any
# of the shared 5-calls/min free-tier budget.
#
# Overrides (for testing on a throwaway clone without paging anyone):
#   REPO, ALERT_FILE, SKIP_DISCORD=1
#
set -uo pipefail

. "$(dirname "$0")/lib-critical-alert.sh"

REPO="${REPO:-/root/trading-bot}"
cd "$REPO"

LOCK="$REPO/momentum.lock"
UNIVERSE="data/sp500.json"
GIT_EMAIL="236492174+handiman876-create@users.noreply.github.com"
GIT_NAME="handiman876-create"

log() { echo "$(date -Is) sp500-refresh: $*"; }

fail() {
    page_critical sp500-refresh "SP500 UNIVERSE COMMIT/PUSH FAILED — $1. $REPO/$UNIVERSE is refreshed on disk (the screen uses it) but NOT recorded on GitHub; see logs/momentum.log"
    echo "===== $(date -Is) sp500-refresh END (exit=1) ====="
    exit 1
}

# Non-blocking lock: if the screen (or a manual refresh) is running, skip
# cleanly. Exit 0 here is correct — nothing is stale, we simply deferred.
exec 9>"$LOCK" || { echo "$(date -Is) sp500-refresh: cannot open lock $LOCK"; exit 1; }
if ! flock -n 9; then
    echo "$(date -Is) sp500-refresh: another run holds the lock — skipping this cycle."
    exit 0
fi

echo "===== $(date -Is) sp500-refresh START ====="
"$REPO/.venv/bin/python" refresh_sp500.py
rc=$?
if [[ $rc -ne 0 ]]; then
    echo "===== $(date -Is) sp500-refresh END (exit=$rc) ====="
    exit "$rc"
fi

# ── Record the universe in git ───────────────────────────────────────────────
branch=$(git symbolic-ref --short HEAD 2>/dev/null) || fail "HEAD is detached"
[[ "$branch" == "main" ]] || fail "checked out on '$branch', not main — not committing there"

if git diff --quiet HEAD -- "$UNIVERSE"; then
    log "$UNIVERSE unchanged vs HEAD — nothing to commit"
    echo "===== $(date -Is) sp500-refresh END (exit=0) ====="
    exit 0
fi

# Subject + body built from the committed vs refreshed file, in the 09-03 style:
#   "S&P 500 universe refresh YYYY-MM-DD: +A +B / -C -D"
msg=$(git show "HEAD:$UNIVERSE" 2>/dev/null | "$REPO/.venv/bin/python" -c '
import json, sys
try:
    old = json.load(sys.stdin)
except ValueError:
    old = {}
new = json.load(open(sys.argv[1]))
o, n = set(old.get("symbols", [])), set(new.get("symbols", []))
added, removed = sorted(n - o), sorted(o - n)
os_, ns = old.get("sectors", {}), new.get("sectors", {})
moved = [(s, os_[s], ns[s]) for s in sorted(o & n)
         if s in os_ and s in ns and os_[s] != ns[s]]
diff = " ".join("+" + s for s in added)
diff += (" / " if added and removed else "") + " ".join("-" + s for s in removed)
print("S&P 500 universe refresh %s: %s" % (new.get("as_of", "?"),
      diff or "no constituent changes"))
print()
print("Written by sp500-refresh.timer (%d constituents); committed by"
      " run-refresh-sp500.sh. as_of %s -> %s." % (len(n), old.get("as_of", "?"),
      new.get("as_of", "?")))
for s, a, b in moved:
    pa = "%s / %s" % (a.get("sector"), a.get("sub_industry"))
    pb = "%s / %s" % (b.get("sector"), b.get("sub_industry"))
    print("Reclassified %s: %s -> %s" % (s, pa, pb))
' "$UNIVERSE") || fail "could not build the commit message from $UNIVERSE"

git -c user.email="$GIT_EMAIL" -c user.name="$GIT_NAME" \
    commit -q --only -m "$msg" -- "$UNIVERSE" || fail "git commit exited $?"
log "committed $(git log -1 --format='%h %s')"
git show --stat --format= HEAD

# Push only our own commit. Refresh origin/main first so "unpushed" is judged
# against GitHub, not a stale local ref.
git fetch -q origin main || fail "git fetch origin main exited $?"
extra=$(git rev-list --count origin/main..HEAD~1 2>/dev/null) || fail "cannot compare HEAD with origin/main"
[[ "$extra" == "0" ]] \
    || fail "local main has $extra other unpushed commit(s) besides the refresh — not pushing them on your behalf"

push_out=$(git push origin main 2>&1)
rc=$?
echo "$push_out"
push_err=$(echo "$push_out" | grep -m1 -E '^(fatal|error|remote|hint): |rejected' || echo "$push_out" | tail -1)
push_err="${push_err%"${push_err##*[![:space:]]}"}"   # strip trailing padding git adds to remote: lines
[[ $rc -eq 0 ]] || fail "git push origin main exited $rc ($push_err)"

local_head=$(git rev-parse HEAD)
remote_head=$(git ls-remote origin refs/heads/main | cut -f1)
[[ "$local_head" == "$remote_head" ]] \
    || fail "push reported success but origin/main is ${remote_head:-unreadable}, local is $local_head"

log "pushed — origin/main == local HEAD ${local_head:0:7}"
echo "===== $(date -Is) sp500-refresh END (exit=0) ====="
exit 0
