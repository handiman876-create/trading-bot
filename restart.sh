#!/usr/bin/env bash
#
# Safely restart the trading bot.
#
# Kills the previous instance by the PID recorded in bot.pid (never `pkill -f
# main.py` — that pattern also matches the calling shell's own command line and
# kills the restart itself). Then launches a fresh instance under nohup and
# records its PID.
#
set -euo pipefail

cd "$(dirname "$0")"

PID_FILE="bot.pid"
LOG_FILE="bot.log"

# ── 1 & 2. Read the old PID and kill it safely ───────────────────────────────
if [[ -f "$PID_FILE" ]]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
else
    OLD_PID=""
fi

if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Stopping old bot (PID $OLD_PID)..."
    kill -TERM "$OLD_PID" 2>/dev/null || true

    # ── 3. Wait up to 10s for a graceful exit, then SIGKILL ──────────────────
    for _ in $(seq 1 10); do
        kill -0 "$OLD_PID" 2>/dev/null || break
        sleep 1
    done

    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "  Did not stop gracefully — sending SIGKILL."
        kill -9 "$OLD_PID" 2>/dev/null || true
        for _ in $(seq 1 5); do
            kill -0 "$OLD_PID" 2>/dev/null || break
            sleep 1
        done
    fi

    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "ERROR: PID $OLD_PID is still alive (possibly stuck in D state). Aborting." >&2
        exit 1
    fi
    echo "  Old bot stopped."
else
    echo "No running bot found in $PID_FILE — starting fresh."
fi

# ── 4 & 5. Start a fresh instance and record its PID ─────────────────────────
echo "Starting bot..."
nohup python3 main.py > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

# ── 6. Confirm the new process is actually running ───────────────────────────
sleep 3
if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "Bot started successfully (PID $NEW_PID)."
    ps -p "$NEW_PID" -o pid,etime,cmd
else
    echo "ERROR: bot exited immediately after launch. Recent log:" >&2
    tail -n 20 "$LOG_FILE" >&2
    exit 1
fi
