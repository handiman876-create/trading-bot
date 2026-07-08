"""
Unit tests for the futures order-building path — NO network.

Monkeypatches tradestation_client._post to capture the request body, so we can
assert the futures order/confirm payloads without hitting the API or placing
anything.

Run:  python3 test_futures_orders.py
"""

import tradestation_client as tc

_calls = []   # (path, body) captured from each _post


def _fake_post(path, json_body):
    _calls.append((path, json_body))
    if path.endswith("orderconfirm"):
        return {"Confirmations": [{"OrderAssetCategory": "FUTURE",
                                   "InitialMarginDisplay": "28,116.00 USD",
                                   "Route": json_body.get("Route"),
                                   "SummaryMessage": "Buy 1 ESU26 @ Market"}]}
    return {"Orders": [{"OrderID": "TEST-1"}]}


def _reset():
    _calls.clear()


def test_place_futures_buy_body():
    _reset()
    result = tc.place_futures_order("SIM1F", "ESU26", "buy", 1)
    assert result == {"order": {"id": "TEST-1"}}, result
    path, body = _calls[-1]
    assert path == "orderexecution/orders", path
    assert body["TradeAction"] == "BUY", body
    assert body["Symbol"] == "ESU26"
    assert body["Quantity"] == "1"
    assert body["OrderType"] == "Market"
    assert body["Route"] == "Intelligent"       # confirmed valid+default for futures
    assert body["TimeInForce"] == {"Duration": "DAY"}


def test_place_futures_sell_body():
    _reset()
    tc.place_futures_order("SIM1F", "ESU26", "sell", 2)
    _, body = _calls[-1]
    assert body["TradeAction"] == "SELL", body
    assert body["Quantity"] == "2"


def test_place_futures_unknown_side_returns_none():
    _reset()
    # BUYTOCOVER/SELLSHORT are equity concepts; futures only accept buy/sell.
    assert tc.place_futures_order("SIM1F", "ESU26", "buy_to_cover", 1) is None
    assert _calls == []          # nothing was posted


def test_confirm_order_uses_orderconfirm_endpoint():
    _reset()
    conf = tc.confirm_order("SIM1F", "ESU26", "BUY", 1)
    path, body = _calls[-1]
    assert path == "orderexecution/orderconfirm", path
    assert body["TradeAction"] == "BUY"
    assert conf["OrderAssetCategory"] == "FUTURE"
    assert conf["InitialMarginDisplay"] == "28,116.00 USD"


if __name__ == "__main__":
    _orig = tc._post
    tc._post = _fake_post
    try:
        tests = [v for k, v in sorted(globals().items())
                 if k.startswith("test_") and callable(v)]
        passed = 0
        for t in tests:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        print(f"All {passed} assertions passed.")
    finally:
        tc._post = _orig
