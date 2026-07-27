"""
Tests for fill-price resolution on EXIT paths.

WHY THIS EXISTS: _resolve_fill (5f26dcd, 2026-07-20) ran on entries only. Every
exit ever logged carried price = the signal-bar close and fill_price = null, so
half of every round-trip was mispriced. A broker audit on 2026-07-27 re-priced
all 25 closed trips at real fills and moved realized P&L from -$36,296.78 to
-$39,229.12 — 8.1% understated.

The error is DIRECTIONALLY BIASED, not noisy: you sell into the bid and buy at
the ask, so both legs fill worse and the gap accumulates rather than averaging
out. Pricing only the entry leg fixed only half of it.

The slippage SIGN is the subtle part. It depends on whether we are buying or
selling, NOT on whether the position is long or short — a BUY_TO_COVER closing a
short is still a purchase, so a higher fill is worse. Getting that backwards
would invert the reading on every short exit and would be invisible: the numbers
would still look plausible.
"""

import pytest

import strategy


class _FakeOrder:
    """Stand-in for tc.get_order — returns a fill price, or None for 'unknown'."""
    def __init__(self, fill):
        self.fill = fill
        self.calls = []

    def __call__(self, account_id, order_id):
        self.calls.append((account_id, order_id))
        return self.fill


# ── Slippage sign ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", ["BUY", "BUY_TO_COVER", "BUY_TO_OPEN", "long"])
def test_buying_actions_treat_higher_fill_as_worse(action):
    assert strategy._slippage_sign(action) == 1.0


@pytest.mark.parametrize("action", ["SELL", "SELL_SHORT", "SELL_TO_CLOSE", "short"])
def test_selling_actions_treat_lower_fill_as_worse(action):
    assert strategy._slippage_sign(action) == -1.0


def test_unknown_action_raises_rather_than_guessing():
    """A silently wrong sign would invert slippage on that path and stay
    invisible, because the magnitudes would still look reasonable."""
    with pytest.raises(ValueError):
        strategy._slippage_sign("HODL")


# ── Exit-side slippage math ───────────────────────────────────────────────────

def test_sell_exit_positive_slippage_when_sold_cheaper(monkeypatch):
    """Closing a long: filled BELOW the signal = worse = positive slippage."""
    monkeypatch.setattr(strategy.tc, "get_order", _FakeOrder(99.50))
    _, fill, slip = strategy._resolve_fill("X", "ACCT", "OID", 100.0, "SELL")
    assert fill == 99.50 and slip == 0.50


def test_sell_exit_negative_slippage_when_sold_higher(monkeypatch):
    monkeypatch.setattr(strategy.tc, "get_order", _FakeOrder(100.75))
    _, _, slip = strategy._resolve_fill("X", "ACCT", "OID", 100.0, "SELL")
    assert slip == -0.75


def test_cover_exit_positive_slippage_when_paid_more(monkeypatch):
    """The real AMD cover: signal 491.26, filled 491.73. Buying back a short at
    a HIGHER price is worse, so slippage is positive."""
    monkeypatch.setattr(strategy.tc, "get_order", _FakeOrder(491.73))
    _, fill, slip = strategy._resolve_fill("AMD", "ACCT", "OID", 491.26, "BUY_TO_COVER")
    assert fill == 491.73
    assert slip == pytest.approx(0.47)


def test_cover_and_sell_signs_are_opposite(monkeypatch):
    """Same fill drift, opposite verdict — this is the asymmetry that makes the
    sign worth a dedicated test."""
    monkeypatch.setattr(strategy.tc, "get_order", _FakeOrder(101.0))
    _, _, cover_slip = strategy._resolve_fill("X", "ACCT", "OID", 100.0, "BUY_TO_COVER")
    _, _, sell_slip = strategy._resolve_fill("X", "ACCT", "OID", 100.0, "SELL")
    assert cover_slip > 0 > sell_slip


def test_exit_falls_back_to_signal_when_fill_unavailable(monkeypatch):
    """Degraded, not disabled — the exit still gets logged."""
    monkeypatch.setattr(strategy.tc, "get_order", _FakeOrder(None))
    resolved, fill, slip = strategy._resolve_fill("X", "ACCT", "OID", 100.0, "SELL")
    assert resolved == 100.0 and fill is None and slip is None


def test_no_order_id_skips_the_broker_call(monkeypatch):
    fake = _FakeOrder(99.0)
    monkeypatch.setattr(strategy.tc, "get_order", fake)
    resolved, fill, slip = strategy._resolve_fill("X", "ACCT", None, 100.0, "SELL")
    assert resolved == 100.0 and fill is None and slip is None
    assert fake.calls == [], "must not query the broker without an order id"


# ── _log_exit_trade wiring ────────────────────────────────────────────────────

def test_log_exit_trade_records_the_fill(monkeypatch):
    monkeypatch.setattr(strategy.tc, "get_order", _FakeOrder(191.44))
    captured = {}
    monkeypatch.setattr(strategy, "log_trade",
                        lambda *a, **k: captured.update(args=a, kwargs=k))
    strategy._log_exit_trade("SELL", "DHR", 258, 192.38, "OID", "stop hit", "ACCT")
    assert captured["kwargs"]["fill_price"] == 191.44
    assert captured["kwargs"]["signal_price"] == 192.38
    assert captured["kwargs"]["slippage"] == pytest.approx(0.94)
    assert captured["args"][0] == "SELL"


def test_log_exit_trade_still_logs_when_fill_unknown(monkeypatch):
    """A broker hiccup must not swallow the trade record — that would create the
    orphan exits the analyzer already warns about."""
    monkeypatch.setattr(strategy.tc, "get_order", _FakeOrder(None))
    captured = {}
    monkeypatch.setattr(strategy, "log_trade",
                        lambda *a, **k: captured.update(args=a, kwargs=k))
    strategy._log_exit_trade("BUY_TO_COVER", "AMD", 96, 534.50, "OID", "cover", "ACCT")
    assert captured["args"][0] == "BUY_TO_COVER"
    assert captured["kwargs"]["fill_price"] is None
    assert captured["kwargs"]["signal_price"] == 534.50


def test_every_exit_path_routes_through_the_helper():
    """Guard against the next exit path being written with a bare log_trade.

    Eight exit sites previously called log_trade directly, which is exactly how
    every exit leg ended up signal-priced. Only entry paths (which resolve their
    own fill because they also arm a stop) and the helper itself may call
    log_trade directly."""
    import ast
    src = open(strategy.__file__).read()
    tree = ast.parse(src)
    ALLOWED = {"_log_exit_trade", "_enter_long", "_enter_short",
               "evaluate_future", "_open_option"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name in ALLOWED:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and getattr(sub.func, "id", "") == "log_trade":
                offenders.append(f"{node.name}:{sub.lineno}")
    assert not offenders, (
        f"bare log_trade() outside an entry path: {offenders} — exits must go "
        f"through _log_exit_trade so they carry a real fill price"
    )


if __name__ == "__main__":
    print("run under pytest (uses monkeypatch/parametrize)")
