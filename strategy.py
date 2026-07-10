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
import futures_market_hours as fmh
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


def _atm_strike(price: float) -> float:
    """ATM strike chosen at signal time: the nearest listed $5 strike increment
    to the underlying price (same rule for calls and puts)."""
    return round(price / 5.0) * 5.0


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

    # ATM strike is chosen at signal time from the underlying price (nearest $5),
    # so it tracks the market instead of drifting from a hardcoded config value.
    strike = _atm_strike(sig["close"])

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

    if _already_signaled_today(occ_symbol):
        return

    is_call = opt_type.lower() == "call"

    # Open new position
    if held == 0:
        if is_call and sig["bullish_cross"] and sig["rsi"] < config.RSI_OVERBOUGHT:
            if _open_option(account_id, occ_symbol, "buy_to_open", opt_price,
                            symbol, expiration, strike, opt_type, sig):
                _mark_signaled(occ_symbol)
        elif not is_call and sig["bearish_cross"] and sig["rsi"] > config.RSI_OVERSOLD:
            if _open_option(account_id, occ_symbol, "buy_to_open", opt_price,
                            symbol, expiration, strike, opt_type, sig):
                _mark_signaled(occ_symbol)

    # Close existing position on opposite cross
    elif held > 0:
        if is_call and sig["bearish_cross"]:
            if _close_option(account_id, occ_symbol, held, opt_price,
                             symbol, expiration, strike, opt_type, sig):
                _mark_signaled(occ_symbol)
        elif not is_call and sig["bullish_cross"]:
            if _close_option(account_id, occ_symbol, held, opt_price,
                             symbol, expiration, strike, opt_type, sig):
                _mark_signaled(occ_symbol)


# ── Futures Strategy ──────────────────────────────────────────────────────────
# Long-only, mirroring evaluate_stock: BUY the front-month on a bullish cross,
# SELL to flatten on a bearish cross. Signals are computed on the CONTINUOUS
# symbol (@ES) for a clean bar history; orders go to the DATED front month
# (ESU26). We do NOT roll while holding — an open position in a rolled-past
# contract is flattened first, and the new front month is picked up next cycle.

def _stale_futures_position(positions: list[dict], root: str, current_symbol: str) -> Optional[dict]:
    """An open position in a different-dated contract of the same root (i.e. one
    we've rolled past), or None."""
    for p in positions:
        sym = p.get("symbol") or ""
        if sym.startswith(root) and sym != current_symbol and int(p.get("quantity", 0)) != 0:
            return p
    return None


def evaluate_future(root: str, account_id: str, positions: list[dict]) -> None:
    trade_symbol = fmh.front_month_contract(root, roll_days=config.FUTURES_ROLL_DAYS)
    sig_symbol   = fmh.signal_symbol(root)

    history = tc.get_historical(sig_symbol, days=90)
    if not history:
        logger.warning("%s: no bar history for %s", root, sig_symbol)
        return

    sig = ind.compute_indicators(
        history,
        config.MA_SHORT_PERIOD,
        config.MA_LONG_PERIOD,
        config.RSI_PERIOD,
    )
    if not sig:
        logger.warning("%s: not enough history for indicators", root)
        return

    held  = _current_position(positions, trade_symbol)
    price = sig["close"]

    logger.info(
        "FUT %s | signal=%s trade=%s  close=%.2f  EMA%d=%.2f  EMA%d=%.2f  RSI=%.1f  held=%d",
        root, sig_symbol, trade_symbol, price,
        config.MA_SHORT_PERIOD, sig["ema_short"],
        config.MA_LONG_PERIOD,  sig["ema_long"],
        sig["rsi"], held,
    )

    # Roll guard: flatten any position in a rolled-past contract before trading
    # the new front month. Skip the rest of this cycle for this root.
    stale = _stale_futures_position(positions, root, trade_symbol)
    if stale:
        qty = abs(int(stale.get("quantity", 0)))
        logger.info("ROLL: flattening expiring %s x%d before trading %s",
                    stale.get("symbol"), qty, trade_symbol)
        result = tc.place_futures_order(account_id, stale.get("symbol"), "sell", qty)
        if result:
            order_id = result.get("order", {}).get("id")
            log_trade("SELL", stale.get("symbol"), qty, price, "market", order_id,
                      f"{root} roll: flatten expiring contract")
        return

    if _already_signaled_today(trade_symbol):
        return

    qty = config.FUTURES_CONTRACTS

    # BUY signal — open long front month
    if sig["bullish_cross"] and sig["rsi"] < config.RSI_OVERBOUGHT and held == 0:
        logger.info("SIGNAL BUY %s x%d", trade_symbol, qty)
        result = tc.place_futures_order(account_id, trade_symbol, "buy", qty)
        if result:
            _mark_signaled(trade_symbol)
            order_id = result.get("order", {}).get("id")
            log_trade("BUY", trade_symbol, qty, price, "market", order_id,
                      f"{root} EMA cross up, RSI={sig['rsi']:.1f}")

    # SELL signal — flatten long front month
    elif sig["bearish_cross"] and sig["rsi"] > config.RSI_OVERSOLD and held > 0:
        logger.info("SIGNAL SELL %s x%d", trade_symbol, held)
        result = tc.place_futures_order(account_id, trade_symbol, "sell", held)
        if result:
            _mark_signaled(trade_symbol)
            order_id = result.get("order", {}).get("id")
            log_trade("SELL", trade_symbol, held, price, "market", order_id,
                      f"{root} EMA cross down, RSI={sig['rsi']:.1f}")


def _open_option(account_id, occ_symbol, side, price, symbol, exp, strike, opt_type, sig):
    qty = config.OPTIONS_CONTRACTS
    logger.info("SIGNAL %s %s x%d", side.upper(), occ_symbol, qty)
    result = tc.place_option_order(account_id, occ_symbol, side, qty)
    if result:
        order_id = result.get("order", {}).get("id")
        log_trade(side.upper(), occ_symbol, qty, price, "market", order_id,
                  f"{symbol} EMA cross, RSI={sig['rsi']:.1f}, strike={strike} {opt_type} exp={exp}")
    return result


def _close_option(account_id, occ_symbol, held, price, symbol, exp, strike, opt_type, sig):
    logger.info("SIGNAL SELL_TO_CLOSE %s x%d", occ_symbol, held)
    result = tc.place_option_order(account_id, occ_symbol, "sell_to_close", held)
    if result:
        order_id = result.get("order", {}).get("id")
        log_trade("SELL_TO_CLOSE", occ_symbol, held, price, "market", order_id,
                  f"{symbol} reversal, RSI={sig['rsi']:.1f}, strike={strike} {opt_type} exp={exp}")
    return result
