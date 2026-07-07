"""
Read-only TradeStation smoke test — replaces the stale Tradier tests
(test_live.py, test_config.py, test_sandbox_switch.py).

Verifies the four things the bot needs before it can trade, WITHOUT placing
any orders:
  1. OAuth token refresh   -> tradestation_client._refresh_access_token / _get_access_token
  2. Account ID retrieval  -> get_account_id()
  3. SPY quote             -> get_quote("SPY")
  4. SPY bar data          -> get_historical("SPY")

Run:  python3 test_smoke.py
Exits non-zero if any check fails, so it doubles as a pre-flight gate.
"""

import sys

import config
import tradestation_client as tc

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main() -> int:
    print("=" * 60)
    print(f"Endpoint: {config.TS_BASE_URL}  (sandbox={config.TS_SANDBOX})")
    print("=" * 60)

    # ── 0. Credentials present ────────────────────────────────────────────────
    check("credentials_set",
          bool(config.TS_CLIENT_ID and config.TS_CLIENT_SECRET and config.TS_REFRESH_TOKEN),
          "→ TS_CLIENT_ID / TS_CLIENT_SECRET / TS_REFRESH_TOKEN")

    # ── 1. OAuth token refresh ────────────────────────────────────────────────
    print("1. OAuth token refresh")
    try:
        tc._force_refresh()                 # force a live refresh-token exchange
        tok = tc._get_access_token()
        check("oauth_refresh", isinstance(tok, str) and len(tok) > 20,
              f"→ access token acquired ({len(tok)} chars)")
    except Exception as exc:
        check("oauth_refresh", False, f"→ {type(exc).__name__}: {exc}")

    # ── 2. Account ID ─────────────────────────────────────────────────────────
    print("2. Account ID")
    account_id = tc.get_account_id()
    check("account_id", bool(account_id), f"→ {account_id}")

    # ── 3. SPY quote ──────────────────────────────────────────────────────────
    print("3. SPY quote")
    q = tc.get_quote("SPY")
    check("quote_SPY", bool(q) and q.get("last") is not None,
          f"→ last=${q.get('last')} bid=${q.get('bid')} ask=${q.get('ask')} close=${q.get('close')}"
          if q else "→ None")

    # ── 4. SPY bar data ───────────────────────────────────────────────────────
    print("4. SPY bar data")
    bars = tc.get_historical("SPY", interval="daily", days=90)
    last_bar = bars[-1] if bars else None
    check("bars_SPY", len(bars) >= config.MA_LONG_PERIOD,
          f"→ {len(bars)} bars; latest {last_bar.get('date')} close=${last_bar.get('close')}"
          if last_bar else "→ 0 bars")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("=" * 60)
    print(f"RESULTS: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    print("All read-only checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
