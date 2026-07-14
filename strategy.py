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

import json
import logging
import math
import os
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


def _shares_to_buy(price: float, equity: Optional[float]) -> int:
    """Size a stock entry at EQUITY_PER_TRADE_PCT of current account equity.
    Returns 0 (caller skips the trade) when price or equity is unusable, so a
    failed balance read can never place a mis-sized order — safety over blind
    sizing."""
    if price <= 0 or not equity or equity <= 0:
        return 0
    position_size = equity * config.EQUITY_PER_TRADE_PCT
    return max(1, math.floor(position_size / price))


def _open_position_count(positions: list[dict]) -> int:
    """Number of positions currently held (non-zero qty).

    KNOWN LIMITATION: `positions` is fetched once per cycle (main._run_cycle) and
    is not refreshed after an order fills mid-cycle. If several symbols cross in
    the same cycle, this count won't reflect fills placed earlier in that cycle,
    so the MAX_POSITIONS cap can be momentarily exceeded by the number of
    same-cycle entries. Accepted for now; the skip log below makes it visible."""
    return sum(1 for p in positions if int(p.get("quantity", 0)) != 0)


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


# ── Trailing Stop (bot-managed, persisted to config.STOP_PRICE_FILE) ──────────
# Per-position ATR trailing-stop state survives restarts via a JSON file keyed by
# symbol (schema documented in config.py). Checked every cycle BEFORE the EMA
# signal so a same-day entry can still stop out. Paper-trading choice; swap to a
# broker-native Sell Stop order when we go live.

_STOPS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           config.STOP_PRICE_FILE)
_MOM_ENTRIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 config.MOMENTUM_ENTRY_FILE)

# Observability: safety-net / signal counters this process lifetime. Every safety
# net gets a counter so we can tell whether it's still earning its keep.
_stop_exits = 0
_momentum_align_entries = 0


def _load_json(path: str) -> dict:
    """Read a persisted JSON dict. Returns {} on any problem (missing file,
    malformed JSON, non-dict) so a corrupt/absent file degrades gracefully rather
    than crashing the cycle."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("State file %s unreadable (%s) — treating as empty.", path, exc)
        return {}


def _save_json(path: str, data: dict) -> None:
    """Atomically persist a JSON dict (temp file + os.replace) so a crash
    mid-write can never leave a half-written, unparseable file."""
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        logger.error("Could not write state file %s: %s", path, exc)


def _load_stops() -> dict:
    return _load_json(_STOPS_PATH)


def _save_stops(stops: dict) -> None:
    _save_json(_STOPS_PATH, stops)


def _live_price(symbol: str) -> Optional[float]:
    """Latest trade price from a live quote, or None if unavailable. Callers fall
    back to the daily-bar close so a quote blip degrades the stop rather than
    disabling it."""
    q = tc.get_quote(symbol)
    if q:
        last = q.get("last") or q.get("bid")
        if last:
            try:
                return float(last)
            except (TypeError, ValueError):
                return None
    return None


def _cost_basis(positions: list[dict], symbol: str) -> Optional[float]:
    for p in positions:
        if p.get("symbol") == symbol:
            return p.get("cost_basis")
    return None


def _bootstrap_stop(symbol: str, held: int, sig: dict, positions: list[dict],
                    price: float) -> Optional[dict]:
    """Build a stop record for a pre-existing position we're adopting (no prior
    record). Entry is estimated from cost_basis/qty; ATR is computed now; the
    high-water seed is max(entry, current) so the stop is as tight as possible
    without an immediate exit. Returns None if ATR is unavailable."""
    atr = sig.get("atr")
    if atr is None or atr <= 0:
        logger.warning("STOP BOOTSTRAP %s skipped: ATR unavailable", symbol)
        return None
    basis = _cost_basis(positions, symbol)
    entry = (basis / held) if (basis and held) else price
    high_water = max(entry, price)
    stop = high_water - config.STOP_LOSS_ATR_MULT * atr
    logger.info("STOP BOOTSTRAP %s entry≈%.2f atr=%.2f stop=%.2f "
                "(adopted pre-existing position)", symbol, entry, atr, stop)
    return {
        "entry_price":  round(entry, 4),
        "atr_at_entry": round(atr, 4),
        "high_water":   round(high_water, 4),
        "stop_price":   round(stop, 4),
        "opened":       date.today().isoformat(),
        "bootstrapped": True,
    }


def _arm_stop_on_entry(symbol: str, entry_price: float, atr: Optional[float]) -> None:
    """Create a fresh stop record after a BUY fills. No-op with a warning if ATR
    is unavailable (equities always carry high/low, so this should never fire)."""
    if atr is None or atr <= 0:
        logger.warning("Could not arm stop for %s: ATR unavailable — position is "
                       "UNPROTECTED until bootstrap re-arms it.", symbol)
        return
    stop = entry_price - config.STOP_LOSS_ATR_MULT * atr
    stops = _load_stops()
    stops[symbol] = {
        "entry_price":  round(entry_price, 4),
        "atr_at_entry": round(atr, 4),
        "high_water":   round(entry_price, 4),
        "stop_price":   round(stop, 4),
        "opened":       date.today().isoformat(),
        "bootstrapped": False,
    }
    _save_stops(stops)
    logger.info("STOP ARMED %s entry=%.2f atr=%.2f stop=%.2f",
                symbol, entry_price, atr, stop)


def _clear_stop(symbol: str) -> None:
    """Drop a symbol's stop record (called when we exit the position)."""
    stops = _load_stops()
    if symbol in stops:
        del stops[symbol]
        _save_stops(stops)


def reconcile_stops(positions: list[dict]) -> None:
    """Prune stop records for symbols we no longer hold. Called once per cycle.

    Guarded on an empty positions list: get_positions() returns [] on API error,
    and pruning against that would wipe every stop, then re-bootstrap next cycle
    with a reset high-water — silently loosening ratcheted stops. Skipping prune
    on empty leaves stale records inert for a cycle (harmless)."""
    if not positions:
        return
    held = {p.get("symbol") for p in positions
            if int(p.get("quantity", 0)) != 0 and p.get("symbol")}
    stops = _load_stops()
    stale = [s for s in stops if s not in held]
    for s in stale:
        del stops[s]
        logger.info("STOP PRUNE %s: no longer held — dropping stop record", s)
    if stale:
        _save_stops(stops)


def _check_and_trail_stop(symbol: str, held: int, sig: dict,
                          account_id: str, positions: list[dict]) -> bool:
    """Update the trailing stop for a held position and exit if breached.

    Returns True iff a stop-exit order was placed (caller then returns, skipping
    signal logic for the cycle). False = no exit; continue to EMA-cross logic."""
    global _stop_exits

    price = _live_price(symbol)
    if price is None:
        price = sig["close"]          # daily-bar fallback — degraded, not disabled

    stops = _load_stops()
    rec = stops.get(symbol)
    if rec is None:
        rec = _bootstrap_stop(symbol, held, sig, positions, price)
        if rec is None:
            return False              # no ATR → can't arm a stop this cycle
    stops[symbol] = rec               # ensure present (bootstrap path)

    # Ratchet: high-water and stop only ever rise — never lower the stop.
    rec["high_water"] = round(max(rec["high_water"], price), 4)
    new_stop = rec["high_water"] - config.STOP_LOSS_ATR_MULT * rec["atr_at_entry"]
    rec["stop_price"] = round(max(rec["stop_price"], new_stop), 4)

    if price <= rec["stop_price"]:
        logger.warning("STOP-LOSS EXIT %s x%d @ %.2f (stop=%.2f entry=%.2f "
                       "high_water=%.2f) — exit #%d",
                       symbol, held, price, rec["stop_price"],
                       rec["entry_price"], rec["high_water"], _stop_exits + 1)
        result = tc.place_equity_order(account_id, symbol, "sell", held)
        if result:
            _stop_exits += 1
            stops.pop(symbol, None)
            _save_stops(stops)
            _mark_signaled(symbol)    # block a same-day re-buy on the next cross
            order_id = result.get("order", {}).get("id")
            log_trade("SELL", symbol, held, price, "market", order_id,
                      f"trailing stop hit @ {rec['stop_price']:.2f}")
            return True
        logger.error("STOP-LOSS EXIT %s: sell order failed — retrying next cycle",
                     symbol)

    _save_stops(stops)                # persist ratcheted high-water/stop progress
    return False


# ── Momentum alignment latch (one-shot entry per rotation) ────────────────────
# Momentum-slot names are already trending when added, so they never fire a fresh
# EMA cross. We give them one "enter on alignment" shot per rotation; the latch
# below (a separate file — it must survive stop-out exits, unlike a stop record)
# records which rotation we entered on so a stop-out can't trigger an immediate
# re-buy. Re-arms automatically when the rotation's `generation` id changes.

def _momentum_entry_taken(symbol: str, generation: str) -> bool:
    """True if we've already taken our one alignment entry for `symbol` in the
    current rotation. A changed `generation` (new twice-monthly screen) re-arms."""
    rec = _load_json(_MOM_ENTRIES_PATH).get(symbol)
    return bool(rec and rec.get("generation") == generation)


def _record_momentum_entry(symbol: str, generation: str) -> None:
    entries = _load_json(_MOM_ENTRIES_PATH)
    entries[symbol] = {"generation": generation, "entered": date.today().isoformat()}
    _save_json(_MOM_ENTRIES_PATH, entries)


def reconcile_momentum_entries(momentum_symbols) -> None:
    """Prune latch records for names no longer in the momentum slot. Once per
    cycle. Guarded on an empty slot (screen failure returns []) so a blip can't
    wipe latches — mirrors reconcile_stops."""
    if not momentum_symbols:
        return
    current = set(momentum_symbols)
    entries = _load_json(_MOM_ENTRIES_PATH)
    stale = [s for s in entries if s not in current]
    for s in stale:
        del entries[s]
        logger.info("MOMENTUM LATCH PRUNE %s: no longer in slot — dropping latch", s)
    if stale:
        _save_json(_MOM_ENTRIES_PATH, entries)


# ── Stock Strategy ────────────────────────────────────────────────────────────


def _enter_long(symbol: str, sig: dict, price: float, account_id: str,
                positions: list[dict], equity: Optional[float], reason: str) -> bool:
    """Shared long-entry path for both the fresh-cross and momentum-alignment
    signals: enforce MAX_POSITIONS, size at EQUITY_PER_TRADE_PCT, place the buy,
    and on a filled order mark the symbol signaled, log the trade, and arm the
    trailing stop. Returns True iff an order was placed and accepted."""
    open_count = _open_position_count(positions)
    if open_count >= config.MAX_POSITIONS:
        logger.info("Skip BUY %s: %d/%d positions open (max reached)",
                    symbol, open_count, config.MAX_POSITIONS)
        return False
    qty = _shares_to_buy(price, equity)
    if qty < 1:
        logger.warning("Skip BUY %s: could not size order (equity=%s price=%.2f)",
                       symbol, equity, price)
        return False
    logger.info("SIGNAL BUY %s x%d (~$%.0f, %.0f%% of $%.0f equity, %d/%d open) — %s",
                symbol, qty, qty * price,
                config.EQUITY_PER_TRADE_PCT * 100, equity or 0.0,
                open_count, config.MAX_POSITIONS, reason)
    result = tc.place_equity_order(account_id, symbol, "buy", qty)
    if result:
        _mark_signaled(symbol)
        order_id = result.get("order", {}).get("id")
        log_trade("BUY", symbol, qty, price, "market", order_id, reason)
        _arm_stop_on_entry(symbol, price, sig.get("atr"))
        return True
    return False

def evaluate_stock(symbol: str, account_id: str, positions: list[dict],
                   equity: Optional[float],
                   is_momentum: bool = False, momentum_generation: str = "") -> None:
    global _momentum_align_entries

    history = tc.get_historical(symbol, days=90)
    if not history:
        return

    sig = ind.compute_indicators(
        history,
        config.MA_SHORT_PERIOD,
        config.MA_LONG_PERIOD,
        config.RSI_PERIOD,
        config.STOP_LOSS_ATR_PERIOD,
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

    # Trailing stop: checked BEFORE the daily-signal gate and the EMA logic, so a
    # position bought today can still stop out the same day.
    if held > 0 and config.USE_TRAILING_STOP:
        if _check_and_trail_stop(symbol, held, sig, account_id, positions):
            return                      # stop fired — exited, skip signal logic

    if _already_signaled_today(symbol):
        return

    # BUY signal — fresh EMA cross (all symbols)
    if sig["bullish_cross"] and sig["rsi"] < config.RSI_OVERBOUGHT and held == 0:
        _enter_long(symbol, sig, price, account_id, positions, equity,
                    reason=f"EMA cross up, RSI={sig['rsi']:.1f}")

    # BUY signal — momentum alignment (momentum slot only, one-shot per rotation).
    # Reached only when there was NO fresh cross (elif), so a genuine cross always
    # takes the standard path; this is the fallback for names already trending when
    # the screen added them. The latch is consumed only on a *placed* order, so a
    # MAX_POSITIONS block leaves the shot available to retry when a slot frees.
    elif (is_momentum and held == 0 and config.USE_MOMENTUM_ALIGNMENT
          and sig["ema_short"] > sig["ema_long"]
          and config.MOMENTUM_ALIGN_RSI_MIN <= sig["rsi"] <= config.MOMENTUM_ALIGN_RSI_MAX
          and not _momentum_entry_taken(symbol, momentum_generation)):
        if _enter_long(symbol, sig, price, account_id, positions, equity,
                       reason=f"momentum alignment entry, RSI={sig['rsi']:.1f}"):
            _momentum_align_entries += 1
            _record_momentum_entry(symbol, momentum_generation)
            logger.info("MOMENTUM ALIGNMENT ENTRY %s (gen=%s) — align entries #%d",
                        symbol, momentum_generation or "<none>", _momentum_align_entries)

    # SELL signal
    elif sig["bearish_cross"] and sig["rsi"] > config.RSI_OVERSOLD and held > 0:
        logger.info("SIGNAL SELL %s x%d", symbol, held)
        result = tc.place_equity_order(account_id, symbol, "sell", held)
        if result:
            _mark_signaled(symbol)
            order_id = result.get("order", {}).get("id")
            log_trade("SELL", symbol, held, price, "market", order_id,
                      f"EMA cross down, RSI={sig['rsi']:.1f}")
            _clear_stop(symbol)


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
