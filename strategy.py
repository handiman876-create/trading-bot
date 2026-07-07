"""
Signal generation and order execution for stocks and options.

Stock signals  — EMA crossover + RSI confirmation:
  BUY  when short EMA crosses above long EMA  AND  RSI < overbought
  SELL when short EMA crosses below long EMA  AND  RSI > oversold

Options signals — same crossover applied to the underlying:
  BUY_TO_OPEN  call when bullish cross + RSI < overbought
  BUY_TO_OPEN  put  when bearish cross + RSI > oversold
  Existing positions are closed on the opposite cross.
"""

import logging
import math
from datetime import date
from typing import Optional

import config
import tradestation_client as tc
import indicators as ind
from trade_logger import log_trade

logger = logging.getLogger(__name__)

# Tracks the last date a BUY/SELL was fired per symbol, preventing the daily
# EMA cross from re-triggering on every 60-second poll within the same day.
_last_signal_date: dict[str, str] = {}


def _already_signaled_today(symbol: str) -> bool:
    return _last_signal_date.get(symbol) == date.today().isoformat()


def _mark_signaled(symbol: str) -> None:
    _last_signal_date[symbol] = date.today().isoformat()


def _shares_to_buy(price: float) -> int:
    if price <= 0:
        return 0
    return max(1, math.floor(config.MAX_POSITION_VALUE / price))


def _current_position(positions: list[dict], symbol: str) -> int:
    """Return net quantity held for symbol (0 if none)."""
    for p in positions:
        if p.get("symbol") == symbol:
            return int(p.get("quantity", 0))
    return 0


# ── Stock Strategy ────────────────────────────────────────────────────────────

def evaluate_stock(symbol: str, account_id: str, positions: list[dict]) -> None:
    history = tc.get_historical(symbol, days=90)
    if not history:
        return

    sig = ind.compute_indicators(
        history,
        config.MA_SHORT_PERIOD,
        config.MA_LONG_PERIOD,
        config.RSI_PERIOD,
    )
    if not sig:
        logger.warning("%s: not enough history for indicators", symbol)
        return

    held = _current_position(positions, symbol)
    price = sig["close"]

    logger.info(
        "%s | price=%.2f  EMA%d=%.2f  EMA%d=%.2f  RSI=%.1f  held=%d",
        symbol, price,
        config.MA_SHORT_PERIOD, sig["ema_short"],
        config.MA_LONG_PERIOD,  sig["ema_long"],
        sig["rsi"], held,
    )

    if _already_signaled_today(symbol):
        return

    # BUY signal
    if sig["bullish_cross"] and sig["rsi"] < config.RSI_OVERBOUGHT and held == 0:
        qty = _shares_to_buy(price)
        logger.info("SIGNAL BUY %s x%d", symbol, qty)
        result = tc.place_equity_order(account_id, symbol, "buy", qty)
        if result:
            _mark_signaled(symbol)
            order_id = result.get("order", {}).get("id")
            log_trade("BUY", symbol, qty, price, "market", order_id,
                      f"EMA cross up, RSI={sig['rsi']:.1f}")

    # SELL signal
    elif sig["bearish_cross"] and sig["rsi"] > config.RSI_OVERSOLD and held > 0:
        logger.info("SIGNAL SELL %s x%d", symbol, held)
        result = tc.place_equity_order(account_id, symbol, "sell", held)
        if result:
            _mark_signaled(symbol)
            order_id = result.get("order", {}).get("id")
            log_trade("SELL", symbol, held, price, "market", order_id,
                      f"EMA cross down, RSI={sig['rsi']:.1f}")


# ── Options Strategy ──────────────────────────────────────────────────────────

def evaluate_option(
    symbol:     str,
    expiration: str,
    strike:     float,
    opt_type:   str,
    account_id: str,
    positions:  list[dict],
) -> None:
    history = tc.get_historical(symbol, days=90)
    if not history:
        return

    sig = ind.compute_indicators(
        history,
        config.MA_SHORT_PERIOD,
        config.MA_LONG_PERIOD,
        config.RSI_PERIOD,
    )
    if not sig:
        return

    occ_symbol = tc.find_option_symbol(symbol, expiration, strike, opt_type)
    if not occ_symbol:
        return

    held = _current_position(positions, occ_symbol)
    opt_quote = tc.get_option_quote(occ_symbol)
    opt_price = float(opt_quote.get("last") or opt_quote.get("bid") or 0) if opt_quote else 0.0

    logger.info(
        "OPTION %s %s %.2f %s | underlying=%.2f  RSI=%.1f  opt_price=%.2f  held=%d",
        symbol, expiration, strike, opt_type,
        sig["close"], sig["rsi"], opt_price, held,
    )

    is_call = opt_type.lower() == "call"

    # Open new position
    if held == 0:
        if is_call and sig["bullish_cross"] and sig["rsi"] < config.RSI_OVERBOUGHT:
            _open_option(account_id, occ_symbol, "buy_to_open", opt_price,
                         symbol, expiration, strike, opt_type, sig)
        elif not is_call and sig["bearish_cross"] and sig["rsi"] > config.RSI_OVERSOLD:
            _open_option(account_id, occ_symbol, "buy_to_open", opt_price,
                         symbol, expiration, strike, opt_type, sig)

    # Close existing position on opposite cross
    elif held > 0:
        if is_call and sig["bearish_cross"]:
            _close_option(account_id, occ_symbol, held, opt_price,
                          symbol, expiration, strike, opt_type, sig)
        elif not is_call and sig["bullish_cross"]:
            _close_option(account_id, occ_symbol, held, opt_price,
                          symbol, expiration, strike, opt_type, sig)


def _open_option(account_id, occ_symbol, side, price, symbol, exp, strike, opt_type, sig):
    qty = config.OPTIONS_CONTRACTS
    logger.info("SIGNAL %s %s x%d", side.upper(), occ_symbol, qty)
    result = tc.place_option_order(account_id, occ_symbol, side, qty)
    if result:
        order_id = result.get("order", {}).get("id")
        log_trade(side.upper(), occ_symbol, qty, price, "market", order_id,
                  f"{symbol} EMA cross, RSI={sig['rsi']:.1f}, strike={strike} {opt_type} exp={exp}")


def _close_option(account_id, occ_symbol, held, price, symbol, exp, strike, opt_type, sig):
    logger.info("SIGNAL SELL_TO_CLOSE %s x%d", occ_symbol, held)
    result = tc.place_option_order(account_id, occ_symbol, "sell_to_close", held)
    if result:
        order_id = result.get("order", {}).get("id")
        log_trade("SELL_TO_CLOSE", occ_symbol, held, price, "market", order_id,
                  f"{symbol} reversal, RSI={sig['rsi']:.1f}, strike={strike} {opt_type} exp={exp}")
