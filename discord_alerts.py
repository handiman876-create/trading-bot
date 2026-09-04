"""Push new CRITICAL log lines to a Discord webhook.

`critical_alerts.log` and its futures twin are durable but silent sinks — they
guarantee the record survives, they do not tell anyone. This is the alert
channel docs/backlog.md gates going live on.

Contract this module holds to, in priority order:

1. **Never break a trading cycle.** Every failure path is caught and logged.
   A dead webhook, a DNS failure, a malformed log byte — none of them may
   propagate into `_run_cycle`.
2. **Never silently drop an alert.** The watermark advances only after Discord
   accepts the message, so a failed push is retried next cycle rather than lost.
   That is the opposite of the usual "advance then send" shape and it is the
   whole reason this file is not four lines long.
3. **Never double-push.** Both bots watch both sinks (their sessions differ, so
   cross-watching is the coverage), which means the offset has to be shared
   state, not per-process.
4. **Be visible when it is doing nothing.** Counters, because a safety net you
   cannot see firing is one you cannot later argue for keeping.
"""
import fcntl
import json
import logging
import os

import requests

import config

logger = logging.getLogger("bot")

# Observability. Per-process and reset on restart, like every other counter in
# this repo, so the durable record is the log line, not the number.
_pushes = 0          # messages Discord accepted
_failures = 0        # push attempts that did not land (content NOT dropped)
_truncations = 0     # cycles where a file grew past MAX_CHUNKS worth of text


def _watermark_path() -> str:
    return config.DISCORD_WATERMARK_FILE


def _sink_paths() -> list[str]:
    """Absolute-ish paths of both CRITICAL sinks.

    Derived from CRITICAL_ALERT_FILE's directory so a conftest redirect into a
    tmpdir carries over to both sinks — otherwise a test that writes a fixture
    alert would append to the real repo-root file.
    """
    base_dir = os.path.dirname(config.CRITICAL_ALERT_FILE)
    return [os.path.join(base_dir, name) if base_dir else name
            for name in config.CRITICAL_ALERT_SINKS]


def _load_marks(handle) -> dict:
    """Read the watermark map from an already-locked handle."""
    handle.seek(0)
    raw = handle.read()
    if not raw.strip():
        return {}
    try:
        marks = json.loads(raw)
        return marks if isinstance(marks, dict) else {}
    except json.JSONDecodeError:
        # A corrupt watermark file must not wedge the channel. Reset to empty:
        # worst case is one duplicate batch, which is strictly better than an
        # alert channel that stays down until someone deletes a file.
        logger.warning("Discord alerts: watermark file unreadable, resetting")
        return {}


def _store_marks(handle, marks: dict) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(marks, handle)
    handle.flush()
    os.fsync(handle.fileno())


def _read_new_bytes(path: str, offset: int) -> tuple[str, int]:
    """Return (text, new_offset) for everything after `offset`.

    Opened in BINARY and decoded with errors="replace" on purpose. The offset is
    a byte count from os.path.getsize, and these log lines contain multi-byte
    characters (the banner and several CRITICAL messages use em dashes and
    arrows). Seeking a text-mode handle to an arbitrary byte offset can land
    mid-character and raise UnicodeDecodeError — which, before the try/except
    below existed, would have surfaced as a traceback in the trading loop.
    """
    size = os.path.getsize(path)
    if size < offset:
        # Truncated or rotated out from under us — start over from the top
        # rather than seeking past EOF and reporting nothing forever.
        logger.info("Discord alerts: %s shrank (%d -> %d bytes), watermark reset",
                    path, offset, size)
        offset = 0
    if size == offset:
        return "", offset
    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    return chunk.decode("utf-8", errors="replace"), offset + len(chunk)


def _post(text: str, source: str) -> bool:
    """Send one batch to Discord, in chunks. True only if ALL chunks landed.

    Discord's content limit is 2000 characters. The naive shape is `text[:1900]`,
    which drops the tail of exactly the multi-line traceback you wanted to see,
    so this splits instead and says so when it still has to cut.
    """
    global _pushes, _failures, _truncations

    body = text.strip()
    if not body:
        return True

    limit = 1900
    chunks = [body[i:i + limit] for i in range(0, len(body), limit)]
    if len(chunks) > config.DISCORD_ALERT_MAX_CHUNKS:
        dropped = len(chunks) - config.DISCORD_ALERT_MAX_CHUNKS
        chunks = chunks[:config.DISCORD_ALERT_MAX_CHUNKS]
        chunks[-1] += f"\n… +{dropped} more chunk(s) suppressed; see {source}"
        _truncations += 1

    for idx, chunk in enumerate(chunks, 1):
        tag = f" ({idx}/{len(chunks)})" if len(chunks) > 1 else ""
        payload = {"content": f"🚨 **TRADING BOT CRITICAL**{tag}  `{source}`\n"
                              f"```\n{chunk}\n```"}
        try:
            resp = requests.post(config.DISCORD_WEBHOOK_URL, json=payload,
                                 timeout=config.DISCORD_ALERT_TIMEOUT)
        except Exception as exc:            # network, DNS, timeout, anything
            _failures += 1
            logger.error("Discord alerts: push FAILED for %s (%s) — content kept, "
                         "will retry next cycle — failures #%d",
                         source, exc, _failures)
            return False
        if resp.status_code >= 300:
            _failures += 1
            logger.error("Discord alerts: push REJECTED for %s (HTTP %s: %s) — "
                         "content kept, will retry next cycle — failures #%d",
                         source, resp.status_code, resp.text[:200], _failures)
            return False
        _pushes += 1

    logger.info("Discord alerts: pushed %d chunk(s) from %s — pushes #%d",
                len(chunks), source, _pushes)
    return True


def check_critical_alerts() -> None:
    """Push any new CRITICAL content from both sinks. Safe to call every cycle.

    Cheap when idle: one os.path.getsize per sink plus one small locked read, no
    network call unless a file actually grew.
    """
    if not config.DISCORD_WEBHOOK_URL:
        return

    try:
        _run_check()
    except Exception as exc:
        # Point 1 of the module contract. Nothing this module does is worth
        # interrupting trading for.
        logger.exception("Discord alerts: check failed, skipping this cycle (%s)", exc)


def _run_check() -> None:
    path = _watermark_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # One lock over read-modify-write. Both bots poll on the same 60s cadence, so
    # without this they can both read offset N, both push, and both write N+k —
    # the duplicate the shared watermark exists to prevent.
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            marks = _load_marks(handle)
            changed = False

            for sink in _sink_paths():
                try:
                    text, new_offset = _read_new_bytes(sink, int(marks.get(sink, 0)))
                except FileNotFoundError:
                    continue
                if not text:
                    if marks.get(sink) != new_offset:
                        marks[sink] = new_offset
                        changed = True
                    continue
                # Watermark advances ONLY on a confirmed delivery (contract 2).
                if _post(text, os.path.basename(sink)):
                    marks[sink] = new_offset
                    changed = True

            if changed:
                _store_marks(handle, marks)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def banner() -> str:
    """The `Alert push :` startup banner value."""
    if not config.DISCORD_WEBHOOK_URL:
        return ("DISABLED — CRITICAL events reach critical_alerts.log and nothing "
                "else; set DISCORD_WEBHOOK_URL in .env to enable. This is the "
                "docs/backlog.md pre-live alerting gate and it is OPEN.")
    return ("ENABLED — Discord webhook, both sinks (%s) checked every cycle, "
            "shared watermark %s. Watermark advances only on a confirmed 2xx, so "
            "a failed push retries rather than dropping. Counters: Discord alerts "
            "pushes/failures #N (per-process)."
            % (", ".join(config.CRITICAL_ALERT_SINKS), config.DISCORD_WATERMARK_FILE))
