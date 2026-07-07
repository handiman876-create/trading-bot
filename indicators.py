import pandas as pd
import numpy as np


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_indicators(history: list[dict], short: int, long_: int, rsi_period: int) -> dict:
    """
    Given list of OHLCV dicts from Tradier, compute indicators.
    Returns dict with latest values or None if insufficient data.
    """
    if len(history) < long_:
        return {}

    df = pd.DataFrame(history)
    df["close"] = pd.to_numeric(df["close"])
    closes = df["close"]

    short_ema  = ema(closes, short)
    long_ema   = ema(closes, long_)
    rsi_series = rsi(closes, rsi_period)

    prev_short = short_ema.iloc[-2]
    prev_long  = long_ema.iloc[-2]
    curr_short = short_ema.iloc[-1]
    curr_long  = long_ema.iloc[-1]

    # Golden cross: short crosses above long
    bullish_cross = (prev_short <= prev_long) and (curr_short > curr_long)
    # Death cross: short crosses below long
    bearish_cross = (prev_short >= prev_long) and (curr_short < curr_long)

    return {
        "close":         float(closes.iloc[-1]),
        "ema_short":     float(curr_short),
        "ema_long":      float(curr_long),
        "rsi":           float(rsi_series.iloc[-1]),
        "bullish_cross": bullish_cross,
        "bearish_cross": bearish_cross,
    }
