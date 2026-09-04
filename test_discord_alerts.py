"""Tests for the Discord CRITICAL push channel.

The interesting cases are not "does it post" — they are the three contract
points that make it an alert channel rather than a fire-and-forget log tail:
a failed push must not lose content, two bots must not double-post, and nothing
in here may raise into the trading loop.
"""
import json
import os

import pytest

import config
import discord_alerts


class _Resp:
    def __init__(self, status_code=204, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def sinks(tmp_path, monkeypatch):
    """Point both sinks and the watermark at a fresh tmpdir."""
    equities = tmp_path / "critical_alerts.log"
    futures = tmp_path / "futures_critical_alerts.log"
    equities.write_text("")
    futures.write_text("")
    monkeypatch.setattr(config, "CRITICAL_ALERT_FILE", str(equities))
    monkeypatch.setattr(config, "CRITICAL_ALERT_SINKS",
                        ("critical_alerts.log", "futures_critical_alerts.log"))
    monkeypatch.setattr(config, "DISCORD_WATERMARK_FILE",
                        str(tmp_path / "alert_watermarks.json"))
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.test/hook")
    return equities, futures


@pytest.fixture
def posted(monkeypatch):
    """Capture every requests.post the module makes."""
    calls = []

    def _fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _Resp()

    monkeypatch.setattr(discord_alerts.requests, "post", _fake_post)
    return calls


# ── 1. disabled by default ────────────────────────────────────────────────────

def test_no_call_when_url_empty(sinks, posted, monkeypatch):
    equities, _ = sinks
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    equities.write_text("[CRITICAL] exit rejected\n")

    discord_alerts.check_critical_alerts()

    assert posted == [], "empty webhook URL must be a hard off-switch"


def test_empty_url_writes_no_watermark(sinks, posted, monkeypatch):
    """Disabled means inert, not 'silently consume the backlog'.

    If the disabled path advanced the watermark, enabling the webhook later
    would skip every alert already on disk.
    """
    equities, _ = sinks
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    equities.write_text("[CRITICAL] happened while disabled\n")

    discord_alerts.check_critical_alerts()
    assert not os.path.exists(config.DISCORD_WATERMARK_FILE)

    # Now enable: the pre-existing line must still be delivered.
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.test/hook")
    discord_alerts.check_critical_alerts()
    assert len(posted) == 1
    assert "happened while disabled" in posted[0]["json"]["content"]


# ── 2. unchanged file ─────────────────────────────────────────────────────────

def test_no_call_when_unchanged(sinks, posted):
    equities, _ = sinks
    equities.write_text("[CRITICAL] first\n")

    discord_alerts.check_critical_alerts()
    assert len(posted) == 1

    # Second and third checks see no growth.
    discord_alerts.check_critical_alerts()
    discord_alerts.check_critical_alerts()
    assert len(posted) == 1, "an unchanged file must not re-push"


def test_no_call_when_both_files_empty(sinks, posted):
    discord_alerts.check_critical_alerts()
    assert posted == []


def test_missing_file_is_not_an_error(sinks, posted):
    equities, futures = sinks
    os.remove(futures)
    equities.write_text("[CRITICAL] only equities exists\n")

    discord_alerts.check_critical_alerts()

    assert len(posted) == 1


# ── 3. file grows ─────────────────────────────────────────────────────────────

def test_push_when_file_grows(sinks, posted):
    equities, _ = sinks
    equities.write_text("[CRITICAL] first\n")
    discord_alerts.check_critical_alerts()
    assert len(posted) == 1

    with open(equities, "a") as fh:
        fh.write("[CRITICAL] second\n")
    discord_alerts.check_critical_alerts()

    assert len(posted) == 2
    body = posted[1]["json"]["content"]
    assert "second" in body
    assert "first" not in body, "only the NEW bytes may be pushed"


# ── 4. content shape ──────────────────────────────────────────────────────────

def test_content_shape(sinks, posted):
    equities, _ = sinks
    equities.write_text("[CRITICAL] exit rejected — position still open\n")

    discord_alerts.check_critical_alerts()

    body = posted[0]["json"]["content"]
    assert "TRADING BOT CRITICAL" in body
    assert "exit rejected" in body
    assert "critical_alerts.log" in body, "must name which sink it came from"
    assert body.count("```") == 2, "log text belongs in one code fence"
    assert posted[0]["url"] == "https://discord.test/hook"
    assert posted[0]["timeout"] == config.DISCORD_ALERT_TIMEOUT


def test_multibyte_at_watermark_boundary(sinks, posted):
    """An em dash straddling the offset must not raise.

    The CRITICAL messages in this repo contain em dashes and arrows. A text-mode
    handle seeked to an arbitrary byte offset can land mid-character; this is the
    regression guard for that.
    """
    equities, _ = sinks
    equities.write_bytes("[CRITICAL] first — dashed\n".encode())
    discord_alerts.check_critical_alerts()
    assert len(posted) == 1

    with open(equities, "ab") as fh:
        fh.write("[CRITICAL] second — also dashed → arrow\n".encode())
    discord_alerts.check_critical_alerts()

    assert len(posted) == 2
    assert "also dashed" in posted[1]["json"]["content"]


def test_long_content_is_chunked_not_dropped(sinks, posted):
    equities, _ = sinks
    equities.write_text("X" * 4000 + "\nTAILMARKER\n")

    discord_alerts.check_critical_alerts()

    assert len(posted) == 3, "4000 chars must span chunks, not truncate to one"
    joined = "".join(c["json"]["content"] for c in posted)
    assert "TAILMARKER" in joined, "the tail is the part you actually need"


def test_chunk_flood_is_capped_and_says_so(sinks, posted, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_ALERT_MAX_CHUNKS", 2)
    equities, _ = sinks
    equities.write_text("Y" * 20000)

    discord_alerts.check_critical_alerts()

    assert len(posted) == 2
    assert "suppressed" in posted[-1]["json"]["content"]


# ── 5. both files ─────────────────────────────────────────────────────────────

def test_both_files_checked(sinks, posted):
    equities, futures = sinks
    equities.write_text("[CRITICAL] equities problem\n")
    futures.write_text("[CRITICAL] futures problem\n")

    discord_alerts.check_critical_alerts()

    joined = "".join(c["json"]["content"] for c in posted)
    assert "equities problem" in joined
    assert "futures problem" in joined
    marks = json.loads(open(config.DISCORD_WATERMARK_FILE).read())
    assert len(marks) == 2, "both sinks must get a watermark"


def test_futures_sink_name_is_not_doubled(sinks):
    """Regression: the sink list must not be built by string-replacing
    CRITICAL_ALERT_FILE, which on the futures bot yields
    'futures_futures_critical_alerts.log'.
    """
    names = [os.path.basename(p) for p in discord_alerts._sink_paths()]
    assert names == ["critical_alerts.log", "futures_critical_alerts.log"]
    assert not any("futures_futures" in n for n in names)


# ── contract: a failed push must not lose the alert ───────────────────────────

def test_failed_push_does_not_advance_watermark(sinks, monkeypatch):
    equities, _ = sinks
    equities.write_text("[CRITICAL] must not be lost\n")

    attempts = []

    def _boom(url, json=None, timeout=None):
        attempts.append(json)
        raise OSError("connection refused")

    monkeypatch.setattr(discord_alerts.requests, "post", _boom)
    discord_alerts.check_critical_alerts()
    assert len(attempts) == 1

    # Webhook comes back; the SAME content must be delivered, not skipped.
    delivered = []

    def _ok(url, json=None, timeout=None):
        delivered.append(json)
        return _Resp()

    monkeypatch.setattr(discord_alerts.requests, "post", _ok)
    discord_alerts.check_critical_alerts()

    assert len(delivered) == 1
    assert "must not be lost" in delivered[0]["content"]


def test_http_error_is_treated_as_failure(sinks, monkeypatch):
    equities, _ = sinks
    equities.write_text("[CRITICAL] rejected by discord\n")

    monkeypatch.setattr(discord_alerts.requests, "post",
                        lambda url, json=None, timeout=None: _Resp(404, "no such webhook"))
    discord_alerts.check_critical_alerts()

    marks_path = config.DISCORD_WATERMARK_FILE
    marks = json.loads(open(marks_path).read()) if os.path.exists(marks_path) else {}
    assert marks.get(str(equities), 0) == 0, "a 404 must not consume the alert"


# ── contract: never raise into the trading loop ───────────────────────────────

def test_never_raises_into_the_loop(sinks, monkeypatch):
    equities, _ = sinks
    equities.write_text("[CRITICAL] boom\n")

    def _explode(*a, **k):
        raise RuntimeError("unexpected internal failure")

    monkeypatch.setattr(discord_alerts, "_run_check", _explode)
    discord_alerts.check_critical_alerts()  # must not propagate


def test_corrupt_watermark_recovers(sinks, posted):
    equities, _ = sinks
    with open(config.DISCORD_WATERMARK_FILE, "w") as fh:
        fh.write("{not json at all")
    equities.write_text("[CRITICAL] after corruption\n")

    discord_alerts.check_critical_alerts()

    assert len(posted) == 1, "a corrupt watermark must not wedge the channel"


# ── contract: no double-push across the two bots ──────────────────────────────

def test_shared_watermark_prevents_double_push(sinks, posted):
    """Both bots watch both sinks; the shared offset is what dedupes them.

    Two sequential checks stand in for the two processes — they read the same
    watermark file, which is the mechanism under test.
    """
    _, futures = sinks
    futures.write_text("[CRITICAL] one futures alert\n")

    discord_alerts.check_critical_alerts()   # equities bot's cycle
    discord_alerts.check_critical_alerts()   # futures bot's cycle

    assert len(posted) == 1, "one alert must produce exactly one message"


def test_truncation_resets_watermark(sinks, posted):
    equities, _ = sinks
    equities.write_text("[CRITICAL] before truncate\n")
    discord_alerts.check_critical_alerts()
    assert len(posted) == 1

    # `> critical_alerts.log` — the documented cleanup step.
    equities.write_text("")
    discord_alerts.check_critical_alerts()
    assert len(posted) == 1, "truncating to empty pushes nothing"

    equities.write_text("[CRITICAL] after truncate\n")
    discord_alerts.check_critical_alerts()
    assert len(posted) == 2, "a shrunken file must not strand later alerts"
    assert "after truncate" in posted[1]["json"]["content"]
