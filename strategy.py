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

# ── Cross hysteresis (minimum EMA separation) ─────────────────────────────────
# Every EMA comparison in this module — both STATES and EDGES, entries and exits,
# stocks, options and futures — funnels through the four wrappers below. That is
# deliberate: the gap rule lives in ONE place, so no site can drift out of sync
# with the others. Do NOT reintroduce a bare `sig["ema_short"] > sig["ema_long"]`
# or a bare `sig["bullish_cross"]` at a call site; use these.
#
# The raw `bullish_cross`/`bearish_cross` keys stay UNFILTERED in indicators.py
# on purpose: _note_state_only_exit compares state against the raw edge to
# justify its counter, and filtering the edge at source would silently inflate
# _state_only_exits by counting gap-suppressed edges as "missed" ones.

def _valid_ema_cross(ema_short: float, ema_long: float, price: float) -> bool:
    """True when the EMAs are separated by >= EMA_CROSS_MIN_GAP_PCT of price.

    Pure and side-effect free (the counter lives in _cross_gap_ok). A price of
    0/None means the gap cannot be normalised, so the answer is False: with no
    usable denominator we decline to call it a cross rather than guess.
    """
    if not price or price <= 0:
        return False
    return abs(ema_short - ema_long) / price >= config.EMA_CROSS_MIN_GAP_PCT


def _cross_gap_ok(sig: dict, symbol: str = "", what: str = "") -> bool:
    """_valid_ema_cross for a sig dict, counting the suppressions.

    Call ONLY once the raw condition is already true, so a False return means
    exactly one thing: a would-be signal the gap rule suppressed. That is what
    makes the counter mean something.
    """
    price = sig.get("close")
    if _valid_ema_cross(sig["ema_short"], sig["ema_long"], price):
        return True
    _note_cross_gap_block(symbol, sig, what, price)
    return False


def _bearish_state(sig: dict, symbol: str = "") -> bool:
    """Fast EMA below slow by a meaningful margin — the trend state a long should
    not be held in."""
    if sig["ema_short"] >= sig["ema_long"]:
        return False
    return _cross_gap_ok(sig, symbol, "bearish state")


def _bullish_state(sig: dict, symbol: str = "") -> bool:
    """Fast EMA above slow by a meaningful margin — the trend state a short
    should not be held in."""
    if sig["ema_short"] <= sig["ema_long"]:
        return False
    return _cross_gap_ok(sig, symbol, "bullish state")


# ── Cross persistence ─────────────────────────────────────────────────────────
# When a gap-valid entry cross was FIRST observed, per (symbol, direction). The
# edge key stays true for as long as the prior bar sits on the far side, so this
# tracks a state, not a one-poll spike: the moment the cross stops being valid the
# entry is cleared and the clock restarts from zero on the next appearance.
#
# In-memory ON PURPOSE. A restart forgets the clock and re-arms it, which delays
# an entry by up to CROSS_SUSTAIN_MINUTES but can never let an unproven cross
# through early. Failing toward "wait longer" is the safe direction for a rule
# whose whole job is waiting.
_cross_first_seen: dict[tuple[str, str], float] = {}
_cross_confirmed: set = set()


def _sustain_minutes() -> int:
    """The active persistence requirement, or 0 when the rule is off."""
    if not getattr(config, "ENABLE_CROSS_SUSTAIN", True):
        return 0
    return getattr(config, "CROSS_SUSTAIN_MINUTES", 0) or 0


def _cross_sustained(symbol: str, kind: str, what: str) -> bool:
    """True when a gap-valid entry cross has held CROSS_SUSTAIN_MINUTES.

    Call ONLY after the raw edge and the gap check have both passed, so PENDING
    and CONFIRMED describe real would-be signals. Mirrors _cross_gap_ok's
    contract.

    Three states, each logged exactly once per cross episode (not per poll — at a
    60s cadence a per-poll log would print ~30 lines per deferred entry and the
    counter would measure the poll rate rather than the rule):
      PENDING   first sighting; the clock starts
      CONFIRMED the cross survived the window and the signal is released
      BLOCK     the cross lapsed before the window closed — see _clear_cross_clock
    """
    need = _sustain_minutes()
    if need <= 0:
        return True

    key = (symbol or "<unnamed>", kind)
    now = time.time()
    first = _cross_first_seen.get(key)
    if first is None:
        first = _cross_first_seen[key] = now
        logger.info("SUSTAIN PENDING %s %s: cross seen, need %d min sustained "
                    "(0/%d min)", key[0], what, need, need)
        return False

    held_min = (now - first) / 60.0
    if held_min >= need:
        if key not in _cross_confirmed:
            _cross_confirmed.add(key)
            logger.info("SUSTAIN CONFIRMED %s %s: held %.1f min (>= %d), "
                        "firing signal", key[0], what, held_min, need)
        return True
    return False


def _clear_cross_clock(symbol: str, kind: str, what: str = "") -> None:
    """Forget a cross that is no longer valid.

    A cross that lapses BEFORE maturing is the thing this rule exists to stop, so
    that — not "deferred at least once" — is what _cross_sustain_blocks counts.
    One increment = one cross that appeared, failed to hold, and never fired.
    A cross cleared AFTER it already fired is just bookkeeping and counts nothing.
    """
    global _cross_sustain_blocks
    key = (symbol or "<unnamed>", kind)
    first = _cross_first_seen.pop(key, None)
    confirmed = key in _cross_confirmed
    _cross_confirmed.discard(key)
    if first is None or confirmed:
        return
    need = _sustain_minutes()
    if need <= 0:
        return
    held_min = (time.time() - first) / 60.0
    _cross_sustain_blocks += 1
    logger.info("SUSTAIN BLOCK %s %s: cross reversed after %.1f min, before the "
                "%d-min minimum — signal never fired (sustain blocks #%d)",
                key[0], what or kind, held_min, need, _cross_sustain_blocks)


def _clear_cross_clocks_for(symbol: str) -> None:
    """Drop both direction clocks for a symbol once a position exists. Without
    this a stale pre-entry clock would survive the whole trade and hand the next
    cross credit for time served before the position was ever opened."""
    for kind in ("bull", "bear"):
        key = (symbol or "<unnamed>", kind)
        _cross_first_seen.pop(key, None)
        _cross_confirmed.discard(key)


def _bullish_cross_edge(sig: dict, symbol: str = "") -> bool:
    """Golden-cross EDGE, gap-filtered and persistence-filtered. Entry paths use
    this, never the raw key. Exits take the STATE predicates and stay ungated."""
    if not sig.get("bullish_cross"):
        _clear_cross_clock(symbol, "bull", "bullish cross")
        return False
    if not _cross_gap_ok(sig, symbol, "bullish cross"):
        _clear_cross_clock(symbol, "bull", "bullish cross")
        return False
    return _cross_sustained(symbol, "bull", "bullish cross")


def _bearish_cross_edge(sig: dict, symbol: str = "") -> bool:
    """Death-cross EDGE, gap-filtered and persistence-filtered. Mirror of
    _bullish_cross_edge."""
    if not sig.get("bearish_cross"):
        _clear_cross_clock(symbol, "bear", "bearish cross")
        return False
    if not _cross_gap_ok(sig, symbol, "bearish cross"):
        _clear_cross_clock(symbol, "bear", "bearish cross")
        return False
    return _cross_sustained(symbol, "bear", "bearish cross")


def _exit_long_signal(sig: dict, symbol: str = "") -> bool:
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
    return _bearish_state(sig, symbol) and sig["rsi"] > config.RSI_OVERSOLD


def _exit_short_signal(sig: dict, symbol: str = "") -> bool:
    """True when a short should be flat: bullish state, RSI not overbought.
    Mirror of _exit_long_signal."""
    return _bullish_state(sig, symbol) and sig["rsi"] < config.RSI_OVERBOUGHT


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


# One CROSS GAP BLOCK log/count per symbol per day, for the same reason the
# entry-delay latch exists: a name can sit inside the deadband for a whole
# session (~4.2% of all polls in the 8-session replay did), so counting polls
# would measure the clock, not the rule. One increment = one symbol-day on which
# the gap rule actually suppressed a signal.
_cross_gap_logged: dict[str, str] = {}


def _note_cross_gap_block(symbol: str, sig: dict, what: str,
                          price: Optional[float]) -> None:
    """Count a would-be signal suppressed by the minimum-separation rule.

    Reached only when the raw EMA condition was already true, so every increment
    is a real suppression. Watch this against _stop_exits: an exit suppressed
    here is DEFERRED (states are re-derived every poll and fire as soon as the
    gap widens), but a position that never clears the threshold is left riding
    its trailing stop alone — if that shows up, it shows up as a stop exit on a
    name that logged blocks first.
    """
    global _cross_gap_blocks
    key = symbol or "<unnamed>"
    if _cross_gap_logged.get(key) == date.today().isoformat():
        return
    _cross_gap_logged[key] = date.today().isoformat()
    _cross_gap_blocks += 1
    gap = abs(sig["ema_short"] - sig["ema_long"])
    pct = (gap / price * 100) if price else float("nan")
    logger.info("CROSS GAP BLOCK %s — %s suppressed: EMA gap %.4f on price "
                "%.2f = %.4f%%, below the %.2f%% minimum (gap blocks #%d)",
                key, what, gap, price or 0.0, pct,
                config.EMA_CROSS_MIN_GAP_PCT * 100, _cross_gap_blocks)


# Consecutive failed history fetches per symbol. In-memory: a restart forgets
# the streak and the next miss starts a fresh one, which under-reports rather
# than over-reports — the same direction _cross_first_seen fails in.
_history_gap_streak: dict[str, int] = {}


def _note_history_gap(symbol: str, held: int, label: str = "") -> None:
    """Record a poll that evaluated NOTHING for a held name because bar history
    was unavailable.

    All three evaluate_* paths return early on an empty history, which aborts
    the cycle before any exit logic runs — the trailing stop for equities and
    futures, the close-on-opposite-state for options. When the name is flat that
    costs nothing. When it is HELD, protection went unevaluated and nothing said
    so; making that visible is the only reason this exists.

    Silent when flat on purpose. A skipped poll on a flat name protects nothing,
    and counting those would measure the broker's uptime rather than our
    exposure (2026-08-04: DXCM burned 7 such polls flat, PLTR 6 while held —
    indistinguishable in the log before this).

    Watch _history_gaps_held against _stop_exits: a name that stops out shortly
    after logging gaps is one whose exit this delayed.
    """
    global _history_gaps_held
    key = symbol or "<unnamed>"
    streak = _history_gap_streak.get(key, 0) + 1
    _history_gap_streak[key] = streak
    if held == 0:
        return
    _history_gaps_held += 1
    log = (logger.error if streak >= config.HISTORY_GAP_ERROR_STREAK
           else logger.warning)
    log("POSITION UNCHECKED %s%s — no bar history for %d consecutive poll(s); "
        "stop and exit logic not evaluated this cycle (held=%d, unchecked #%d)",
        key, f" ({label})" if label else "", streak, held, _history_gaps_held)


def _clear_history_gap(symbol: str) -> None:
    """A good fetch ends the streak, so escalation measures a CURRENT outage
    rather than accumulating unrelated blips over a session."""
    _history_gap_streak.pop(symbol or "<unnamed>", None)


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
_OPT_POSITIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   config.OPTIONS_POSITION_FILE)

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
_high_vol_stops = 0        # stops armed TIGHTER than normal (ATR/price > 5%)
_low_vol_stops = 0         # stops armed WIDER  than normal (ATR/price <= 2%)
_cross_gap_blocks = 0      # would-be signals suppressed by EMA_CROSS_MIN_GAP_PCT
_cross_sustain_blocks = 0  # gap-valid entry crosses deferred by CROSS_SUSTAIN_MINUTES
_regime_short_blocks = 0   # would-be short entries suppressed by SHORT_MIN_REGIME
_stops_trailed = 0         # trailing stops that actually moved (new extreme)
_breakeven_locks = 0       # stops floored at entry after +1 ATR of profit (principal locked)
_history_gaps_held = 0     # polls that evaluated NO stop because history was unavailable
                           # AND the name was held (flat names are not counted)
_option_expiry_drops    = 0  # stored contracts cleared because their expiration passed
_option_orphan_drops    = 0  # stored contracts the broker no longer reports
_option_adoptions       = 0  # live broker contracts folded into the store (pre-fix legacy)
_option_target_exits    = 0  # contracts closed at OPTION_PROFIT_TARGET_PCT
_option_stop_exits      = 0  # contracts closed at OPTION_STOP_LOSS_PCT
_option_expiry_exits    = 0  # contracts closed with <= OPTION_MIN_DAYS_TO_EXPIRY left
_occ_stop_prunes        = 0  # OCC-keyed stop records dropped (options are not stop-managed)


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
    """The NORMAL-band ATR multiple for a regime (risk_on 2.5 → crisis 1.0).

    This is the regime axis on its own, with no volatility banding: it is what a
    name with an ordinary ATR/price ratio arms at. Used for display (the startup
    banner, which has no symbol and so no ratio) and as the fallback inside
    _get_atr_mult when ATR or price is unusable. Unknown regimes fall back to
    STOP_LOSS_ATR_MULT (risk_on width)."""
    return config.ATR_MULT_BY_REGIME.get(regime, config.STOP_LOSS_ATR_MULT)


def _atr_band(atr: Optional[float], price: Optional[float]) -> Optional[str]:
    """Classify a name's volatility as "low" / "normal" / "high" from ATR/price.

    Returns None when the ratio cannot be computed (missing/zero/negative ATR or
    price), which the caller reads as "don't band, use the plain regime width" —
    a bad ratio must never silently produce a tighter stop than intended.
    Boundaries are exclusive at the top: <=2% low, >5% high, else normal."""
    if not atr or not price or atr <= 0 or price <= 0:
        return None
    ratio = atr / price
    if ratio <= config.ATR_PCT_LOW_THRESHOLD:
        return "low"
    if ratio > config.ATR_PCT_HIGH_THRESHOLD:
        return "high"
    return "normal"


def _get_atr_mult(regime: str, atr: Optional[float] = None,
                  price: Optional[float] = None) -> float:
    """The ATR multiple to ARM a new stop with, from BOTH axes: market regime and
    the name's volatility band (ATR/price at entry).

    Single source of truth for width→arming so the arming sites don't each
    re-derive it. Falls back to the plain regime width (_regime_atr_mult) when the
    regime is unknown OR the ratio is uncomputable, so a missing ATR can only ever
    give the previous behaviour, never a surprise-tight stop.

    Counts the off-normal arms so we can tell whether the banding is doing
    anything: if _high_vol_stops stays at 0 for weeks, the 5% threshold is too
    high to ever bind and the rule is dead weight."""
    global _high_vol_stops, _low_vol_stops
    row = config.ATR_MULT_BY_REGIME_AND_BAND.get(regime)
    band = _atr_band(atr, price)
    if row is None or band is None:
        return _regime_atr_mult(regime)
    low, normal, high = row
    if band == "high":
        _high_vol_stops += 1
        logger.info("VOL BAND high: ATR/price=%.2f%% > %.0f%% — arming %s stop at "
                    "%.2fx instead of %.2fx (tighter) #%d",
                    (atr / price) * 100, config.ATR_PCT_HIGH_THRESHOLD * 100,
                    regime, high, normal, _high_vol_stops)
        return high
    if band == "low":
        _low_vol_stops += 1
        logger.info("VOL BAND low: ATR/price=%.2f%% <= %.0f%% — arming %s stop at "
                    "%.2fx instead of %.2fx (wider) #%d",
                    (atr / price) * 100, config.ATR_PCT_LOW_THRESHOLD * 100,
                    regime, low, normal, _low_vol_stops)
        return low
    return normal


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
    # Entry estimate. cost_basis/|qty| is right for shares but WRONG for an option
    # contract: the basis is in dollars and carries the x100 contract multiplier,
    # so a 8.15 premium fill reads back as 815.00 — a stop then armed 3xATR under
    # 815 and compared against a ~7 premium is breached on its first cycle, every
    # cycle (2026-08-05, NVDA 260821C220). The options store already holds the true
    # per-share fill, so for a contract we read it instead of re-deriving it.
    #
    # DEFENSIVE: with the watchlist OCC filter in place this branch is unreachable
    # in normal operation — options are managed by evaluate_option, which arms no
    # bot stop at all. It stays because the failure it prevents is silent and
    # expensive, and a future caller reaching _bootstrap_stop with a contract
    # should get correct units rather than a 100x-off stop.
    if config.is_occ_symbol(symbol):
        entry = _option_entry_price(symbol)
        if entry is None:
            logger.warning("STOP BOOTSTRAP %s skipped: option contract with no "
                           "stored entry price (refusing a cost-basis estimate — "
                           "it would be off by the x100 multiplier)", symbol)
            return None
    else:
        basis = _cost_basis(positions, symbol)
        entry = (basis / abs(held)) if (basis and held) else price
    # Band off the ESTIMATED entry, not the live price, so the ratio matches the
    # entry_price actually written into the record.
    mult = _get_atr_mult(regime, atr, entry)
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


# ── Broker-native stop floor (static disaster backstop) ───────────────────────
# Counters, so we can tell later whether this ever earned its keep. _floor_fires
# is the one that matters: it should stay at ZERO in normal operation, because a
# floor that fires means the bot's own stop did not. A climbing _floor_fires is
# not the feature working — it is the bot failing to exit and the backstop
# catching what it missed, and each one deserves a look.
_floors_placed = 0
_floors_cancelled = 0
_floor_orphans = 0
_floor_cancel_failures = 0
# Latched once the startup reconcile has run, so it stays a startup-only pass and
# does not add a working-orders fetch to every 60s cycle. A cancel that FAILS is
# therefore retried on the next restart, not on the next poll — the loud error in
# _cancel_broker_floor is what surfaces it in the meantime.
_floors_reconciled = False


def _floor_price(entry: float, atr: float, mult: float, direction: str) -> float:
    """The static GTC backstop level: the INITIAL ATR stop distance, widened by
    BROKER_STOP_FLOOR_BUFFER so the bot's stop always trips first.

    Deliberately derived from entry and the entry ATR — the same anchors the stop
    record uses — and NEVER from the current stop_price, which trails. A floor
    recomputed off a trailed stop would creep toward the market and eventually
    overtake the bot, which is the exact failure this design exists to avoid.
    """
    buf = atr * mult * config.BROKER_STOP_FLOOR_BUFFER
    return entry + buf if direction == "short" else entry - buf


def _place_broker_floor(symbol: str, qty: int, rec: dict,
                        account_id: str) -> None:
    """Place the one-time GTC stop behind a freshly armed position.

    Mutates `rec` in place with broker_floor_price / broker_order_id so the
    caller persists them in the same write as the rest of the stop record. On
    any failure the keys are simply absent: the position keeps its bot-managed
    stop and reconcile re-arms the floor on the next startup. A missing floor is
    a degraded state, never a blocked entry — refusing to hold a position we
    already bought because a backstop order failed would be strictly worse.
    """
    global _floors_placed
    if not config.ENABLE_BROKER_STOP_FLOOR:
        return
    if qty is None or qty < 1 or not account_id:
        return
    floor = _floor_price(rec["entry_price"], rec["atr_at_entry"],
                         rec["atr_mult"], rec["direction"])
    side = "buy_to_cover" if rec["direction"] == "short" else "sell"
    result = tc.place_equity_order(account_id, symbol, side, qty,
                                   order_type="stop", duration="gtc",
                                   stop_price=floor)
    if not result:
        logger.error("BROKER FLOOR %s: placement FAILED — position holds its "
                     "bot-managed stop only (no gap protection until reconcile)",
                     symbol)
        return
    order_id = result.get("order", {}).get("id")
    rec["broker_floor_price"] = round(floor, 2)
    rec["broker_order_id"] = order_id
    _floors_placed += 1
    logger.info("BROKER FLOOR %s %s x%d @ %.2f GTC (entry=%.2f, %.1fx ATR x %.1f "
                "buffer, bot stop=%.2f) order=%s — floors #%d",
                symbol, side, qty, floor, rec["entry_price"], rec["atr_mult"],
                config.BROKER_STOP_FLOOR_BUFFER, rec["stop_price"], order_id,
                _floors_placed)


def _cancel_broker_floor(symbol: str, rec: Optional[dict],
                         account_id: Optional[str]) -> bool:
    """Cancel the resting floor behind a position that is leaving. True if gone.

    Called from EVERY exit route. A floor that outlives its position is not
    inert: a GTC sell stop with no shares behind it fills into a fresh SHORT the
    next time price trades through it. A False return is logged loudly because
    it means exactly that risk is live.
    """
    global _floors_cancelled, _floor_cancel_failures
    if not rec:
        return True
    order_id = rec.get("broker_order_id")
    if not order_id or not account_id:
        return True
    if tc.cancel_order(account_id, order_id):
        _floors_cancelled += 1
        logger.info("BROKER FLOOR %s: cancelled order %s — cancels #%d",
                    symbol, order_id, _floors_cancelled)
        return True
    _floor_cancel_failures += 1
    logger.error("BROKER FLOOR %s: cancel FAILED for order %s — an orphaned GTC "
                 "stop may still be resting and could open an unintended "
                 "position; startup reconcile will retry — failures #%d",
                 symbol, order_id, _floor_cancel_failures)
    return False


def _release_stop(symbol: str, account_id: Optional[str] = None) -> None:
    """Tear down a position's protection: cancel the broker floor, drop the
    record. The single teardown entry point — _clear_stop alone would leave the
    broker order resting, so every exit route calls THIS."""
    stops = _load_stops()
    _cancel_broker_floor(symbol, stops.get(symbol), account_id)
    if symbol in stops:
        del stops[symbol]
        _save_stops(stops)


def reconcile_broker_floors(positions: list[dict], account_id: str) -> None:
    """Cancel orphaned floors and re-arm missing ones. Called once at startup.

    The broker's working-order list is the authority here, NOT the order ids in
    stop_prices.json — mirroring reconcile_stops, which treats `positions` as
    the authority for the same reason. A stored id says what we last did; only
    the broker says what is actually resting.

    Bails on a None fetch: that means the API call failed, and treating it as
    "nothing is resting" would cancel nothing while re-arming duplicates on top
    of floors that are still live.
    """
    global _floor_orphans, _floors_reconciled
    if not config.ENABLE_BROKER_STOP_FLOOR:
        return
    if _floors_reconciled:
        return                            # one-shot per process (startup only)
    working = tc.get_working_orders(account_id)
    if working is None:
        logger.warning("BROKER FLOOR reconcile skipped: working-order fetch "
                       "failed — not cancelling or re-arming on an unknown state")
        return
    stops = _load_stops()
    held = {p.get("symbol"): int(p.get("quantity", 0)) for p in positions
            if p.get("symbol")}
    known_ids = {r.get("broker_order_id") for r in stops.values()
                 if r.get("broker_order_id")}

    # 1. Orphans: a resting stop we own the id for, but hold no position behind.
    for o in working:
        if str(o.get("order_type", "")).lower() not in ("stopmarket", "stop"):
            continue
        oid, sym = o.get("order_id"), o.get("symbol")
        if oid not in known_ids:
            continue                      # not ours — leave manual orders alone
        if held.get(sym):
            continue                      # position still open, floor is correct
        if tc.cancel_order(account_id, oid):
            _floor_orphans += 1
            logger.warning("BROKER FLOOR ORPHAN %s: resting stop %s with no "
                           "position — cancelled (orphans #%d)",
                           sym, oid, _floor_orphans)

    # 2. Missing: a held position whose stop record has no live resting floor.
    live_ids = {o.get("order_id") for o in working}
    changed = False
    unplaced = 0
    for sym, rec in stops.items():
        qty = held.get(sym)
        if not qty or config.is_occ_symbol(sym):
            continue
        if rec.get("broker_order_id") in live_ids:
            continue                      # already protected
        rec.pop("broker_order_id", None)   # stale id: broker says it is gone
        rec.pop("broker_floor_price", None)
        _place_broker_floor(sym, abs(qty), rec, account_id)
        if rec.get("broker_order_id"):
            changed = True
        else:
            unplaced += 1
    if changed:
        _save_stops(stops)
    # Latch only on a CLEAN pass. Two ways this stays unlatched and retries next
    # cycle: the working-order fetch failed above (unknown broker state), or a
    # placement failed here. The second matters most on the first run after
    # enabling — if the broker rejects the order shape, every position is left
    # unfloored, and latching would mean no retry until someone restarts the bot.
    if unplaced:
        logger.warning("BROKER FLOOR reconcile incomplete: %d position(s) still "
                       "unfloored — will retry next cycle", unplaced)
        return
    _floors_reconciled = True


def _arm_stop_on_entry(symbol: str, entry_price: float, atr: Optional[float],
                       direction: str = "long", regime: str = "risk_on",
                       signal_price: Optional[float] = None,
                       fill_price: Optional[float] = None,
                       slippage: Optional[float] = None,
                       qty: Optional[int] = None,
                       account_id: Optional[str] = None) -> None:
    """Create a fresh stop record after a BUY (long) or SELLSHORT (short) fills.
    The stop WIDTH is the regime's ATR multiple (risk_on 2.5 → crisis 1.0),
    persisted as "atr_mult" and reused for all later trailing so the width is
    fixed at entry. A short's stop sits ABOVE entry (entry + mult*atr) and will
    ratchet DOWN; a long's sits below and ratchets up. No-op with a warning if ATR
    is unavailable (equities always carry high/low, so this should never fire)."""
    # entry_price is now the ACTUAL fill when available: the entry paths resolve
    # it via _resolve_fill -> tc.get_order before calling here, and pass the
    # signal_price/fill_price/slippage through for the STOP ARMED log. On a
    # fill-lookup miss they fall back to sig["close"] (signal_price set, fill_price
    # None) and this logs a WARNING that the stop was armed at the signal price.
    # See memory: project_stop_armed_at_signal_price
    if atr is None or atr <= 0:
        logger.warning("Could not arm stop for %s: ATR unavailable — position is "
                       "UNPROTECTED until bootstrap re-arms it.", symbol)
        return
    mult = _get_atr_mult(regime, atr, entry_price)
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
    # Broker floor placed BEFORE the save so the order id lands in the same write
    # as the record it belongs to. A crash between the two would otherwise leave
    # a resting GTC order nobody remembers placing.
    _place_broker_floor(symbol, qty, rec, account_id)
    stops = _load_stops()
    stops[symbol] = rec
    _save_stops(stops)
    if fill_price is not None:
        logger.info("STOP ARMED %s %s entry=%.2f atr=%.2f mult=%.1fx stop=%.2f "
                    "(regime=%s) fill=%.2f signal=%.2f slippage=%+.2f",
                    symbol, direction, entry_price, atr, mult, stop, regime,
                    fill_price, signal_price, slippage)
    elif signal_price is not None:
        logger.warning("STOP ARMED %s %s entry=%.2f atr=%.2f mult=%.1fx stop=%.2f "
                       "(regime=%s) fill=UNAVAILABLE — armed at SIGNAL price",
                       symbol, direction, entry_price, atr, mult, stop, regime)
    else:
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
    global _occ_stop_prunes
    if not positions:
        return
    held = {p.get("symbol") for p in positions
            if int(p.get("quantity", 0)) != 0 and p.get("symbol")}
    stops = _load_stops()
    stale = [s for s in stops if s not in held]
    for s in stale:
        del stops[s]
        logger.info("STOP PRUNE %s: no longer held — dropping stop record", s)
    # Option contracts carry no bot-managed stop (evaluate_option exits on EMA
    # state), so any OCC-keyed record here is debris from the pre-2026-08-05 path
    # where a contract leaked into the stock loop and got one bootstrapped. It is
    # unreachable now, but a held contract survives the not-held prune above, so
    # it would otherwise sit in the file forever carrying a 100x-off entry price.
    # Re-derived from current logic rather than one-shot cleaned, so it also
    # catches records written by any future leak.
    contracts = [s for s in stops if config.is_occ_symbol(s)]
    for s in contracts:
        rec = stops.pop(s)
        _occ_stop_prunes += 1
        logger.info("STOP PRUNE %s: option contract — options are not stop-managed "
                    "(dropping record, entry_price=%s) #%d",
                    s, rec.get("entry_price"), _occ_stop_prunes)
    if stale or contracts:
        _save_stops(stops)


def _check_and_trail_stop(symbol: str, held: int, sig: dict,
                          account_id: str, positions: list[dict],
                          regime: str = "risk_on") -> bool:
    """Update the trailing stop for a held position and exit if breached.

    Returns True iff a stop-exit order was placed (caller then returns, skipping
    signal logic for the cycle). False = no exit; continue to EMA-cross logic."""
    global _stop_exits, _stops_trailed, _breakeven_locks

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

    # Breakeven lock: once this position's best excursion has reached +1 ATR of
    # profit AND it is still in profit right now, floor the stop at entry. Same
    # operation as crisis_floor, different trigger (realized profit, any regime).
    #   * The excursion test reads the STORED water (pre this cycle's ratchet). A
    #     name crossing the threshold on a fresh new high locks one cycle later —
    #     harmless, since on that cycle price is AT a new high, nowhere near the
    #     stop. high/low-water are monotonic, so once true the trigger stays true;
    #     no extra persisted flag is needed.
    #   * The `price > entry` / `price < entry` clamp is what keeps the floor from
    #     ever being armed through the market (which would force an instant exit).
    #     It is why retroactive application to pre-rule positions is safe: an
    #     underwater name (DDOG) gates itself out; an in-profit one (CRL) locks.
    breakeven_lock = False
    if config.ENABLE_BREAKEVEN_LOCK and entry:
        trig = config.BREAKEVEN_LOCK_ATR * rec["atr_at_entry"]
        if direction == "short":
            breakeven_lock = rec["low_water"]  <= entry - trig and price < entry
        else:
            breakeven_lock = rec["high_water"] >= entry + trig and price > entry

    apply_floor = crisis_floor or breakeven_lock

    # Captured ONCE before the branches: both of them mutate rec["stop_price"],
    # so a log inside each would be the same logic dispatched to two sites.
    old_stop = rec["stop_price"]

    if direction == "short":
        # Ratchet DOWN: low-water and stop only ever fall — never raise the stop.
        rec["low_water"] = round(min(rec["low_water"], price), 4)
        raw_trail = rec["low_water"] + mult_atr
        new_stop = min(raw_trail, entry) if apply_floor else raw_trail  # floor: cap short stop at breakeven
        rec["stop_price"] = round(min(rec["stop_price"], new_stop), 4)
        water = rec["low_water"]
        breached = price >= rec["stop_price"]     # price rose into the stop
        exit_side, exit_qty = "buy_to_cover", abs(held)
        exit_action = "BUY_TO_COVER"
    else:
        # Ratchet UP: high-water and stop only ever rise — never lower the stop.
        rec["high_water"] = round(max(rec["high_water"], price), 4)
        raw_trail = rec["high_water"] - mult_atr
        new_stop = max(raw_trail, entry) if apply_floor else raw_trail  # floor: floor long stop at breakeven
        rec["stop_price"] = round(max(rec["stop_price"], new_stop), 4)
        water = rec["high_water"]
        breached = price <= rec["stop_price"]     # price fell into the stop
        exit_side, exit_qty = "sell", held
        exit_action = "SELL"

    # Trail log — fires only when the stop actually MOVED, i.e. on a new extreme.
    # The unconditional _save_stops below runs every poll for every held name
    # (~55k polls/8 sessions in the logs), so an unguarded line here would bury
    # the log rather than illuminate it. The water label names the direction the
    # trail is tracking: high_water ratchets up under a long, low_water ratchets
    # down over a short. `mult` is the EFFECTIVE multiple, so a defensive-regime
    # tighten shows up here as a changed width rather than an unexplained jump.
    if rec["stop_price"] != old_stop:
        _stops_trailed += 1
        logger.info("STOP TRAIL %s %s %.2f → %.2f (%s=%.2f, trail=%.2fx%.2f) "
                    "— trails #%d",
                    symbol, direction, old_stop, rec["stop_price"],
                    "low_water" if direction == "short" else "high_water",
                    water, mult, rec["atr_at_entry"], _stops_trailed)

    # Breakeven-lock event: log + count once, on the cycle the floor first snaps
    # the stop TO entry (the raw trail would have left it short of breakeven). The
    # STOP TRAIL line above already recorded the move; this line explains WHY it
    # jumped to entry. `not crisis_floor` keeps a crisis-regime floor from being
    # mis-attributed here. Idempotent: once stop_price == entry, old_stop == entry
    # on later cycles, so the transition test is false and it will not re-fire.
    entry_r = round(entry, 4) if entry else None
    if (breakeven_lock and not crisis_floor
            and old_stop != entry_r and rec["stop_price"] == entry_r):
        _breakeven_locks += 1
        logger.info("BREAKEVEN LOCK %s %s: floor raised to entry %.2f "
                    "(trail would be %.2f) — locks #%d",
                    symbol, direction, entry, raw_trail, _breakeven_locks)

    if breached:
        logger.warning("STOP-LOSS EXIT %s %s x%d @ %.2f (stop=%.2f entry=%.2f "
                       "water=%.2f) — exit #%d",
                       symbol, direction, exit_qty, price, rec["stop_price"],
                       rec["entry_price"], water, _stop_exits + 1)
        result = tc.place_equity_order(account_id, symbol, exit_side, exit_qty)
        if result:
            _stop_exits += 1
            # Fourth teardown route. Deliberately _cancel_broker_floor and not
            # _release_stop: this path already holds `stops` and saves it below,
            # and _release_stop would reload/rewrite the file underneath it.
            _cancel_broker_floor(symbol, rec, account_id)
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
            _log_exit_trade(exit_action, symbol, exit_qty, price, order_id,
                            f"trailing stop hit @ {rec['stop_price']:.2f}",
                            account_id)
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
    # Resize the broker floor to the shares that REMAIN. The floor was sized for
    # the full position; left alone it would try to sell more than we hold and
    # open a short on the difference. This is the one place the "never update the
    # floor" rule bends, and it bends for a QUANTITY change, not a price change —
    # one-shot per position (profit_taken latches above), so two API calls in a
    # position's entire lifetime, not two per trail.
    remaining = held - sell_qty
    if remaining > 0 and rec.get("broker_order_id"):
        _cancel_broker_floor(symbol, rec, account_id)
        rec.pop("broker_order_id", None)
        rec.pop("broker_floor_price", None)
        _place_broker_floor(symbol, remaining, rec, account_id)
    stops[symbol] = rec
    _save_stops(stops)                       # record kept -> remainder keeps its stop
    order_id = result.get("order", {}).get("id")
    _log_exit_trade("SELL", symbol, sell_qty, price, order_id,
                    f"profit take (+{gain * 100:.1f}% from entry, RSI={sig['rsi']:.1f})",
                    account_id)
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


# Whether a WORSE fill is a HIGHER price depends only on which way we are
# trading, not on whether the position is long or short: buying wants a low fill,
# selling wants a high one. The legacy entry-direction spellings map cleanly
# ("long" entry = BUY, "short" entry = SELL_SHORT) and are still accepted.
_BUYING_SIDE = {"BUY", "BUY_TO_COVER", "BUY_TO_OPEN", "long"}
_SELLING_SIDE = {"SELL", "SELL_SHORT", "SELL_TO_CLOSE", "short"}


def _slippage_sign(action: str) -> float:
    """+1 when a higher fill is worse (we are buying), -1 when lower is worse.

    Raises on an unrecognised action rather than guessing: a silently wrong sign
    would invert every slippage reading for that path and, worse, would be
    invisible — the numbers would still look plausible."""
    if action in _BUYING_SIDE:
        return 1.0
    if action in _SELLING_SIDE:
        return -1.0
    raise ValueError(f"_slippage_sign: unrecognised action {action!r}")


def _resolve_fill(symbol: str, account_id: str, order_id: Optional[str],
                  signal_price: float, action: str) -> tuple:
    """Resolve the ACTUAL price for a just-placed equity order (entry OR exit).

    Queries the broker (tc.get_order) for the fill and returns
    (resolved_price, fill_price, slippage):
      * fill available   -> (fill, fill, slippage)          stop arms off the fill
      * fill unavailable -> (signal_price, None, None) + WARNING; caller falls
        back to the signal-bar close (degraded, not disabled).

    Slippage is signed so POSITIVE always means a WORSE fill than signalled:
      BUY / BUY_TO_COVER        fill - signal   (paid more    = worse)
      SELL / SELL_SHORT         signal - fill   (sold cheaper = worse)

    WHY EXITS MATTER AS MUCH AS ENTRIES: until 2026-07-27 only entries resolved
    a fill, so every exit in the ledger was priced at the signal-bar close. A
    broker audit re-priced all 25 closed trips at real fills and moved realized
    P&L from -$36,296.78 to -$39,229.12 — 8.1% understated. The error is
    directionally biased (both legs fill worse), so it accumulates rather than
    averaging out; pricing only one leg fixed only half of it.
    """
    fill = tc.get_order(account_id, order_id) if order_id else None
    if fill is None:
        logger.warning("Fill price unavailable for %s (%s) — using signal price "
                       "%.4f (degraded, not disabled).", symbol, action, signal_price)
        return signal_price, None, None
    slippage = _slippage_sign(action) * (fill - signal_price)
    return fill, fill, round(slippage, 4)


def _log_exit_trade(action: str, symbol: str, qty, price: float, order_id,
                    notes: str, account_id: str) -> None:
    """Resolve the real fill for an EXIT and write the trade record.

    One dispatch point for all eight exit paths (stop-loss, profit take, crisis
    de-risk, state exit, cover, futures close, option close). They previously
    called log_trade directly with no fill resolution, which is precisely how
    every exit leg ended up signal-priced. Adding it per-site would have meant
    eight chances to forget the argument on the next exit path someone writes.
    """
    _, fill_px, slippage = _resolve_fill(symbol, account_id, order_id, price, action)
    log_trade(action, symbol, qty, price, "market", order_id, notes,
              fill_price=fill_px, signal_price=price, slippage=slippage)


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
        _clear_cross_clocks_for(symbol)   # position exists; clocks are pre-entry state
        order_id = result.get("order", {}).get("id")
        entry_px, fill_px, slippage = _resolve_fill(symbol, account_id, order_id,
                                                     price, "BUY")
        log_trade("BUY", symbol, qty, price, "market", order_id, reason,
                  fill_price=fill_px, signal_price=price, slippage=slippage)
        _arm_stop_on_entry(symbol, entry_px, sig.get("atr"), regime=regime,
                           signal_price=price, fill_price=fill_px, slippage=slippage,
                           qty=qty, account_id=account_id)
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
        _clear_cross_clocks_for(symbol)   # position exists; clocks are pre-entry state
        order_id = result.get("order", {}).get("id")
        entry_px, fill_px, slippage = _resolve_fill(symbol, account_id, order_id,
                                                     price, "SELL_SHORT")
        log_trade("SELL_SHORT", symbol, qty, price, "market", order_id, reason,
                  fill_price=fill_px, signal_price=price, slippage=slippage)
        _arm_stop_on_entry(symbol, entry_px, sig.get("atr"), direction="short",
                           regime=regime, signal_price=price, fill_price=fill_px,
                           slippage=slippage, qty=qty, account_id=account_id)
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
    """Map a regime to entry gates:
    (block_new_entries, block_momentum_align, block_shorts).
    Centralized so evaluate_stock and evaluate_future read identical logic and a
    rule change lands in exactly one place.

    block_shorts is the SHORT_MIN_REGIME gate: a regime less fearful than the
    configured floor blocks NEW shorts. It is deliberately redundant with
    block_new_entries in defensive/crisis (both block) so that reading either
    gate alone still gives the right answer for shorts."""
    block_new_entries    = regime in ("defensive", "crisis")
    block_momentum_align = regime in ("cautious", "defensive", "crisis")
    if getattr(config, "ENABLE_REGIME_SHORT_FILTER", False):
        floor = getattr(config, "SHORT_MIN_REGIME", "risk_on")
        # "unknown" = the VIX quote failed. Blocked UNCONDITIONALLY, not via the
        # rank comparison: unknown ranks 0 alongside risk_on, so once the floor was
        # lowered to "risk_on" (2026-08-03) the rank test stopped blocking it and a
        # VIX outage silently started permitting shorts. Longs fail-OPEN on a data
        # glitch, shorts fail-CLOSED — failing open on a short is how you get short
        # into a rally, and an outage is exactly when you cannot see the rally.
        block_shorts = (regime == "unknown"
                        or _REGIME_RANK.get(regime, 0) < _REGIME_RANK.get(floor, 0))
    else:
        block_shorts = False
    return block_new_entries, block_momentum_align, block_shorts


# Fear ordering for the belt-&-suspenders VIX-vs-sentiment combination.
_REGIME_RANK = {"risk_on": 0, "unknown": 0, "cautious": 1, "defensive": 2, "crisis": 3}


def _more_fearful(a: str, b: str) -> str:
    """Return the more fearful of two regimes — the effective regime is the MORE
    fearful of the VIX regime and the Claude-sentiment regime (if either says fear,
    respect it)."""
    return a if _REGIME_RANK.get(a, 0) >= _REGIME_RANK.get(b, 0) else b


def effective_regime(vix_regime: str, sent_regime: Optional[str]) -> str:
    """The regime every gate should read: VIX combined with sentiment, or VIX
    alone when config.ENABLE_SENTIMENT_OVERRIDE is False.

    A helper rather than an inline conditional because the combine has two
    callers — the live path in main._run_cycle and the startup banner's
    "what width would the next entry arm at" preview. Those drifting apart would
    mean the banner advertises a regime the bot does not actually trade, which is
    the kind of divergence nobody notices until it matters. One switch, one place.
    """
    if not getattr(config, "ENABLE_SENTIMENT_OVERRIDE", True):
        return vix_regime
    return _more_fearful(vix_regime, sent_regime or "risk_on")


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
        # Same condition, two different messages. With the override off, sentiment
        # being more fearful than VIX changes NOTHING, so calling it an OVERRIDE
        # would report an action that did not happen — and this line is logged
        # every cycle, so a wrong one becomes hundreds of wrong lines a session.
        # It still logs: the divergence is the whole reason to keep running the
        # overlay, and it is what a later "should we switch it back on?" is judged on.
        if getattr(config, "ENABLE_SENTIMENT_OVERRIDE", True):
            logger.warning("SENTIMENT OVERRIDE: %s mode from Claude analysis "
                           "(fear=%s, VIX-regime=%s, risks: %s)", sent_regime, fear,
                           vix_regime, ", ".join(risks or []) or "n/a")
        else:
            logger.info("SENTIMENT ADVISORY (no override): Claude reads %s vs "
                        "VIX-regime %s (fear=%s) — regime stays %s. risks: %s",
                        sent_regime, vix_regime, fear, regime,
                        ", ".join(risks or []) or "n/a")
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
    global _crisis_exits, _sentiment_sector_blocks, _regime_short_blocks

    history = tc.get_historical(symbol, days=90)
    if not history:
        # `positions` is already in hand, so held is knowable WITHOUT history --
        # which is what lets this separate a harmless skip on a flat name from a
        # live stop that went unchecked.
        _note_history_gap(symbol, _current_position(positions, symbol))
        return
    _clear_history_gap(symbol)

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
                _log_exit_trade("SELL", symbol, held, price, order_id,
                                "VIX crisis de-risk", account_id)
                _release_stop(symbol, account_id)
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
    if held > 0 and _exit_long_signal(sig, symbol) and not _already_sold_today(symbol):
        logger.info("SIGNAL SELL %s x%d", symbol, held)
        result = tc.place_equity_order(account_id, symbol, "sell", held)
        if result:
            _mark_sold(symbol)
            order_id = result.get("order", {}).get("id")
            _log_exit_trade("SELL", symbol, held, price, order_id,
                            f"EMA bearish, RSI={sig['rsi']:.1f}", account_id)
            _note_state_only_exit(symbol, sig, "bearish_cross")
            _release_stop(symbol, account_id)
        return

    # COVER — close a short whenever the trend is bullish (mirror of SELL).
    if held < 0 and _exit_short_signal(sig, symbol) and not _already_bought_today(symbol):
        qty = abs(held)
        logger.info("SIGNAL BUY_TO_COVER %s x%d", symbol, qty)
        result = tc.place_equity_order(account_id, symbol, "buy_to_cover", qty)
        if result:
            _short_covers += 1
            _mark_bought(symbol)
            order_id = result.get("order", {}).get("id")
            _log_exit_trade("BUY_TO_COVER", symbol, qty, price, order_id,
                            f"EMA bullish (cover), RSI={sig['rsi']:.1f}", account_id)
            _note_state_only_exit(symbol, sig, "bullish_cross")
            _release_stop(symbol, account_id)
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
            _bullish_cross_edge(sig, symbol)
            or (is_momentum and _bullish_state(sig, symbol))))
        return

    # One entry per name per day (what the old single gate actually protected).
    # A name that already traded today does not get re-entered on a later blip.
    if _already_bought_today(symbol) or _already_sold_today(symbol):
        return

    # VIX regime entry gates (centralized in _apply_regime_rules). Exits and stops
    # above are deliberately ungated — de-risking is always allowed; only ENTRIES
    # are throttled by fear. cautious blocks only momentum-alignment; defensive and
    # crisis block every new entry (fresh-cross longs, alignment, and shorts).
    block_new_entries, block_momentum_align, block_shorts = _apply_regime_rules(regime)

    # BUY signal — fresh EMA cross (all symbols)
    if (_bullish_cross_edge(sig, symbol) and sig["rsi"] < config.RSI_OVERBOUGHT
            and held == 0 and not block_new_entries):
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
          and _bullish_state(sig, symbol)
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
    elif (_bearish_cross_edge(sig, symbol) and sig["rsi"] > config.RSI_OVERSOLD
          and held == 0 and config.ENABLE_SHORTING and not block_new_entries):
        # SHORT_MIN_REGIME gate. Checked INSIDE the branch, not as an extra elif
        # condition, so a blocked short is counted and logged rather than
        # silently falling through to nothing — a safety net you cannot see
        # firing is one you cannot later argue for removing.
        if block_shorts:
            _regime_short_blocks += 1
            logger.info("REGIME BLOCK: skipping short %s — regime=%s below "
                        "SHORT_MIN_REGIME=%s (market not fearful enough to "
                        "short) #%d", symbol, regime,
                        getattr(config, "SHORT_MIN_REGIME", "risk_on"),
                        _regime_short_blocks)
        elif _enter_short(symbol, sig, price, account_id, positions, equity,
                          reason=f"EMA cross down (short), RSI={sig['rsi']:.1f}",
                          regime=regime):
            _short_entries += 1
            logger.info("SHORT ENTRY %s — short entries #%d", symbol, _short_entries)


# ── Options Strategy ──────────────────────────────────────────────────────────

# ── Options position store ────────────────────────────────────────────────────
# WHY THIS EXISTS: the exit branch used to resolve `held` against an occ_symbol
# RECOMPUTED every cycle from _atm_strike(current underlying). Once the
# underlying drifted more than half a strike increment off the entry, that key
# stopped naming the contract we actually owned, _current_position returned 0,
# and the `elif held > 0` branch became unreachable — the position rode to
# expiration completely unmanaged. SPY260717C00540000 (opened 2026-07-01) was
# reconciled away on 2026-07-26 as "not in broker positions" without a single
# SELL_TO_CLOSE ever being attempted. That is the only options trade this bot has
# ever placed, and it failed this way.
#
# The futures path already solved this exact class of bug with
# _stale_futures_position (a live position whose symbol no longer matches the
# computed one). Options take the other route, mirroring equities: persist the
# contract at entry and drive every exit off the STORED symbol, the same way
# stop_prices.json anchors stops to entry data rather than to anything rederived
# from the current price.

def _option_key(symbol: str, opt_type: str) -> str:
    """Store key for a watchlist pair — one open contract per (underlying, type)."""
    return f"{symbol}_{opt_type.lower()}"


def _load_option_positions() -> dict:
    return _load_json(_OPT_POSITIONS_PATH)


def _save_option_positions(store: dict) -> None:
    _save_json(_OPT_POSITIONS_PATH, store)


def _save_option_position(key: str, record: dict) -> None:
    """Persist one open contract, leaving the other watchlist pairs untouched."""
    store = _load_option_positions()
    store[key] = record
    _save_option_positions(store)


def _option_entry_price(occ_symbol: str) -> Optional[float]:
    """The stored per-share entry premium for a contract, or None if unknown.

    Looked up BY occ_symbol (scanning the store) rather than by rebuilding the
    "<UNDERLYING>_<type>" key from the symbol — same reasoning as the section
    header above: the stored record is the anchor, and re-deriving a key is the
    bug class this store exists to kill.
    """
    for rec in _load_option_positions().values():
        if rec.get("occ_symbol") == occ_symbol:
            entry = rec.get("entry_price")
            if isinstance(entry, (int, float)) and entry > 0:
                return float(entry)
    return None


def _close_option_position(key: str) -> None:
    """Remove one contract from the store. A no-op if it was never there."""
    store = _load_option_positions()
    if store.pop(key, None) is not None:
        _save_option_positions(store)


def _drop_option_position(key: str, occ_symbol: str, reason: str) -> None:
    """Clear a stored contract we can no longer act on, and say why. Three paths
    reach this (expiry, broker orphan, malformed record), so the removal and the
    log line live here; each caller owns its own counter."""
    _close_option_position(key)
    logger.info("OPTION POSITION CLEARED %s (%s) — %s", key, occ_symbol, reason)


def _option_record(occ_symbol: str, entry_price: float, expiration: str,
                   opt_type: str, strike: float, underlying: float) -> dict:
    """Build a store record. Two callers (fresh entry, legacy adoption), so the
    schema is defined once — config.OPTIONS_POSITION_FILE documents it."""
    return {
        "occ_symbol":       occ_symbol,
        "entry_price":      entry_price,
        "entry_date":       date.today().isoformat(),
        "expiration":       expiration,
        "opt_type":         opt_type.lower(),
        "strike":           strike,
        "contracts":        config.OPTIONS_CONTRACTS,
        "underlying_entry": underlying,
    }


def _option_expired(expiration: str) -> bool:
    """True once the expiration date has PASSED. Equality is deliberately not
    expiry — a contract trades through the close on its expiration date, so
    dropping it that morning would abandon a still-sellable position."""
    try:
        return date.fromisoformat(str(expiration)) < date.today()
    except (TypeError, ValueError):
        # Unparseable date: fail OPEN and keep managing the contract. Dropping it
        # here would strand a real position on a cosmetic data problem.
        return False


def _days_to_expiry(expiration: str) -> Optional[int]:
    """Calendar days from today to `expiration`, or None if unparseable.

    None (not 0, and not a huge number) so the caller can tell "no idea" apart
    from "expires today". _option_expired already fails OPEN on a bad date; this
    mirrors that — an unreadable date must not trigger a forced liquidation.
    """
    try:
        return (date.fromisoformat(str(expiration)) - date.today()).days
    except (TypeError, ValueError):
        return None


def _option_exit_reason(bid: float, entry: Optional[float],
                        expiration: str) -> Optional[str]:
    """Which premium-based exit rule fires, or None. Ordered worst-news-first:
    stop before target (a contract cannot be at both, but if the data is weird
    the loss branch should win), then expiry.

    Split out of evaluate_option so the thresholds are testable without stubbing
    a broker, a quote feed and an indicator stack — the same reason
    _option_fill_price is its own function.
    """
    if not config.ENABLE_OPTION_EXIT_TARGETS:
        return None

    # Adopted contracts store entry_price 0.0 (unknown). Guard BEFORE arithmetic:
    # 0.0 would make every ratio degenerate and close the position on sight.
    if entry is not None and entry > 0 and bid > 0:
        # Half a cent — below the minimum tick, so it can never reach past a real
        # quote into the next price level, but it absorbs the float error in the
        # threshold: 8.15 * 1.50 is 12.225000000000001, so a bid of exactly 12.225
        # would MISS its own target without this.
        eps = 0.005
        if bid <= entry * config.OPTION_STOP_LOSS_PCT + eps:
            return (f"stop loss — bid {bid:.2f} <= "
                    f"{config.OPTION_STOP_LOSS_PCT:.0%} of entry {entry:.2f}")
        if bid >= entry * config.OPTION_PROFIT_TARGET_PCT - eps:
            return (f"profit target — bid {bid:.2f} >= "
                    f"{config.OPTION_PROFIT_TARGET_PCT:.0%} of entry {entry:.2f}")

    days = _days_to_expiry(expiration)
    if days is not None and days <= config.OPTION_MIN_DAYS_TO_EXPIRY:
        return f"near expiry — {days}d left (<= {config.OPTION_MIN_DAYS_TO_EXPIRY})"
    return None


def _option_fill_price(quote: Optional[dict], side: str) -> float:
    """Marketable price from the side of the book we actually trade against: an
    ENTRY lifts the ASK, an EXIT hits the BID.

    The old code read `last or bid` for BOTH sides (the line docs/backlog.md
    flags as strategy.py:1433). `last` can be a stale print from outside the
    current spread — the backlog's own sample has NVDA 260821P205 at last=8.93
    against bid=9.25 — so on entry it understates cost by roughly the full
    spread. These are market orders, so this does not change WHAT we send; it
    changes what the ledger records, and every realized-P&L figure downstream is
    computed from that. Options spreads run ~2.1% round-trip vs ~0.04% on
    equities, so mispricing the entry is ~50x more costly here than it is there.
    """
    if not quote:
        return 0.0
    for field in (("ask", "last", "bid") if side == "entry" else ("bid", "last", "ask")):
        val = quote.get(field)
        if val:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return 0.0


def evaluate_option(
    symbol:     str,
    expiration: str,
    opt_type:   str,
    account_id: str,
    positions:  list[dict],
) -> None:
    global _entries_delayed, _option_expiry_drops
    global _option_orphan_drops, _option_adoptions
    global _option_target_exits, _option_stop_exits, _option_expiry_exits

    history = tc.get_historical(symbol, days=90)
    if not history:
        # occ_symbol is derived from `sig` further down, so it is unavailable
        # here — but the STORED contract already carries it, the same record
        # every exit keys off. No record means no open contract, hence held=0.
        rec = _load_option_positions().get(_option_key(symbol, opt_type))
        occ = rec.get("occ_symbol") if rec else None
        _note_history_gap(symbol,
                          _current_position(positions, occ) if occ else 0,
                          label=f"{opt_type} option")
        return
    _clear_history_gap(symbol)

    sig = ind.compute_indicators(
        history,
        config.MA_SHORT_PERIOD,
        config.MA_LONG_PERIOD,
        config.RSI_PERIOD,
    )
    if not sig:
        return

    is_call = opt_type.lower() == "call"
    key = _option_key(symbol, opt_type)
    stored = _load_option_positions().get(key)

    if stored:
        # ── Held: EVERY field comes from the store, nothing is recomputed. This
        # is the whole point of the fix — the contract we own does not change
        # identity just because the underlying moved.
        occ_symbol = stored.get("occ_symbol")
        exp_used   = stored.get("expiration") or expiration
        strike     = stored.get("strike") or 0.0

        if not occ_symbol:
            _option_orphan_drops += 1
            _drop_option_position(key, "<none>", "stored record carries no occ_symbol "
                                  f"(orphan drops #{_option_orphan_drops})")
            return

        if _option_expired(exp_used):
            # Nothing to sell — the contract stopped existing. An ITM long was
            # auto-exercised by the broker, an OTM one expired worthless; either
            # way the store must let go or it blocks the pair forever.
            _option_expiry_drops += 1
            _drop_option_position(key, occ_symbol, f"expiration {exp_used} has passed "
                                  f"(expiry drops #{_option_expiry_drops})")
            return

        held = _current_position(positions, occ_symbol)
        if held <= 0:
            # Closed or assigned outside the bot. Clear it so the pair can trade
            # again rather than pinning on a contract nobody holds.
            _option_orphan_drops += 1
            _drop_option_position(key, occ_symbol, "broker no longer reports this "
                                  f"contract (orphan drops #{_option_orphan_drops})")
            return

        # RETIRED 2026-08-06: this used to recompute the ATM symbol every poll and
        # log STALE OPTION RECOVERED when it disagreed with the stored one. It was
        # never the lookup — `held` above resolves against the STORED occ_symbol —
        # so the counter could only ever measure how far the underlying had drifted
        # from the strike, which is not a fault condition. It fired 42 times in the
        # 2026-08-06 session against two healthy positions, and cost one
        # find_option_symbol API call per poll per held contract to say so. The
        # regression it guarded is covered by test_price_move_uses_stored_symbol.
    else:
        # ── Flat per the store: the ATM contract we WOULD open. Strike is chosen
        # at signal time from the underlying (nearest $5) so entries track the
        # market instead of a hardcoded config value.
        strike     = _atm_strike(sig["close"])
        exp_used   = expiration
        occ_symbol = tc.find_option_symbol(symbol, expiration, strike, opt_type)
        if not occ_symbol:
            return

        held = _current_position(positions, occ_symbol)
        if held > 0:
            # A live contract the store does not know about — opened before this
            # file existed. Adopt it so the exit path can manage it from here on.
            # entry_price is unknown at this point; the ledger remains the record
            # of what was actually paid.
            _option_adoptions += 1
            _save_option_position(key, _option_record(occ_symbol, 0.0, exp_used,
                                                      opt_type, strike, sig["close"]))
            logger.info("OPTION POSITION ADOPTED %s (%s) — held at the broker but "
                        "absent from the store; exits now key off the stored symbol "
                        "(adoptions #%d)", key, occ_symbol, _option_adoptions)

    opt_quote   = tc.get_option_quote(occ_symbol)
    entry_price = _option_fill_price(opt_quote, "entry")   # ASK — what a buy pays
    exit_price  = _option_fill_price(opt_quote, "exit")    # BID — what a sell gets

    logger.info(
        "OPTION %s %s %.2f %s | underlying=%.2f  RSI=%.1f  bid=%.2f  ask=%.2f  held=%d",
        symbol, exp_used, strike, opt_type,
        sig["close"], sig["rsi"], exit_price, entry_price, held,
    )

    # NOTE: the old single gate sat here, above BOTH branches, so a contract
    # opened today could not be closed today — the same defect as the equities
    # path. The gate now lives inside the open branch only; closes below are
    # never gated on it.

    # Open new position — an entry, so it waits for the bar to form like every
    # other entry path. Options run off the same underlying's daily bar, so a
    # 9:30:05 open would be bought on the same stub EMAs as QQQ was.
    if held == 0:
        if not mh.entries_allowed():
            _note_entry_delayed(occ_symbol,
                                _bullish_cross_edge(sig, occ_symbol) if is_call
                                else _bearish_cross_edge(sig, occ_symbol))
            return
        if _already_bought_today(occ_symbol) or _already_sold_today(occ_symbol):
            return
        # One open path for both sides. The call/put asymmetry is only WHICH edge
        # and WHICH RSI bound apply, so it is two predicates rather than two
        # duplicated _open_option blocks (the duplication is how the stored-symbol
        # write below could have been added to one branch and missed on the other).
        if is_call:
            entry_ok = (_bullish_cross_edge(sig, occ_symbol)
                        and sig["rsi"] < config.RSI_OVERBOUGHT)
        else:
            entry_ok = (_bearish_cross_edge(sig, occ_symbol)
                        and sig["rsi"] > config.RSI_OVERSOLD)
        if entry_ok and _open_option(account_id, occ_symbol, "buy_to_open",
                                     entry_price, symbol, exp_used, strike,
                                     opt_type, sig):
            _mark_bought(occ_symbol)
            # Persist immediately: from here every exit keys off THIS symbol, so
            # the underlying is free to move without orphaning the contract.
            _save_option_position(key, _option_record(
                occ_symbol, entry_price, exp_used, opt_type, strike, sig["close"]))

    # Close existing position on the opposite STATE (not edge) — same fix as the
    # equities exits: a long call stranded by a missed bearish edge would ride to
    # expiry. A call is long the underlying, a put is short it, so they take the
    # long/short exit helpers respectively.
    elif held > 0:
        # ── Premium-based exits FIRST. The EMA state below is a view on the
        # UNDERLYING; it says nothing about what the contract is worth. A call can
        # lose half its premium to theta and a small adverse move while the EMAs
        # stay bullish, and before this block the only way out was expiry. These
        # are de-risking, so like the equities stop they run ahead of the signal.
        entry_ref = (stored or {}).get("entry_price") if stored else None
        reason    = _option_exit_reason(exit_price, entry_ref, exp_used)
        if reason:
            if _close_option(account_id, occ_symbol, held, exit_price,
                             symbol, exp_used, strike, opt_type, sig):
                _mark_sold(occ_symbol)
                _close_option_position(key)
                if reason.startswith("stop loss"):
                    _option_stop_exits += 1
                    tag, n = "stop exits", _option_stop_exits
                elif reason.startswith("profit target"):
                    _option_target_exits += 1
                    tag, n = "target exits", _option_target_exits
                else:
                    _option_expiry_exits += 1
                    tag, n = "expiry exits", _option_expiry_exits
                logger.warning("OPTION TARGET EXIT %s %s x%d @ %.2f — %s (%s #%d)",
                               key, occ_symbol, held, exit_price, reason, tag, n)
            return

        exit_state = (_bearish_state(sig, occ_symbol) if is_call
                      else _bullish_state(sig, occ_symbol))
        edge_key = "bearish_cross" if is_call else "bullish_cross"
        if exit_state and _close_option(account_id, occ_symbol, held, exit_price,
                                        symbol, exp_used, strike, opt_type, sig):
            _mark_sold(occ_symbol)
            _close_option_position(key)
            _note_state_only_exit(occ_symbol, sig, edge_key)


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
        # Replaces a bare WARNING that named the outage but never said a held
        # contract had gone unevaluated, and carried no counter.
        _note_history_gap(sig_symbol,
                          _current_position(positions, trade_symbol),
                          label=root)
        return
    _clear_history_gap(sig_symbol)

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
            _log_exit_trade("SELL", stale.get("symbol"), qty, price, order_id,
                            f"{root} roll: flatten expiring contract", account_id)
        return

    qty = config.FUTURES_CONTRACTS

    # SELL — flatten the long on bearish STATE, before the entry gate, same as
    # equities. Uses the FUTURES clock: the ES daily bar runs 18:00 -> 17:00 ET,
    # so its unformed stub window is the evening reopen, not the 9:30 bell.
    if held > 0 and _exit_long_signal(sig, trade_symbol) \
            and not _already_sold_today(trade_symbol):
        logger.info("SIGNAL SELL %s x%d", trade_symbol, held)
        result = tc.place_futures_order(account_id, trade_symbol, "sell", held)
        if result:
            _mark_sold(trade_symbol)
            order_id = result.get("order", {}).get("id")
            _log_exit_trade("SELL", trade_symbol, held, price, order_id,
                            f"{root} EMA bearish, RSI={sig['rsi']:.1f}", account_id)
            _note_state_only_exit(trade_symbol, sig, "bearish_cross")
        return

    if not fmh.entries_allowed():
        _note_entry_delayed(trade_symbol,
                            held == 0 and _bullish_cross_edge(sig, trade_symbol))
        return

    if _already_bought_today(trade_symbol) or _already_sold_today(trade_symbol):
        return

    # VIX regime gate — futures have no momentum slot or bot-managed stop, so the
    # filter reduces to blocking new entries in defensive/crisis (the roll-flatten
    # and state exit above are de-risking and stay ungated).
    # (futures have no short path, so block_shorts is unused here)
    block_new_entries, _, _ = _apply_regime_rules(regime)

    # BUY signal — open long front month (EDGE: it is an entry)
    if (_bullish_cross_edge(sig, trade_symbol) and sig["rsi"] < config.RSI_OVERBOUGHT
            and held == 0 and not block_new_entries):
        logger.info("SIGNAL BUY %s x%d", trade_symbol, qty)
        result = tc.place_futures_order(account_id, trade_symbol, "buy", qty)
        if result:
            _mark_bought(trade_symbol)
            order_id = result.get("order", {}).get("id")
            # Futures carry no bot-managed stop (see note above), so the fill is
            # not used to arm one — but resolve it anyway to record real fill vs
            # signal slippage in the trade log, same as the equity entries.
            _, fill_px, slippage = _resolve_fill(trade_symbol, account_id, order_id,
                                                 price, "BUY")
            log_trade("BUY", trade_symbol, qty, price, "market", order_id,
                      f"{root} EMA cross up, RSI={sig['rsi']:.1f}",
                      fill_price=fill_px, signal_price=price, slippage=slippage)


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
        _log_exit_trade("SELL_TO_CLOSE", occ_symbol, held, price, order_id,
                        f"{symbol} reversal, RSI={sig['rsi']:.1f}, "
                        f"strike={strike} {opt_type} exp={exp}", account_id)
    return result
