"""
Tests for three fixes landed together on 2026-08-17.

1. expected_cancel — a cancel WE asked for must not log at ERROR.
2. option exit attribution — the premium rule reaches the ledger note, and
   _exit_reason buckets it instead of filing it as "signal".
3. CRITICAL alert sink — a CRITICAL-only FileHandler on a path logrotate does
   not glob, and the test suite must never be able to write to the real one.

WHY THESE ARE ONE FILE: all three exist to make the ERROR/CRITICAL channel
trustworthy. A false ERROR on routine success (1), a mislabelled exit (2), and a
sink the suite can pollute (3) are the same failure in three places — the log
says something that is not true, and a human calibrates on it.
"""

import logging
import os
import tempfile

import config
import performance_analyzer as pa
import strategy
import tradestation_client as tc


# ── 1. expected_cancel ────────────────────────────────────────────────────────

def _order(status_desc, code="", reject=None):
    o = {"StatusDescription": status_desc, "Status": code}
    if reject:
        o["RejectReason"] = reject
    return o


def test_urout_is_a_cancellation():
    """The statuses a cancel REQUEST produces."""
    assert tc._order_was_cancelled(_order("UROut"))
    assert tc._order_was_cancelled(_order("Canceled"))
    assert tc._order_was_cancelled(_order("", "UROUT"))
    assert tc._order_was_cancelled(_order("", "CAN"))


def test_rejection_and_expiry_are_NOT_cancellations():
    """The negative case that makes expected_cancel safe.

    Both are terminal-without-fill, so _order_failed is true for them — but
    nobody ASKED for either, so expecting a cancel must not quiet them. If this
    ever passes for a rejection, a refused exit goes silent.
    """
    assert not tc._order_was_cancelled(_order("Rejected"))
    assert not tc._order_was_cancelled(_order("Expired"))
    assert not tc._order_was_cancelled(_order("", "REJ"))
    assert not tc._order_was_cancelled(_order("", "EXP"))
    # ...while still being "failed", which is what makes the distinction matter.
    assert tc._order_failed(_order("Rejected"))
    assert tc._order_failed(_order("Expired"))


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def levels_for(self, needle):
        return [r.levelno for r in self.records if needle in r.getMessage()]


def _outcome_with(monkeypatch, order, **kwargs):
    """Drive get_order_outcome against a stubbed single-order response."""
    monkeypatch.setattr(tc, "_get", lambda path: {"Orders": [order]})
    cap = _Capture()
    tc.logger.addHandler(cap)
    # pytest's logging plugin forces the root level, so logger.info() would be
    # dropped by isEnabledFor BEFORE reaching the handler. Pin it locally — the
    # whole point of these tests is to assert on an INFO record.
    saved = tc.logger.level
    tc.logger.setLevel(logging.DEBUG)
    try:
        out = tc.get_order_outcome("ACCT", "OID", **kwargs)
    finally:
        tc.logger.setLevel(saved)
        tc.logger.removeHandler(cap)
    return out, cap


def test_expected_cancel_logs_info_not_error(monkeypatch):
    out, cap = _outcome_with(monkeypatch, _order("UROut"), expected_cancel=True)
    assert out["state"] == "dead", "state must be unchanged — severity only"
    assert cap.levels_for("cancel CONFIRMED") == [logging.INFO]
    assert not cap.levels_for("DEAD"), "the false ERROR must be gone"


def test_unexpected_cancel_still_errors(monkeypatch):
    """A cancel nobody asked for (broker-initiated) stays loud."""
    out, cap = _outcome_with(monkeypatch, _order("UROut"))
    assert out["state"] == "dead"
    assert cap.levels_for("DEAD") == [logging.ERROR]


def test_rejection_errors_even_when_a_cancel_was_expected(monkeypatch):
    """The case that would hide a refused exit. expected_cancel must not
    downgrade a REJECTION."""
    out, cap = _outcome_with(monkeypatch, _order("Rejected", reject="no shares"),
                             expected_cancel=True)
    assert out["state"] == "dead"
    assert out["reason"] == "no shares"
    assert cap.levels_for("DEAD") == [logging.ERROR]
    assert not cap.levels_for("cancel CONFIRMED")


def test_expected_cancel_defaults_to_false():
    """Callers that do not opt in keep the loud behaviour."""
    import inspect
    sig = inspect.signature(tc.get_order_outcome)
    assert sig.parameters["expected_cancel"].default is False


# ── 2. option exit attribution ────────────────────────────────────────────────

def test_exit_reason_buckets_option_premium_rules():
    """The three premium rules must be distinguishable, and the STOP must not
    read as a plain signal exit."""
    assert pa._exit_reason("option stop loss — bid 4.05 <= 50% of entry 8.15, "
                           "RSI=58.4") == "option_stop"
    assert pa._exit_reason("option profit target — bid 12.30 >= 150% of entry "
                           "8.15") == "option_target"
    assert pa._exit_reason("option near expiry — 5 trading days left "
                           "(<= 5)") == "option_expiry"


def test_exit_reason_regression_guards():
    """The buckets that already existed must not move."""
    assert pa._exit_reason("trailing stop hit @ 489.94 (atr trail)") == "stop"
    assert pa._exit_reason("QQQ reversal, RSI=60.6") == "signal"
    assert pa._exit_reason(None) == "signal"
    assert pa._exit_reason("") == "signal"
    marker = config.CORRECTION_NOTE_MARKER
    assert pa._exit_reason(f"{marker} trailing stop hit @ 1.00") == "correction"


def test_option_stop_is_not_folded_into_the_stop_bucket():
    """Options carry no profit_floor_active/atr_trail_at_exit, so they must stay
    out of "stop" — the profit-floor analysis reads that bucket."""
    assert pa._exit_reason("option stop loss — bid 4.05") != "stop"


def test_close_option_note_carries_the_premium_reason(monkeypatch):
    """The regression that mattered: NVDA's -50% stop recorded "reversal"."""
    notes = {}
    monkeypatch.setattr(strategy.tc, "place_option_order",
                        lambda *a, **k: {"order": {"id": "OID"}})
    monkeypatch.setattr(strategy, "_log_exit_trade",
                        lambda *a, **k: notes.setdefault("note", a[5]))

    strategy._close_option("ACCT", "NVDA 260821C220", 1, 4.05, "NVDA",
                           "2026-08-21", 220.0, "call", {"rsi": 58.4},
                           reason="stop loss — bid 4.05 <= 50% of entry 8.15")

    assert notes["note"].startswith("option stop loss")
    assert "reversal" not in notes["note"]
    assert pa._exit_reason(notes["note"]) == "option_stop"


def test_close_option_without_a_reason_keeps_the_reversal_note(monkeypatch):
    """The state-exit path has no premium reason and must be unchanged."""
    notes = {}
    monkeypatch.setattr(strategy.tc, "place_option_order",
                        lambda *a, **k: {"order": {"id": "OID"}})
    monkeypatch.setattr(strategy, "_log_exit_trade",
                        lambda *a, **k: notes.setdefault("note", a[5]))

    strategy._close_option("ACCT", "QQQ 260821C715", 1, 18.40, "QQQ",
                           "2026-08-21", 715.0, "call", {"rsi": 60.6})

    assert notes["note"].startswith("QQQ reversal")
    assert pa._exit_reason(notes["note"]) == "signal"


# ── 3. CRITICAL alert sink ────────────────────────────────────────────────────

def test_critical_sink_path_is_outside_logs_dir():
    """It must not be under logs/, which logrotate globs as logs/*.log."""
    import trade_logger  # noqa: F401 — for the basicConfig side effect
    # Under conftest this is redirected into a tmpdir; assert on the shape config
    # would produce in production instead.
    raw = f"{config._LOG_PREFIX}critical_alerts.log"
    assert not raw.startswith("logs/"), (
        "a path under logs/ is caught by the logrotate glob and copytruncate'd "
        "daily, which defeats the whole point of this file"
    )


def test_critical_handler_is_critical_only():
    """An INFO/WARNING/ERROR record must not reach the alert sink."""
    import trade_logger
    h = trade_logger._critical_handler
    assert h.level == logging.CRITICAL
    for lvl in (logging.INFO, logging.WARNING, logging.ERROR):
        assert not h.createLock() and lvl < h.level


def test_suite_cannot_write_the_production_critical_sink():
    """The isolation assertion. conftest must have re-pointed this into tmp; a
    fabricated fixture CRITICAL reaching the real alert file is worse than one
    reaching bot.log, because this file is what a human trusts."""
    import trade_logger  # noqa: F401
    tmproot = os.path.realpath(tempfile.gettempdir())
    path = os.path.realpath(config.CRITICAL_ALERT_FILE)
    assert path.startswith(tmproot + os.sep), (
        f"CRITICAL_ALERT_FILE = {path} is outside {tmproot}; the suite can "
        f"write the live alert sink"
    )

    prod = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "critical_alerts.log")
    before = os.path.getsize(prod) if os.path.exists(prod) else None
    logging.getLogger("strategy").critical(
        "CRITICAL: EXIT ORDER REJECTED — FAKE x1 fabricated by the test suite")
    for handler in logging.root.handlers:
        handler.flush()
    after = os.path.getsize(prod) if os.path.exists(prod) else None
    assert after == before, (
        f"test CRITICAL leaked into {prod} ({before} -> {after})")


def test_no_non_file_handlers_on_the_root_logger():
    """Generalises test_log_isolation's FileHandler-only check.

    conftest Layer 3 only re-points logging.FileHandler instances. A journal or
    syslog handler would be skipped by it AND by the size-based leak test, so the
    suite could emit into a persistent sink while every isolation test stayed
    green. Pin the absence rather than trusting future edits.
    """
    offenders = []
    for handler in logging.root.handlers:
        if isinstance(handler, logging.FileHandler):
            continue                      # re-pointed into tmp by conftest
        if isinstance(handler, logging.NullHandler):
            continue                      # pytest's _LiveLoggingNullHandler
        if isinstance(handler, logging.StreamHandler):
            continue                      # stdout; captured by pytest
        offenders.append(type(handler).__name__)
    assert not offenders, (
        f"root logger has handlers conftest cannot isolate: {offenders}. "
        f"Add them to _redirect_existing_log_handlers before shipping.")


if __name__ == "__main__":
    # Direct-run path: no conftest, so only the pure-function checks are safe.
    test_urout_is_a_cancellation()
    test_rejection_and_expiry_are_NOT_cancellations()
    test_exit_reason_buckets_option_premium_rules()
    test_exit_reason_regression_guards()
    test_option_stop_is_not_folded_into_the_stop_bucket()
    print("OK (direct run: pure-function checks only)")
