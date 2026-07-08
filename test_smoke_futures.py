"""
Read-only FUTURES smoke test — verifies the bot can see and price futures in
the sandbox WITHOUT placing any orders. Companion to test_smoke.py (equities).

Checks:
  0. Credentials present
  1. OAuth token refresh
  2. Futures account id            -> get_futures_account_id()
  3. Continuous bar history        -> get_historical("@ES")   (indicator source)
  4. Front-month resolution + quote-> front_month_contract("ES") -> get_quote()
  5. Order CONFIRM (no order)      -> confirm_order(...) returns InitialMargin

Run:  python3 test_smoke_futures.py
Exits non-zero if any check fails, so it doubles as a pre-flight gate.
"""

import sys

import config
import tradestation_client as tc
import futures_market_hours as fmh

PASS, FAIL = [], []
ROOT = "ES"


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main() -> int:
    print("=" * 60)
    print(f"Endpoint: {config.TS_BASE_URL}  (sandbox={config.TS_SANDBOX})")
    print("=" * 60)

    check("credentials_set",
          bool(config.TS_CLIENT_ID and config.TS_CLIENT_SECRET and config.TS_REFRESH_TOKEN),
          "→ TS_CLIENT_ID / TS_CLIENT_SECRET / TS_REFRESH_TOKEN")

    # 1. OAuth refresh
    print("1. OAuth token refresh")
    try:
        tc._force_refresh()
        tok = tc._get_access_token()
        check("oauth_refresh", isinstance(tok, str) and len(tok) > 20,
              f"→ access token acquired ({len(tok)} chars)")
    except Exception as exc:
        check("oauth_refresh", False, f"→ {type(exc).__name__}: {exc}")

    # 2. Futures account
    print("2. Futures account id")
    facct = tc.get_futures_account_id()
    check("futures_account_id", bool(facct), f"→ {facct}")

    # 3. Continuous bars for the signal
    print(f"3. Continuous bar history {fmh.signal_symbol(ROOT)}")
    bars = tc.get_historical(fmh.signal_symbol(ROOT), interval="daily", days=90)
    last = bars[-1] if bars else None
    check("continuous_bars", len(bars) >= config.MA_LONG_PERIOD,
          f"→ {len(bars)} bars; latest {last.get('date')} close={last.get('close')}"
          if last else "→ 0 bars")

    # 4. Front-month resolution + quote
    print("4. Front-month contract + quote")
    contract = fmh.front_month_contract(ROOT, roll_days=config.FUTURES_ROLL_DAYS)
    q = tc.get_quote(contract)
    check("front_month_quote", bool(q) and q.get("last") is not None,
          f"→ {contract} last={q.get('last')} bid={q.get('bid')} ask={q.get('ask')}"
          if q else f"→ {contract}: None")

    # 5. Order confirm (NO ORDER PLACED)
    print("5. Order confirm (read-only, NO order placed)")
    if facct and q:
        conf = tc.confirm_order(facct, contract, "BUY", config.FUTURES_CONTRACTS)
        ok = bool(conf) and conf.get("OrderAssetCategory") == "FUTURE"
        check("order_confirm", ok,
              f"→ {conf.get('SummaryMessage')} | margin={conf.get('InitialMarginDisplay')} "
              f"| route={conf.get('Route')}" if conf else "→ None")
    else:
        check("order_confirm", False, "→ skipped (no account or quote)")

    print("=" * 60)
    print(f"RESULTS: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    print("All read-only futures checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
