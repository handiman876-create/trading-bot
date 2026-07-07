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

print("Indicators OK:", sig)
print("All assertions passed.")
