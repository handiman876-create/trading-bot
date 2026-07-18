"""
Signal generation and order execution for stocks and options.

Stock signals  — EMA crossover + RSI confirmation:
  BUY         when short EMA crosses above long EMA  AND  RSI < overbought
  SELL        when short EMA crosses below long EMA  AND  RSI > oversold (long held)
  SELL SHORT  on a death cross (any effective-watchlist name, ENABLE_SHORTING) when flat —
              sized like a long, stop ABOVE entry ratcheting DOWN
  BUY TO COVER on a bullish cross while short (RSI < overbought)

Options signals — same crossover applied to the underlying:
  BUY_TO_OPEN  call when bullish cross + RSI < overbought
  BUY_TO_OPEN  put  when bearish cross + RSI > oversold
  Existing positions are closed on the opposite cross.
"""

import json
import logging
import math
import os
import time
from datetime import date
from typing import Optional

import config
import tradestation_client as tc
import market_hours as mh
import futures_market_hours as fmh
import indicators as ind
from trade_logger import log_trade

logger = logging.getLogger(__name__)

# Tracks the last date a signal fired per symbol, preventing the daily EMA
# signal from re-triggering on every 60-second poll within the same day.
#
# SPLIT by order side, because one shared gate silently blocked EXITS: a name
# bought at 9:30 could not be sold for the rest of that day, even as its stop
# ran. Buy-side ops (BUY, BUY_TO_COVER) check the buy gate; sell-side ops
# (SELL, SELL_SHORT) check the sell gate. Neither blocks the other, so an
# entry can always be exited the same day.
#
# Kept as date-keyed dicts rather than sets so they self-expire on the date
# comparison — a set would need a midnight reset hook that could be missed.
_signaled_buy_today:  dict[str, str] = {}
_signaled_sell_today: dict[str, str] = {}


def _already_bought_today(symbol: str) -> bool:
    return _signaled_buy_today.get(symbol) == date.today().isoformat()


def _already_sold_today(symbol: str) -> bool:
    return _signaled_sell_today.get(symbol) == date.today().isoformat()


def _mark_bought(symbol: str) -> None:
    _signaled_buy_today[symbol] = date.today().isoformat()


def _mark_sold(symbol: str) -> None:
    _signaled_sell_today[symbol] = date.today().isoformat()


# ── Exit conditions — STATE, not edge ─────────────────────────────────────────
# `bearish_cross`/`bullish_cross` are EDGES: true only on the single bar where
# the previous CLOSED bar sat one side of the crossover and the current bar sits
# the other. An edge is a memory of a transition, so it can be missed — and once
# missed it is gone forever, because the next bar's `prev` already reflects the
# new state. Anything that stops us observing that exact bar (a still-forming
# bar, a restart, an outage, a same-day round trip) silently strands the
# position with no exit but its stop. That is what happened to HCA and QQQ:
# both crossed up and back down inside one live bar, so relative to yesterday's
# CLOSE they never transitioned, no bearish edge was ever generated, and neither
# could exit on a cross again at any point in the future.
#
# A state is re-derived from current data every poll and cannot be missed. So:
# ENTRIES are edges (they need a trigger — a reason to act now and not
# yesterday); EXITS are states (if the condition holds and we are still in the
# position, we are wrong to be there, and why we missed the transition is
# irrelevant). Entry paths below deliberately keep their edges.

def _bearish_state(sig: dict) -> bool:
    """Fast EMA below slow — the trend state a long should not be held in."""
    return sig["ema_short"] < sig["ema_long"]


def _bullish_state(sig: dict) -> bool:
    """Fast EMA above slow — the trend state a short should not be held in."""
    return sig["ema_short"] > sig["ema_long"]


def _exit_long_signal(sig: dict) -> bool:
    """True when a long should be flat: bearish state, RSI not oversold.

    The RSI floor now DEFERS an exit rather than cancelling it — an edge-based
    exit blocked by RSI < oversold was lost permanently; this one fires as soon
    as RSI recovers, while the bearish state persists.

    The bare state predicates above exist because the OPTIONS closes never had an
    RSI gate (open with confirmation, close unconditionally on the opposite
    signal) and must not gain one: a contract decays, so refusing to close a
    losing call because RSI < 30 would hold it into theta. Options take the
    primitive; stocks and futures take this policy.
    """
    return _bearish_state(sig) and sig["rsi"] > config.RSI_OVERSOLD


def _exit_short_signal(sig: dict) -> bool:
    """True when a short should be flat: bullish state, RSI not overbought.
    Mirror of _exit_long_signal."""
    return _bullish_state(sig) and sig["rsi"] < config.RSI_OVERBOUGHT


# One ENTRY DELAYED log/count per name per day. Without this latch the counter
# would tick on every quiet poll inside the window (~20 symbols x 30 polls) and
# measure nothing but the clock; we want the number of real would-be entries the
# delay actually deferred, which is the only figure that says whether it earns
# its keep.
_entry_delay_logged: dict[str, str] = {}


def _note_entry_delayed(symbol: str, would_enter: bool) -> None:
    """Count an entry the post-open delay deferred. `would_enter` is the caller's
    answer to 'would this poll have placed an order but for the gate?'"""
    global _entries_delayed
    if not would_enter:
        return
    if _entry_delay_logged.get(symbol) == date.today().isoformat():
        return
    _entry_delay_logged[symbol] = date.today().isoformat()
    _entries_delayed += 1
    logger.info("ENTRY DELAYED %s — entry signal present but the daily bar is "
                "still forming (needs %d min after the session open). Re-checked "
                "every poll: it enters only if the signal survives the window "
                "(delayed entries #%d)",
                symbol, config.CROSS_ENTRY_DELAY_MINUTES, _entries_delayed)


def _note_state_only_exit(symbol: str, sig: dict, edge_key: str) -> None:
    """Count exits that fired on STATE with no matching EDGE this bar — i.e. the
    exits the old edge-based logic would have missed entirely. This counter is
    the fix's justification: if it stays at zero over a long run of real
    crossovers, the edge was adequate and this is dead weight; while it climbs,
    every increment is a position that would otherwise have been stranded."""
    global _state_only_exits
    if not sig.get(edge_key):
        _state_only_exits += 1
        logger.info("STATE-ONLY EXIT %s — no %s edge on this bar; edge-based "
                    "logic would have missed this exit (state-only exits #%d)",
                    symbol, edge_key, _state_only_exits)


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
_state_only_exits = 0
_entries_delayed = 0
_momentum_align_entries = 0
_short_entries = 0
_short_covers = 0
_latches_reconstructed = 0
_crisis_exits = 0
_sentiment_sector_blocks = 0
_profit_takes = 0


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


def _regime_atr_mult(regime: str) -> float:
    """The ATR multiple to ARM a new stop with, by market regime (risk_on 2.5 →
    crisis 1.0). Single source of truth for regime→width so the three arming
    sites don't each re-derive it. Unknown regimes fall back to the default
    STOP_LOSS_ATR_MULT (risk_on width)."""
    return config.ATR_MULT_BY_REGIME.get(regime, config.STOP_LOSS_ATR_MULT)


def _bootstrap_stop(symbol: str, held: int, sig: dict, positions: list[dict],
                    price: float, regime: str = "risk_on") -> Optional[dict]:
    """Build a stop record for a pre-existing position we're adopting (no prior
    record). Direction is inferred from the sign of `held` (negative = short).
    Entry is estimated from cost_basis/|qty|; ATR is computed now; the water-mark
    seed is chosen so the stop is as tight as possible without an immediate exit
    (max(entry, price) for longs, min(entry, price) for shorts). Returns None if
    ATR is unavailable."""
    atr = sig.get("atr")
    if atr is None or atr <= 0:
        logger.warning("STOP BOOTSTRAP %s skipped: ATR unavailable", symbol)
        return None
    basis = _cost_basis(positions, symbol)
    entry = (basis / abs(held)) if (basis and held) else price
    mult = _regime_atr_mult(regime)
    rec = {
        "entry_price":  round(entry, 4),
        "atr_at_entry": round(atr, 4),
        "atr_mult":     mult,
        "opened":       date.today().isoformat(),
        "bootstrapped": True,
    }
    if held < 0:                                   # short
        low_water = min(entry, price)
        stop = low_water + mult * atr
        rec.update({"direction": "short", "low_water": round(low_water, 4),
                    "stop_price": round(stop, 4)})
    else:                                          # long
        high_water = max(entry, price)
        stop = high_water - mult * atr
        rec.update({"direction": "long", "high_water": round(high_water, 4),
                    "stop_price": round(stop, 4)})
    logger.info("STOP BOOTSTRAP %s %s entry≈%.2f atr=%.2f mult=%.1fx stop=%.2f "
                "(adopted pre-existing position, regime=%s)",
                symbol, rec["direction"], entry, atr, mult, stop, regime)
    return rec


def _arm_stop_on_entry(symbol: str, entry_price: float, atr: Optional[float],
                       direction: str = "long", regime: str = "risk_on") -> None:
    """Create a fresh stop record after a BUY (long) or SELLSHORT (short) fills.
    The stop WIDTH is the regime's ATR multiple (risk_on 2.5 → crisis 1.0),
    persisted as "atr_mult" and reused for all later trailing so the width is
    fixed at entry. A short's stop sits ABOVE entry (entry + mult*atr) and will
    ratchet DOWN; a long's sits below and ratchets up. No-op with a warning if ATR
    is unavailable (equities always carry high/low, so this should never fire)."""
    # TODO: callers pass the SIGNAL-bar close (sig["close"]), not the actual fill
    # price — the order response carries no fill price to read (a market order is
    # accepted async, and _place_order returns only the OrderID). Market fills
    # typically arrive at or above sig["close"], so entry_price lands low and the
    # stop sits slightly lower than intended: under-protective, not over. Measured
    # 2026-07-15: DDOG 254.64 armed vs 256.34 basis (+1.70/sh), LII 559.87 vs
    # 568.03 (+8.16/sh).
    # Fix: reconcile entry_price from cost_basis/qty on a later cycle (the
    # arithmetic already exists in _bootstrap_stop) WITHOUT lowering an
    # already-ratcheted stop_price — the ratchet is monotonic by design.
    # Re-measure first: every stop armed so far predates CROSS_ENTRY_DELAY_MINUTES
    # going live (2026-07-15 21:26). LII was armed at 09:30:10 ET off a daily bar
    # holding seconds of data, so its gap is inflated; the 30-min delay should
    # shrink this on its own. Confirm the size on post-delay entries before
    # prioritising.
    # See memory: project_stop_armed_at_signal_price
    if atr is None or atr <= 0:
        logger.warning("Could not arm stop for %s: ATR unavailable — position is "
                       "UNPROTECTED until bootstrap re-arms it.", symbol)
        return
    mult = _regime_atr_mult(regime)
    rec = {
        "entry_price":  round(entry_price, 4),
        "atr_at_entry": round(atr, 4),
        "atr_mult":     mult,
        "opened":       date.today().isoformat(),
        "bootstrapped": False,
        "direction":    direction,
    }
    if direction == "short":
        stop = entry_price + mult * atr
        rec["low_water"] = round(entry_price, 4)
    else:
        stop = entry_price - mult * atr
        rec["high_water"] = round(entry_price, 4)
    rec["stop_price"] = round(stop, 4)
    stops = _load_stops()
    stops[symbol] = rec
    _save_stops(stops)
    logger.info("STOP ARMED %s %s entry=%.2f atr=%.2f mult=%.1fx stop=%.2f (regime=%s)",
                symbol, direction, entry_price, atr, mult, stop, regime)


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
                          account_id: str, positions: list[dict],
                          regime: str = "risk_on") -> bool:
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
        rec = _bootstrap_stop(symbol, held, sig, positions, price, regime)
        if rec is None:
            return False              # no ATR → can't arm a stop this cycle
    stops[symbol] = rec               # ensure present (bootstrap path)

    direction = rec.get("direction", "long")   # legacy records (no key) are longs
    entry = rec.get("entry_price")

    # Base trail width = the multiple this position was ARMED with (persisted at
    # entry by regime), NOT the live regime — a position's stop width is fixed at
    # entry, so a later regime change only affects NEW entries. Legacy records with
    # no "atr_mult" fall back to STOP_LOSS_ATR_MULT (2.5), unchanged.
    #
    # VIX regime stop adjustments layer ON TOP of that base — both hold the monotonic
    # ratchet (they only ever move a stop favorably, never loosen it) and both are
    # IMMEDIATE: this runs every cycle for every held position, BEFORE the entry gate,
    # so a mid-day VIX spike re-stops open positions on the next poll.
    #   defensive → tighten the trail to 1.5x ATR on a >3% loser (overrides base)
    #   crisis    → floor the stop at breakeven (entry), applied per-branch below
    mult = rec.get("atr_mult", config.STOP_LOSS_ATR_MULT)
    if regime == "defensive" and entry:
        drawdown = ((price - entry) / entry) if direction == "short" \
                   else ((entry - price) / entry)
        if drawdown > config.VIX_DEFENSIVE_DRAWDOWN:
            mult = config.VIX_DEFENSIVE_ATR_MULT
            logger.info("DEFENSIVE stop tighten %s: down %.1f%% -> %.1fx ATR",
                        symbol, drawdown * 100, mult)
    mult_atr = mult * rec["atr_at_entry"]
    # Crisis breakeven floor is armed-only (shadow logs the regime, changes nothing).
    crisis_floor = (regime == "crisis" and not config.VIX_CRISIS_SHADOW and bool(entry))

    if direction == "short":
        # Ratchet DOWN: low-water and stop only ever fall — never raise the stop.
        rec["low_water"] = round(min(rec["low_water"], price), 4)
        new_stop = rec["low_water"] + mult_atr
        if crisis_floor:
            new_stop = min(new_stop, entry)      # crisis: cap short stop at breakeven
        rec["stop_price"] = round(min(rec["stop_price"], new_stop), 4)
        water = rec["low_water"]
        breached = price >= rec["stop_price"]     # price rose into the stop
        exit_side, exit_qty = "buy_to_cover", abs(held)
        exit_action = "BUY_TO_COVER"
    else:
        # Ratchet UP: high-water and stop only ever rise — never lower the stop.
        rec["high_water"] = round(max(rec["high_water"], price), 4)
        new_stop = rec["high_water"] - mult_atr
        if crisis_floor:
            new_stop = max(new_stop, entry)      # crisis: floor long stop at breakeven
        rec["stop_price"] = round(max(rec["stop_price"], new_stop), 4)
        water = rec["high_water"]
        breached = price <= rec["stop_price"]     # price fell into the stop
        exit_side, exit_qty = "sell", held
        exit_action = "SELL"

    if breached:
        logger.warning("STOP-LOSS EXIT %s %s x%d @ %.2f (stop=%.2f entry=%.2f "
                       "water=%.2f) — exit #%d",
                       symbol, direction, exit_qty, price, rec["stop_price"],
                       rec["entry_price"], water, _stop_exits + 1)
        result = tc.place_equity_order(account_id, symbol, exit_side, exit_qty)
        if result:
            _stop_exits += 1
            stops.pop(symbol, None)
            _save_stops(stops)
            # Mark BOTH gates: a stop-out should block every same-day signal for
            # this name (the old single gate did exactly that). The buy mark is
            # the one that matters — it blocks the re-entry this comment has
            # always been about — but marking only the exit side would leave a
            # stopped-out name free to re-enter on the next cross the same day.
            _mark_bought(symbol)
            _mark_sold(symbol)
            order_id = result.get("order", {}).get("id")
            log_trade(exit_action, symbol, exit_qty, price, "market", order_id,
                      f"trailing stop hit @ {rec['stop_price']:.2f}")
            return True
        logger.error("STOP-LOSS EXIT %s: %s order failed — retrying next cycle",
                     symbol, exit_side)

    _save_stops(stops)                # persist ratcheted water/stop progress
    return False


# ── Profit taking (scale out of a winner) ─────────────────────────────────────


def _maybe_take_profit(symbol: str, held: int, sig: dict, account_id: str) -> bool:
    """Sell config.PROFIT_TAKE_FRACTION of a winning long once it is up
    >= PROFIT_TAKE_PCT from entry AND RSI >= PROFIT_TAKE_RSI_MIN. One-shot per
    position: the `profit_taken` flag in the stop record guards re-firing (a
    missing flag reads as False — back-compat with records predating this rule).
    The trailing stop record is deliberately KEPT so the remaining shares stay
    protected. De-risking, so it runs ungated like the stop and state exits.

    Entry basis comes from the stop record's entry_price; with no record (stops
    disabled, or a name we can't size the gain for) it is a no-op. Returns True
    iff a partial-sell order was placed, in which case the caller returns and
    skips the rest of the cycle for this name (mirrors _check_and_trail_stop)."""
    global _profit_takes
    if not config.ENABLE_PROFIT_TAKING or held <= 0:
        return False
    stops = _load_stops()
    rec = stops.get(symbol)
    if not rec:
        return False                         # no entry basis -> cannot size the gain
    entry = rec.get("entry_price")
    if not entry or entry <= 0:
        return False
    if rec.get("profit_taken", False):       # missing flag == not yet taken
        return False
    price = sig["close"]
    gain = (price - entry) / entry
    if gain < config.PROFIT_TAKE_PCT or sig["rsi"] < config.PROFIT_TAKE_RSI_MIN:
        return False
    sell_qty = math.floor(held * config.PROFIT_TAKE_FRACTION)
    if sell_qty < 1:
        return False                         # position too small to halve — leave it

    logger.info("PROFIT TAKE %s x%d (+%.1f%% from entry, RSI=%.1f)",
                symbol, sell_qty, gain * 100, sig["rsi"])
    result = tc.place_equity_order(account_id, symbol, "sell", sell_qty)
    if not result:
        logger.error("PROFIT TAKE %s: sell order failed — retry next cycle", symbol)
        return False
    _profit_takes += 1
    rec["profit_taken"] = True               # latch BEFORE anything else can re-read
    stops[symbol] = rec
    _save_stops(stops)                       # record kept -> remainder keeps its stop
    order_id = result.get("order", {}).get("id")
    log_trade("SELL", symbol, sell_qty, price, "market", order_id,
              f"profit take (+{gain * 100:.1f}% from entry, RSI={sig['rsi']:.1f})")
    return True


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


def reconcile_momentum_entries(momentum_symbols, positions: list[dict],
                               generation: str) -> None:
    """Reconcile the latch file against the slot and the broker, once per cycle.

    Two directions:
      PRUNE       — drop latches for names no longer in the momentum slot.
      RECONSTRUCT — re-create a missing latch for a momentum name we HOLD.

    Reconstruct exists because the latch file is deletable out from under a
    running bot: test_exit_state.py's _reset() os.remove()d the live file twice
    (fixed in f08931f), and on 2026-07-15 that wipe plus a 503 the next day cost
    us double-sized CRL and LII. A held momentum name with no latch is proof we
    entered it — the record was lost, not never written.

    `positions` is the authority, deliberately NOT stop_prices.json: the same
    _reset() deletes BOTH files, so the stop records are empty in exactly the
    scenario this defends against (the bootstrapped=true flags on AAPL/AMZN/META/
    NVDA are the scar). The broker is the only witness that survives.

    Per-cycle rather than at startup, also deliberately: the 07-15 wipe landed
    ~27 minutes AFTER the last process start, and the bot then ran unrestarted
    through the 07-16 doubling. A startup-only check would have slept through it.

    Guarded on an empty slot (screen failure returns []) so a blip can't wipe
    latches — mirrors reconcile_stops."""
    global _latches_reconstructed

    if not momentum_symbols:
        return
    current = set(momentum_symbols)
    entries = _load_json(_MOM_ENTRIES_PATH)
    dirty = False

    stale = [s for s in entries if s not in current]
    for s in stale:
        del entries[s]
        dirty = True
        logger.info("MOMENTUM LATCH PRUNE %s: no longer in slot — dropping latch", s)

    # Reconstruct ONLY where there is no record at all. An existing record with an
    # older generation is meaningful and must not be overwritten: a name held from
    # rotation N-1 into rotation N legitimately has an unused shot for N (the latch
    # re-arms per rotation), and stamping it with N would silently consume a
    # re-entry the strategy is entitled to after a stop-out. Reconstructed records
    # take the CURRENT generation, which is mildly conservative in the other
    # direction — a stop-out during recovery won't re-buy this rotation — and only
    # ever applies to state we already know is corrupt.
    held = {p.get("symbol") for p in positions
            if int(p.get("quantity", 0)) != 0 and p.get("symbol") in current}
    for s in sorted(held - set(entries)):
        entries[s] = {"generation": generation, "entered": date.today().isoformat(),
                      "reconstructed": True}
        dirty = True
        _latches_reconstructed += 1
        logger.warning(
            "MOMENTUM LATCH RECONSTRUCTED %s (gen=%s) — held with no latch record; "
            "the latch was lost, not unwritten. Blocking re-entry this rotation "
            "(latches reconstructed #%d)",
            s, generation or "<none>", _latches_reconstructed)

    if dirty:
        _save_json(_MOM_ENTRIES_PATH, entries)


# ── Stock Strategy ────────────────────────────────────────────────────────────


def _enter_long(symbol: str, sig: dict, price: float, account_id: str,
                positions: list[dict], equity: Optional[float], reason: str,
                regime: str = "risk_on") -> bool:
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
        _mark_bought(symbol)
        order_id = result.get("order", {}).get("id")
        log_trade("BUY", symbol, qty, price, "market", order_id, reason)
        _arm_stop_on_entry(symbol, price, sig.get("atr"), regime=regime)
        return True
    return False


def _enter_short(symbol: str, sig: dict, price: float, account_id: str,
                 positions: list[dict], equity: Optional[float], reason: str,
                 regime: str = "risk_on") -> bool:
    """Short-entry path (any effective-watchlist name, fresh death cross): enforce MAX_POSITIONS,
    size like a long at EQUITY_PER_TRADE_PCT, place a SELLSHORT, and on a filled
    order mark the symbol signaled, log the trade, and arm the ABOVE-entry trailing
    stop. Mirrors _enter_long. Returns True iff an order was placed and accepted."""
    open_count = _open_position_count(positions)
    if open_count >= config.MAX_POSITIONS:
        logger.info("Skip SHORT %s: %d/%d positions open (max reached)",
                    symbol, open_count, config.MAX_POSITIONS)
        return False
    qty = _shares_to_buy(price, equity)          # same sizing as a long
    if qty < 1:
        logger.warning("Skip SHORT %s: could not size order (equity=%s price=%.2f)",
                       symbol, equity, price)
        return False
    logger.info("SIGNAL SELL_SHORT %s x%d (~$%.0f, %.0f%% of $%.0f equity, %d/%d open) — %s",
                symbol, qty, qty * price,
                config.EQUITY_PER_TRADE_PCT * 100, equity or 0.0,
                open_count, config.MAX_POSITIONS, reason)
    result = tc.place_equity_order(account_id, symbol, "sell_short", qty)
    if result:
        _mark_sold(symbol)
        order_id = result.get("order", {}).get("id")
        log_trade("SELL_SHORT", symbol, qty, price, "market", order_id, reason)
        _arm_stop_on_entry(symbol, price, sig.get("atr"), direction="short", regime=regime)
        return True
    return False


# ── VIX fear gauge / market regime ────────────────────────────────────────────
# One VIX quote drives a market-wide regime that gates entries (equities AND
# futures) and, at the extreme, tightens stops and de-risks the momentum slot.
# _get_market_regime is a PURE mapping (unit-tested at every boundary);
# current_regime wraps it with a 5-minute cache and a fail-OPEN path; note_regime
# does per-cycle logging (level, transitions, mode line) and counting.
_REGIMES = ("risk_on", "cautious", "defensive", "crisis", "unknown")
_regime_counts = {r: 0 for r in _REGIMES}
_vix_cache = {"ts": None, "vix": None, "regime": "risk_on"}
_last_logged_regime = None            # drives REGIME TRANSITION logging


def _get_market_regime(vix: Optional[float]) -> str:
    """Pure VIX → regime. Constants mark the CEILING of their namesake regime, so
    the original boundaries hold: risk_on <20, cautious 20-25, defensive 25-30,
    crisis >=30. vix=None → 'unknown' (caller fails open to risk_on)."""
    if vix is None:
        return "unknown"
    if vix >= config.VIX_DEFENSIVE:        # >= 30
        return "crisis"
    if vix >= config.VIX_CAUTIOUS:         # >= 25
        return "defensive"
    if vix >= config.VIX_NORMAL:           # >= 20
        return "cautious"
    return "risk_on"


def _is_extreme(vix: Optional[float]) -> bool:
    """True at/above the EXTREME sub-tier of crisis (VIX_CRISIS, 35)."""
    return vix is not None and vix >= config.VIX_CRISIS


def _apply_regime_rules(regime: str):
    """Map a regime to entry gates: (block_new_entries, block_momentum_align).
    Centralized so evaluate_stock and evaluate_future read identical logic and a
    rule change lands in exactly one place."""
    block_new_entries    = regime in ("defensive", "crisis")
    block_momentum_align = regime in ("cautious", "defensive", "crisis")
    return block_new_entries, block_momentum_align


# Fear ordering for the belt-&-suspenders VIX-vs-sentiment combination.
_REGIME_RANK = {"risk_on": 0, "unknown": 0, "cautious": 1, "defensive": 2, "crisis": 3}


def _more_fearful(a: str, b: str) -> str:
    """Return the more fearful of two regimes — the effective regime is the MORE
    fearful of the VIX regime and the Claude-sentiment regime (if either says fear,
    respect it)."""
    return a if _REGIME_RANK.get(a, 0) >= _REGIME_RANK.get(b, 0) else b


def current_regime(now: Optional[float] = None):
    """(vix, regime) for this cycle, refetching config.VIX_SYMBOL at most every
    VIX_CACHE_SECONDS.  Fail-OPEN: a failed/absent quote yields 'unknown', which
    every gate treats as risk_on — a VIX data glitch never blocks trading or
    liquidates.  ENABLE_VIX_FILTER False forces (None, 'risk_on').  `now` is
    injectable for tests."""
    if not config.ENABLE_VIX_FILTER:
        return None, "risk_on"
    t = now if now is not None else time.time()
    ts = _vix_cache["ts"]
    if ts is not None and (t - ts) < config.VIX_CACHE_SECONDS:
        return _vix_cache["vix"], _vix_cache["regime"]
    vix = tc.get_vix_level()
    regime = _get_market_regime(vix)
    if vix is None:
        logger.warning("VIX unavailable — regime unknown; failing OPEN (risk_on "
                       "gating this cycle)")
    _vix_cache.update({"ts": t, "vix": vix, "regime": regime})
    return vix, regime


def note_regime(vix: Optional[float], regime: str, vix_regime: Optional[str] = None,
                sent_regime: Optional[str] = None, fear=None, risks=None) -> None:
    """Per-cycle bookkeeping — call once per cycle from the run loop. `regime` is the
    EFFECTIVE (combined) regime; the optional vix_regime/sent_regime/fear/risks let it
    log a SENTIMENT OVERRIDE when Claude's read is strictly more fearful than the VIX
    read. Counts the effective regime, logs the level, flags transitions, and emits
    the human-readable mode line for the entry-gating regimes."""
    global _last_logged_regime
    _regime_counts[regime if regime in _regime_counts else "unknown"] += 1
    vtxt = f"{vix:.1f}" if isinstance(vix, (int, float)) else "n/a"
    extreme = " EXTREME" if _is_extreme(vix) else ""
    if _last_logged_regime is not None and regime != _last_logged_regime:
        logger.warning("REGIME TRANSITION %s -> %s (VIX=%s%s)",
                       _last_logged_regime, regime, vtxt, extreme)
    logger.info("VIX=%s regime=%s%s", vtxt, regime, extreme)
    if (sent_regime and vix_regime
            and _REGIME_RANK.get(sent_regime, 0) > _REGIME_RANK.get(vix_regime, 0)):
        logger.warning("SENTIMENT OVERRIDE: %s mode from Claude analysis "
                       "(fear=%s, VIX-regime=%s, risks: %s)", sent_regime, fear,
                       vix_regime, ", ".join(risks or []) or "n/a")
    if regime == "cautious":
        logger.info("CAUTIOUS MODE - skipping momentum alignment (VIX=%s)", vtxt)
    elif regime == "defensive":
        logger.info("DEFENSIVE MODE - no new entries (VIX=%s)", vtxt)
    elif regime == "crisis":
        logger.warning("CRISIS MODE%s [%s] - no entries; de-risking momentum slot; "
                       "stops -> breakeven (VIX=%s)", extreme,
                       "SHADOW" if config.VIX_CRISIS_SHADOW else "LIVE", vtxt)
    _last_logged_regime = regime


def evaluate_stock(symbol: str, account_id: str, positions: list[dict],
                   equity: Optional[float],
                   is_momentum: bool = False, momentum_generation: str = "",
                   regime: str = "risk_on",
                   blocked_symbols=frozenset()) -> None:
    global _momentum_align_entries, _short_entries, _short_covers, _entries_delayed
    global _crisis_exits, _sentiment_sector_blocks

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
    # position opened today can still stop out the same day. held != 0 covers both
    # longs (stop below) and shorts (stop above).
    if held != 0 and config.USE_TRAILING_STOP:
        if _check_and_trail_stop(symbol, held, sig, account_id, positions, regime):
            return                      # stop fired — exited, skip signal logic

    # CRISIS de-risk — force-exit a held momentum-slot name through the SAME SELL
    # path as a normal state exit, regardless of EMA state (the momentum slot is
    # the highest-risk bucket in a panic). Core names are kept; their stops move to
    # breakeven in _check_and_trail_stop above. Shadow only LOGS and falls through,
    # so normal signals still apply; armed sells and returns.
    if (regime == "crisis" and is_momentum and held > 0
            and not _already_sold_today(symbol)):
        if config.VIX_CRISIS_SHADOW:
            logger.warning("CRISIS would SELL momentum %s x%d (shadow — normal "
                           "signals still apply)", symbol, held)
        else:
            logger.warning("CRISIS de-risk SELL %s x%d", symbol, held)
            result = tc.place_equity_order(account_id, symbol, "sell", held)
            if result:
                _crisis_exits += 1
                _mark_sold(symbol)
                order_id = result.get("order", {}).get("id")
                log_trade("SELL", symbol, held, price, "market", order_id,
                          "VIX crisis de-risk")
                _clear_stop(symbol)
            else:
                logger.error("CRISIS SELL %s FAILED — retry next cycle", symbol)
            return

    # PROFIT TAKE — scale out of a winning long before the exit/entry logic.
    # De-risking, so like the stop and state exits it runs ungated by regime and
    # the entry delay. One-shot per position; the trailing stop stays on the
    # remainder. Placed after the stop check, before the exit signal.
    if held > 0 and _maybe_take_profit(symbol, held, sig, account_id):
        return

    # ── EXITS ─────────────────────────────────────────────────────────────────
    # Evaluated BEFORE the entry gate, and on state rather than an edge, so a
    # position can always leave: at the bell, mid-outage, or the same day it was
    # opened. The sell/buy gate below blocks only a DUPLICATE exit while an order
    # is in flight (held stays non-zero until it fills), never the first one.

    # SELL — close a long whenever the trend is bearish, not just on the crossing
    # bar. This is the HCA/QQQ fix.
    if held > 0 and _exit_long_signal(sig) and not _already_sold_today(symbol):
        logger.info("SIGNAL SELL %s x%d", symbol, held)
        result = tc.place_equity_order(account_id, symbol, "sell", held)
        if result:
            _mark_sold(symbol)
            order_id = result.get("order", {}).get("id")
            log_trade("SELL", symbol, held, price, "market", order_id,
                      f"EMA bearish, RSI={sig['rsi']:.1f}")
            _note_state_only_exit(symbol, sig, "bearish_cross")
            _clear_stop(symbol)
        return

    # COVER — close a short whenever the trend is bullish (mirror of SELL).
    if held < 0 and _exit_short_signal(sig) and not _already_bought_today(symbol):
        qty = abs(held)
        logger.info("SIGNAL BUY_TO_COVER %s x%d", symbol, qty)
        result = tc.place_equity_order(account_id, symbol, "buy_to_cover", qty)
        if result:
            _short_covers += 1
            _mark_bought(symbol)
            order_id = result.get("order", {}).get("id")
            log_trade("BUY_TO_COVER", symbol, qty, price, "market", order_id,
                      f"EMA bullish (cover), RSI={sig['rsi']:.1f}")
            _note_state_only_exit(symbol, sig, "bullish_cross")
            _clear_stop(symbol)
        return

    # ── ENTRIES ───────────────────────────────────────────────────────────────
    # One gate for every entry path below. The daily bar is still forming — at
    # 9:30:05 its EMAs are computed from seconds of data, which is how QQQ was
    # bought on a 0.017%-wide "cross" and HCA on a five-minute-old stub bar at
    # RSI 35. A stub bar is a stub bar whether it is read as an edge or a state,
    # so this gates the momentum path too. Exits above are deliberately outside
    # it: acting on noise costs an early exit, entering on noise costs capital.
    if not mh.entries_allowed():
        _note_entry_delayed(symbol, held == 0 and (
            sig["bullish_cross"] or (is_momentum and _bullish_state(sig))))
        return

    # One entry per name per day (what the old single gate actually protected).
    # A name that already traded today does not get re-entered on a later blip.
    if _already_bought_today(symbol) or _already_sold_today(symbol):
        return

    # VIX regime entry gates (centralized in _apply_regime_rules). Exits and stops
    # above are deliberately ungated — de-risking is always allowed; only ENTRIES
    # are throttled by fear. cautious blocks only momentum-alignment; defensive and
    # crisis block every new entry (fresh-cross longs, alignment, and shorts).
    block_new_entries, block_momentum_align = _apply_regime_rules(regime)

    # BUY signal — fresh EMA cross (all symbols)
    if (sig["bullish_cross"] and sig["rsi"] < config.RSI_OVERBOUGHT and held == 0
            and not block_new_entries):
        if symbol in blocked_symbols:
            _sentiment_sector_blocks += 1
            logger.info("SECTOR RISK: skipping %s long entry — sector rated high "
                        "(sentiment) #%d", symbol, _sentiment_sector_blocks)
        else:
            _enter_long(symbol, sig, price, account_id, positions, equity,
                        reason=f"EMA cross up, RSI={sig['rsi']:.1f}", regime=regime)

    # BUY signal — momentum alignment (momentum slot only, one-shot per rotation).
    # Reached only when there was NO fresh cross (elif), so a genuine cross always
    # takes the standard path; this is the fallback for names already trending when
    # the screen added them. The latch is consumed only on a *placed* order, so a
    # MAX_POSITIONS block — or the entry delay above — leaves the shot available
    # to retry once the bar has formed.
    elif (is_momentum and held == 0 and not block_momentum_align
          and config.USE_MOMENTUM_ALIGNMENT
          and sig["ema_short"] > sig["ema_long"]
          and config.MOMENTUM_ALIGN_RSI_MIN <= sig["rsi"] <= config.MOMENTUM_ALIGN_RSI_MAX
          and not _momentum_entry_taken(symbol, momentum_generation)):
        if symbol in blocked_symbols:
            _sentiment_sector_blocks += 1
            logger.info("SECTOR RISK: skipping %s momentum entry — sector rated high "
                        "(sentiment) #%d — latch preserved", symbol,
                        _sentiment_sector_blocks)
        elif _enter_long(symbol, sig, price, account_id, positions, equity,
                         reason=f"momentum alignment entry, RSI={sig['rsi']:.1f}",
                         regime=regime):
            _momentum_align_entries += 1
            _record_momentum_entry(symbol, momentum_generation)
            logger.info("MOMENTUM ALIGNMENT ENTRY %s (gen=%s) — align entries #%d",
                        symbol, momentum_generation or "<none>", _momentum_align_entries)

    # SHORT signal — fresh death cross, any name in the effective watchlist
    # (core ∪ momentum ∪ held). The loop only ever feeds effective-watchlist
    # symbols, so reaching here already means the bot actively watches this name;
    # momentum picks are now shortable too. Crisis is still blocked by
    # block_new_entries. Mirrors the long BUY: same RSI gate, same held==0.
    # Stays EDGE-based: it is an entry. On state it would re-short every poll.
    elif (sig["bearish_cross"] and sig["rsi"] > config.RSI_OVERSOLD and held == 0
          and config.ENABLE_SHORTING and not block_new_entries):
        if _enter_short(symbol, sig, price, account_id, positions, equity,
                        reason=f"EMA cross down (short), RSI={sig['rsi']:.1f}",
                        regime=regime):
            _short_entries += 1
            logger.info("SHORT ENTRY %s — short entries #%d", symbol, _short_entries)


# ── Options Strategy ──────────────────────────────────────────────────────────

def evaluate_option(
    symbol:     str,
    expiration: str,
    opt_type:   str,
    account_id: str,
    positions:  list[dict],
) -> None:
    global _entries_delayed

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

    # NOTE: the old single gate sat here, above BOTH branches, so a contract
    # opened today could not be closed today — the same defect as the equities
    # path. The gate now lives inside the open branch only; closes below are
    # never gated on it.
    is_call = opt_type.lower() == "call"

    # Open new position — an entry, so it waits for the bar to form like every
    # other entry path. Options run off the same underlying's daily bar, so a
    # 9:30:05 open would be bought on the same stub EMAs as QQQ was.
    if held == 0:
        if not mh.entries_allowed():
            _note_entry_delayed(occ_symbol, sig["bullish_cross"] if is_call
                                else sig["bearish_cross"])
            return
        if _already_bought_today(occ_symbol) or _already_sold_today(occ_symbol):
            return
        if is_call and sig["bullish_cross"] and sig["rsi"] < config.RSI_OVERBOUGHT:
            if _open_option(account_id, occ_symbol, "buy_to_open", opt_price,
                            symbol, expiration, strike, opt_type, sig):
                _mark_bought(occ_symbol)
        elif not is_call and sig["bearish_cross"] and sig["rsi"] > config.RSI_OVERSOLD:
            if _open_option(account_id, occ_symbol, "buy_to_open", opt_price,
                            symbol, expiration, strike, opt_type, sig):
                _mark_bought(occ_symbol)

    # Close existing position on the opposite STATE (not edge) — same fix as the
    # equities exits: a long call stranded by a missed bearish edge would ride to
    # expiry. A call is long the underlying, a put is short it, so they take the
    # long/short exit helpers respectively.
    elif held > 0:
        if is_call and _bearish_state(sig):
            if _close_option(account_id, occ_symbol, held, opt_price,
                             symbol, expiration, strike, opt_type, sig):
                _mark_sold(occ_symbol)
                _note_state_only_exit(occ_symbol, sig, "bearish_cross")
        elif not is_call and _bullish_state(sig):
            if _close_option(account_id, occ_symbol, held, opt_price,
                             symbol, expiration, strike, opt_type, sig):
                _mark_sold(occ_symbol)
                _note_state_only_exit(occ_symbol, sig, "bullish_cross")


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


def evaluate_future(root: str, account_id: str, positions: list[dict],
                    regime: str = "risk_on") -> None:
    global _entries_delayed

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

    qty = config.FUTURES_CONTRACTS

    # SELL — flatten the long on bearish STATE, before the entry gate, same as
    # equities. Uses the FUTURES clock: the ES daily bar runs 18:00 -> 17:00 ET,
    # so its unformed stub window is the evening reopen, not the 9:30 bell.
    if held > 0 and _exit_long_signal(sig) and not _already_sold_today(trade_symbol):
        logger.info("SIGNAL SELL %s x%d", trade_symbol, held)
        result = tc.place_futures_order(account_id, trade_symbol, "sell", held)
        if result:
            _mark_sold(trade_symbol)
            order_id = result.get("order", {}).get("id")
            log_trade("SELL", trade_symbol, held, price, "market", order_id,
                      f"{root} EMA bearish, RSI={sig['rsi']:.1f}")
            _note_state_only_exit(trade_symbol, sig, "bearish_cross")
        return

    if not fmh.entries_allowed():
        _note_entry_delayed(trade_symbol, held == 0 and sig["bullish_cross"])
        return

    if _already_bought_today(trade_symbol) or _already_sold_today(trade_symbol):
        return

    # VIX regime gate — futures have no momentum slot or bot-managed stop, so the
    # filter reduces to blocking new entries in defensive/crisis (the roll-flatten
    # and state exit above are de-risking and stay ungated).
    block_new_entries, _ = _apply_regime_rules(regime)

    # BUY signal — open long front month (EDGE: it is an entry)
    if (sig["bullish_cross"] and sig["rsi"] < config.RSI_OVERBOUGHT and held == 0
            and not block_new_entries):
        logger.info("SIGNAL BUY %s x%d", trade_symbol, qty)
        result = tc.place_futures_order(account_id, trade_symbol, "buy", qty)
        if result:
            _mark_bought(trade_symbol)
            order_id = result.get("order", {}).get("id")
            log_trade("BUY", trade_symbol, qty, price, "market", order_id,
                      f"{root} EMA cross up, RSI={sig['rsi']:.1f}")


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
