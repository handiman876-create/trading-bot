"""
Unit tests for order-outcome classification — NO network.

WHY THIS EXISTS: get_order() decided filled-vs-pending purely on "is there a
FilledPrice?" — a test a REJECTED order fails for exactly the same reason a slow
one does. On 2026-08-11 a GOOGL stop exit was refused by the broker ("You are
long 127 shares with 127 remaining on sell orders!"); get_order saw no fill
price, logged the rejection as "still pending", and returned None. The caller
read that None as "it filled but we could not read the price" and logged a
completed SELL of 127 shares that never happened, at a price 2.55 better than
reality, then released the position's trailing stop.

A rejection and a slow fill are OPPOSITE outcomes. The invariant here is that
they never share a return value, and that an unreachable API is a third thing
again — "unknown" must never be mistaken for "refused", or a network blip would
trigger a rollback of a perfectly good exit.

Run:  python3 test_order_outcome.py
"""

import tradestation_client as tc


# ── Response builders ─────────────────────────────────────────────────────────

def _resp(status_desc, status=None, filled_price=None, reject_reason=None):
    """Shape a TradeStation GetOrders response. Numerics are STRINGS, as the API
    returns them — a rejected order really does carry FilledPrice '0'."""
    o = {"OrderID": "X1", "StatusDescription": status_desc, "Legs": []}
    if status is not None:
        o["Status"] = status
    if filled_price is not None:
        o["FilledPrice"] = str(filled_price)
    if reject_reason is not None:
        o["RejectReason"] = reject_reason
    return {"Orders": [o]}


# The verbatim broker response that started all of this.
_GOOGL_REJECTION = _resp("Rejected", status="REJ", filled_price=0,
                         reject_reason="You are long 127 shares with 127 "
                                       "remaining on sell orders!")


def _with_get(resp_or_raiser):
    """Swap tc._get for the duration of a `with` block."""
    class _Ctx:
        def __enter__(self):
            self.orig, self.sleep = tc._get, tc.time.sleep
            tc._get = (resp_or_raiser if callable(resp_or_raiser)
                       else (lambda path, params=None: resp_or_raiser))
            tc.time.sleep = lambda s: None
            return self
        def __exit__(self, *exc):
            tc._get, tc.time.sleep = self.orig, self.sleep
    return _Ctx()


# ── The three states must not collapse ────────────────────────────────────────

def test_rejected_order_is_dead_not_working():
    """THE bug: a rejection has no fill price, exactly like a slow fill."""
    with _with_get(_GOOGL_REJECTION):
        out = tc.get_order_outcome("ACCT", "X1")
    assert out["state"] == "dead", out
    assert out["fill_price"] is None, out


def test_rejection_reason_is_surfaced():
    """The broker's reason reached NO log during the incident — it had to be
    pulled from the API by hand afterwards. It is the single most useful fact
    about a refused exit."""
    with _with_get(_GOOGL_REJECTION):
        out = tc.get_order_outcome("ACCT", "X1")
    assert "remaining on sell orders" in out["reason"], out


def test_working_order_is_not_dead():
    """A genuinely pending order must stay 'working' — the fix must not turn
    slow fills into false rejections, which would strand real exits."""
    with _with_get(_resp("Received", status="ACK")):
        out = tc.get_order_outcome("ACCT", "X1")
    assert out["state"] == "working", out


# ── Polling and backoff ───────────────────────────────────────────────────────

def test_working_order_is_polled_with_backoff():
    """A slow fill gets the full ladder, so a thin name that takes >2s is priced
    at its real fill instead of the signal bar."""
    slept, calls = [], []
    orig, orig_sleep = tc._get, tc.time.sleep

    def _count(path, params=None):
        calls.append(path)
        return _resp("Received", status="ACK")
    tc._get, tc.time.sleep = _count, slept.append
    try:
        tc.get_order_outcome("ACCT", "X1")
    finally:
        tc._get, tc.time.sleep = orig, orig_sleep
    assert slept == list(tc._ORDER_POLL_BACKOFF), slept
    assert len(calls) == len(tc._ORDER_POLL_BACKOFF) + 1, calls


def test_a_fill_arriving_mid_backoff_ends_the_polling():
    """The ladder must stop the moment the fill lands, not run to its end."""
    seq = [_resp("Received", status="ACK"),
           _resp("Filled", status="FLL", filled_price=349.91)]
    slept = []
    orig, orig_sleep = tc._get, tc.time.sleep
    tc._get = lambda path, params=None: seq.pop(0)
    tc.time.sleep = slept.append
    try:
        out = tc.get_order_outcome("ACCT", "X1")
    finally:
        tc._get, tc.time.sleep = orig, orig_sleep
    assert out["state"] == "filled" and out["fill_price"] == 349.91, out
    assert len(slept) == 1, slept


def test_rejection_is_never_retried():
    """THE correction. A rejection is TERMINAL — polling it five times returns
    the same REJ five times, 14 seconds later, and delays the retry-next-cycle
    by a full loop. Backoff buys fill-price accuracy on slow fills; it does
    nothing whatsoever for refusals."""
    slept, calls = [], []
    orig, orig_sleep = tc._get, tc.time.sleep

    def _count(path, params=None):
        calls.append(path)
        return _GOOGL_REJECTION
    tc._get, tc.time.sleep = _count, slept.append
    try:
        out = tc.get_order_outcome("ACCT", "X1")
    finally:
        tc._get, tc.time.sleep = orig, orig_sleep
    assert out["state"] == "dead", out
    assert calls == calls[:1], calls          # exactly one lookup
    assert slept == [], "a terminal order must not sleep at all"


def test_filled_order_reports_price():
    with _with_get(_resp("Filled", status="FLL", filled_price=349.91)):
        out = tc.get_order_outcome("ACCT", "X1")
    assert out["state"] == "filled" and out["fill_price"] == 349.91, out


def test_fill_price_falls_back_to_leg_execution_price():
    resp = _resp("Filled", status="FLL")
    resp["Orders"][0]["Legs"] = [{"ExecutionPrice": "512.18"}]
    with _with_get(resp):
        out = tc.get_order_outcome("ACCT", "X1")
    assert out["state"] == "filled" and out["fill_price"] == 512.18, out


def test_api_error_is_unknown_not_dead():
    """An unreachable API must never read as a rejection: that would make a
    network blip look like a refused exit and trigger a needless rollback."""
    def _boom(path, params=None):
        raise RuntimeError("503")
    with _with_get(_boom):
        assert tc.get_order_outcome("ACCT", "X1")["state"] == "unknown"


def test_missing_order_is_unknown():
    with _with_get({"Orders": []}):
        assert tc.get_order_outcome("ACCT", "X1")["state"] == "unknown"


def test_get_order_wrapper_still_returns_none_on_rejection():
    """Back-compat: entry-side callers only want a price and fall back to the
    signal bar however the price went missing."""
    with _with_get(_GOOGL_REJECTION):
        assert tc.get_order("ACCT", "X1") is None


def test_get_order_wrapper_returns_the_fill():
    with _with_get(_resp("Filled", status="FLL", filled_price=349.91)):
        assert tc.get_order("ACCT", "X1") == 349.91


# ── UROut is terminal ─────────────────────────────────────────────────────────

def test_urout_counts_as_done():
    """'UROut' (User Requested Out) is what a CANCELLED order becomes, and it
    was missing from _DONE_STATUSES — so GOOGL's dead floor came back from
    get_working_orders looking live. Had the stop record still carried that
    order id, reconcile would have matched it and skipped re-arming the
    position forever, believing it protected."""
    assert tc._order_is_done({"StatusDescription": "UROut"})
    assert tc._order_failed({"StatusDescription": "UROut"})


def test_status_code_alone_is_enough():
    """The two fields are not redundant: match on either, since the codes and
    the descriptions each carry spellings the other does not."""
    assert tc._order_is_done({"Status": "REJ"})
    assert tc._order_failed({"Status": "REJ"})


def test_filled_is_done_but_not_failed():
    """The one status separating the two sets: a filled order is finished but it
    DID execute, so it must never be rolled back as a failure."""
    assert tc._order_is_done({"StatusDescription": "Filled"})
    assert not tc._order_failed({"StatusDescription": "Filled"})


def test_live_order_is_neither():
    assert not tc._order_is_done({"StatusDescription": "Received"})
    assert not tc._order_failed({"StatusDescription": "Received"})


def test_working_orders_excludes_urout():
    resp = {"Orders": [
        {"OrderID": "DEAD", "StatusDescription": "UROut", "OrderType": "StopMarket",
         "Legs": [{"Symbol": "GOOGL", "BuyOrSell": "Sell", "QuantityOrdered": "127"}]},
        {"OrderID": "LIVE", "StatusDescription": "Received", "OrderType": "StopMarket",
         "Legs": [{"Symbol": "QQQ", "BuyOrSell": "Sell", "QuantityOrdered": "66"}]},
    ]}
    with _with_get(resp):
        ids = [o["order_id"] for o in tc.get_working_orders("ACCT")]
    assert ids == ["LIVE"], ids


if __name__ == "__main__":
    import sys
    mod = sys.modules[__name__]
    for name in [n for n in dir(mod) if n.startswith("test_")]:
        getattr(mod, name)()
        print("ok", name)
    print("all passed")
