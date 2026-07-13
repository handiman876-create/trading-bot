"""Smoke test for indicators module using synthetic price data."""

import random
from indicators import compute_indicators

random.seed(42)

prices = [150.0]
for _ in range(59):
    prices.append(round(prices[-1] * (1 + random.uniform(-0.02, 0.02)), 2))

history = [
    {
        "close":  p,
        "open":   p,
        "high":   round(p * 1.01, 2),
        "low":    round(p * 0.99, 2),
        "volume": 1_000_000,
        "date":   "2025-01-01",
    }
    for p in prices
]

sig = compute_indicators(history, short=9, long_=21, rsi_period=14)

assert sig, "compute_indicators returned empty result"
assert "close"         in sig
assert "ema_short"     in sig
assert "ema_long"      in sig
assert "rsi"           in sig
assert "bullish_cross" in sig
assert "bearish_cross" in sig
assert 0 <= sig["rsi"] <= 100, f"RSI out of range: {sig['rsi']}"

# ── ATR (Wilder) ──────────────────────────────────────────────────────────────
assert "atr" in sig, "atr key missing"
assert sig["atr"] is not None and sig["atr"] > 0, f"atr should be positive: {sig['atr']}"

# Known value: a constant true range of 2.0 -> Wilder ATR converges to exactly 2.0
flat = [{"open": 101, "high": 102, "low": 100, "close": 101,
         "volume": 1, "date": "d"} for _ in range(30)]
flat_sig = compute_indicators(flat, short=9, long_=21, rsi_period=14, atr_period=14)
assert abs(flat_sig["atr"] - 2.0) < 1e-9, \
    f"constant-TR ATR should be 2.0, got {flat_sig['atr']}"

# atr is None ONLY when high/low are absent — never from short history, since
# compute_indicators already requires len >= long_ (21) > atr_period (14).
no_hl = [{"close": p} for p in prices]
nohl_sig = compute_indicators(no_hl, short=9, long_=21, rsi_period=14, atr_period=14)
assert nohl_sig["atr"] is None, f"atr should be None without high/low, got {nohl_sig['atr']}"

# Wilder must differ from a simple rolling mean of TR — guards against a
# regression to rolling().mean().
import pandas as pd
from indicators import atr as atr_fn

df = pd.DataFrame(history)
tr = pd.concat([
    df["high"] - df["low"],
    (df["high"] - df["close"].shift(1)).abs(),
    (df["low"]  - df["close"].shift(1)).abs(),
], axis=1).max(axis=1)
wilder = atr_fn(df["high"], df["low"], df["close"], 14).iloc[-1]
simple = tr.rolling(14).mean().iloc[-1]
assert abs(wilder - simple) > 1e-6, "Wilder ATR should differ from simple rolling mean"

print("Indicators OK:", sig)
print("All assertions passed.")
