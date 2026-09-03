#!/usr/bin/env python3
"""
TradeStation Paper-Trading Bot
==============================
Starts automatically at NYSE open (9:30 AM ET), evaluates each symbol on the
effective stock watchlist (core + momentum slot + held; see watchlist.py) and
OPTIONS_WATCHLIST every POLL_INTERVAL seconds using EMA-crossover + RSI signals,
logs all trades and performance, then shuts down cleanly at market close.

Usage:
    # One-time: authorize and save your refresh token
    python3 auth_setup.py
    # Then run the bot (reads credentials from .env)
    python3 main.py
"""

import argparse
import os
import sys


def _resolve_mode() -> str:
    parser = argparse.ArgumentParser(description="TradeStation paper-trading bot")
    parser.add_argument("--mode", choices=["equities", "futures"], default="equities",
                        help="equities (stocks + options) or futures")
    args, _ = parser.parse_known_args()
    return args.mode


# Resolve mode and export BOT_MODE BEFORE importing config/trade_logger, which
# select their lock file and log filenames from it at import time.
MODE = _resolve_mode()
os.environ["BOT_MODE"] = MODE

import fcntl
import logging
import signal
import threading
import time

import trade_logger  # noqa: F401 – configures logging as side-effect
import config
import tradestation_client as tc
import market_hours as mh
import futures_market_hours as fmh
import strategy
import sentiment_analyzer
import watchlist
import momentum_screen
from trade_logger import log_performance

logger = logging.getLogger("bot")

_shutdown = threading.Event()

# Cycles abandoned because the positions fetch failed (see _evaluate_cycle). Counted
# so the banner can report whether the guard is still firing; _consecutive is
# tracked separately because a sustained outage is a different problem from an
# isolated blip and should be louder.
_positions_fetch_failures = 0
_positions_fetch_consecutive = 0

# Per-cycle work counters. Reset by _run_cycle, incremented BEFORE each evaluate
# call, and read by its single timing log line. Incrementing first is what makes
# a partial cycle legible: if a broker-side throttle stalls the run, the count
# says how many symbols got through rather than reporting zero.
#
# These measure ATTEMPTED evaluations, not successful ones — each loop catches
# its own exceptions and continues, so a symbol that errored still counted as
# work (it consumed the API call and the wall-clock).
_cycle_symbols = 0
_cycle_options = 0

# Consecutive skips before the log escalates WARNING -> ERROR. At a 60s poll,
# 3 skips is ~3 minutes with no stop enforcement (stops are bot-managed, so an
# outage suspends them entirely — the same hole as the overnight gap).
_POSITIONS_FAILURE_ESCALATE_AFTER = 3

# Per-mode clock, account getter and singleton lock so an equities instance and a
# futures instance can run as independent processes.
_clock       = fmh if MODE == "futures" else mh
_get_account = tc.get_futures_account_id if MODE == "futures" else tc.get_account_id

_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), config.LOCK_FILE)
_lock_fh = None  # module-global so the fd isn't GC'd (that would release the lock)


def _acquire_singleton_lock() -> None:
    """Refuse to start if another bot instance already holds the lock.

    Uses an advisory flock held for the process lifetime. The kernel releases
    it automatically when this process exits — including on crash or SIGKILL —
    so there is never a stale lock to clean up (unlike a bare pidfile)."""
    global _lock_fh
    _lock_fh = open(_LOCK_PATH, "a+")            # "a+" so we DON'T truncate a live lock's contents
    try:
        fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error(
            "Another bot instance already holds %s — refusing to start a "
            "second instance. (Use restart.sh to replace the running one.)",
            _LOCK_PATH,
        )
        sys.exit(1)
    # We hold the lock — now safe to record our PID for humans/tooling.
    _lock_fh.seek(0)
    _lock_fh.truncate()
    _lock_fh.write(f"{os.getpid()}\n")
    _lock_fh.flush()


def _handle_signal(signum, frame):
    logger.info("Shutdown signal received (%s).", signum)
    _shutdown.set()


def _run_cycle(account_id: str) -> None:
    """Time one evaluation pass and log what it cost, then hand off to
    _evaluate_cycle for the actual work.

    ONE log site, in a `finally`, deliberately: _evaluate_cycle has three exits
    (skipped cycle on a failed positions fetch, the futures early return, and
    the equities fall-through) and the caller catches its exceptions. Logging at
    each exit would be four sites drifting apart; this way every cycle emits
    exactly one timing line, including the ones that fail.

    The counter is module-level rather than a return value so a cycle that
    raises PART WAY THROUGH the symbol loop still reports how far it got — the
    exact signal a broker-side throttle would produce.
    """
    global _cycle_symbols, _cycle_options
    _cycle_symbols = 0
    _cycle_options = 0
    started = time.perf_counter()
    try:
        _evaluate_cycle(account_id)
    finally:
        logger.info("cycle work=%.3fs symbols=%d options=%d",
                    time.perf_counter() - started, _cycle_symbols, _cycle_options)


def _evaluate_cycle(account_id: str) -> None:
    """One evaluation pass over the active watchlist.

    Abandons the cycle outright when the positions fetch fails. Everything below
    is derived from `positions` — held quantity, stop trailing, the effective
    watchlist, the performance snapshot — so a failed fetch invalidates the whole
    pass, not just the entry paths. Skipping costs one poll and loses nothing:
    with holdings unknown every symbol reads held=0, and no exit or stop check
    can fire on held=0 anyway (that is already true today). What it prevents is
    the entry paths reading held=0 as "flat" and re-entering held positions."""
    global _positions_fetch_failures, _positions_fetch_consecutive
    global _cycle_symbols, _cycle_options

    positions = tc.get_positions(account_id)
    if positions is None:                     # None = fetch failed; [] = truly flat
        _positions_fetch_failures += 1
        _positions_fetch_consecutive += 1
        escalate = _positions_fetch_consecutive >= _POSITIONS_FAILURE_ESCALATE_AFTER
        logger.log(
            logging.ERROR if escalate else logging.WARNING,
            "Skipping cycle — positions fetch failed; holdings UNKNOWN, not flat. "
            "No entries, exits or stop checks this pass (%d consecutive, "
            "skipped cycles #%d)%s",
            _positions_fetch_consecutive, _positions_fetch_failures,
            " — SUSTAINED OUTAGE: stops are unenforced while this persists."
            if escalate else "",
        )
        return
    _positions_fetch_consecutive = 0

    # Market-wide regime, once per cycle: the MORE FEARFUL of the VIX regime (cached
    # 5 min) and the Claude-sentiment regime (from the 08:00 report; NEUTRAL if
    # missing/stale). `blocked` = symbols in a sentiment "high"-risk sector, gated
    # from NEW long entries. Both modes use `regime`; equities also use `blocked`.
    vix, vix_regime = strategy.current_regime()
    sentiment = sentiment_analyzer.current_sentiment()
    sent_regime = sentiment_analyzer.sentiment_regime(sentiment)
    regime = strategy.effective_regime(vix_regime, sent_regime,
                                       sentiment.get("fear_score"))
    blocked = sentiment_analyzer.sectors_blocked(sentiment)
    if config.ENABLE_VIX_FILTER or config.ENABLE_SENTIMENT:
        strategy.note_regime(vix, regime, vix_regime=vix_regime, sent_regime=sent_regime,
                             fear=sentiment.get("fear_score"),
                             risks=sentiment.get("top_risks"))

    balance   = tc.get_account_balance(account_id)
    equity    = balance.get("total_equity") if balance else None
    log_performance(account_id, balance, positions)

    # Prune trailing-stop records for positions we no longer hold (once per cycle,
    # before per-symbol evaluation). Guarded internally against an empty/failed
    # positions fetch too.
    #
    # Runs in BOTH modes now, and that is only safe because STOP_PRICE_FILE
    # carries _PROC_SUFFIX: each process prunes against its own positions list in
    # its own file. It used to sit below the futures early-return precisely
    # because the file was shared, and pruning equity stops against a futures
    # positions list (or the reverse) deletes every record and re-bootstraps it
    # with a reset water-mark — silently loosening a ratcheted stop. If the
    # per-process file split is ever undone, this call has to move back down.
    strategy.reconcile_stops(positions)

    # Broker-native stop floors: cancel any GTC stop left resting behind a
    # position we no longer hold, and re-arm a held position whose floor the
    # broker says is gone. Self-latching to a single pass per process, so this
    # costs one working-orders fetch at startup and nothing thereafter. No-op
    # while ENABLE_BROKER_STOP_FLOOR is False. Also both modes: the futures
    # account has its own id and its own working orders, so the two processes
    # cannot see or cancel each other's floors.
    strategy.reconcile_broker_floors(positions, account_id)

    if MODE == "futures":
        for root in config.FUTURES_WATCHLIST:
            _cycle_symbols += 1
            try:
                strategy.evaluate_future(root, account_id, positions, regime)
            except Exception as exc:
                logger.error("Error evaluating future %s: %s", root, exc)
        return

    # Momentum slot + rotation id, read once per cycle. is_momentum drives the
    # one-shot alignment entry; generation re-arms the latch each new rotation.
    momentum_symbols, generation = watchlist.momentum_slot()
    momentum_set = set(momentum_symbols)
    strategy.reconcile_momentum_entries(momentum_symbols, positions, generation)

    # Crisis de-risking is applied per-symbol inside evaluate_stock (momentum exits
    # via the normal SELL path) and _check_and_trail_stop (breakeven-floored stops),
    # so there is no separate bulk step here — the regime flows in via evaluate_stock.
    for symbol in watchlist.effective_stock_watchlist(positions):
        _cycle_symbols += 1
        try:
            strategy.evaluate_stock(symbol, account_id, positions, equity,
                                    is_momentum=(symbol in momentum_set),
                                    momentum_generation=generation,
                                    regime=regime, blocked_symbols=blocked)
        except Exception as exc:
            logger.error("Error evaluating stock %s: %s", symbol, exc)

    expiration = mh.next_monthly_expiration()
    for (symbol, opt_type) in config.OPTIONS_WATCHLIST:
        _cycle_options += 1
        try:
            strategy.evaluate_option(symbol, expiration, opt_type,
                                     account_id, positions)
        except Exception as exc:
            logger.error("Error evaluating option %s %s: %s", symbol, expiration, exc)


def _wait_for_market_open() -> None:
    secs = _clock.seconds_until_open()
    if secs > 0:
        logger.info("Market closed. Sleeping %.0f s until next open (%s).",
                    secs, _clock.describe_next_open())
        # Wait on the shutdown Event so SIGTERM wakes us instantly, while the
        # timeout still lets us re-check the market-open time periodically.
        while secs > 0 and not _shutdown.is_set():
            chunk = min(secs, 30)
            if _shutdown.wait(chunk):
                return
            secs -= chunk
            if _clock.is_market_open():
                break


def _log_sentiment_banner(rep: dict) -> None:
    """Startup banner for the sentiment overlay. A function rather than inline
    banner code so the fallback branch is testable without booting the bot (main()
    takes the singleton lock and starts trading)."""
    if rep.get("fallback"):
        # The old banner buried this as a " [FALLBACK]" suffix at the end of a line
        # whose headline numbers (fear=1/10 regime=risk_on) read exactly like a calm
        # live reading — the most reassuring output the overlay can produce is also
        # what it emits when it is completely dead. fear=1 is a hardcoded constant in
        # _neutral_report, not a measurement, so lead with the failure and drop the
        # fake score entirely rather than printing a number nobody measured.
        logger.warning("Sentiment   : FALLBACK (API unavailable) — %s",
                       rep.get("summary") or "no live report")
        # What a fallback actually costs depends on whether the override is live.
        # With it ON, the overlay losing its voice on the regime is the headline;
        # with it OFF the regime was never listening, so the only real loss is the
        # sector gate. Saying "VIX-only regime active" in the second case would
        # describe a degradation that is just the configured behaviour.
        if getattr(config, "ENABLE_SENTIMENT_OVERRIDE", True):
            logger.warning("              ⚠️  VIX-only regime active: the overlay cannot "
                           "raise the regime to cautious, and every sector reads 'low', "
                           "so NO sector can block a long entry.")
        else:
            logger.warning("              ⚠️  Regime is VIX-only by config anyway; the "
                           "loss here is the sector gate — every sector reads 'low', "
                           "so NO sector can block a long entry.")
    else:
        logger.info("Sentiment   : fear=%s/10 regime=%s risks=%s (model=%s, "
                    "weekdays 08:00 ET, stale>%dh, cap=$%.2f)",
                    rep.get("fear_score"), rep.get("regime"),
                    rep.get("top_risks") or [],
                    config.SENTIMENT_MODEL, config.SENTIMENT_MAX_AGE_HOURS,
                    config.SENTIMENT_MAX_COST_USD)
    # The Combine line states the rule actually in force. It described the
    # MORE-FEARFUL combine unconditionally, which would be a lie about the live
    # regime the moment ENABLE_SENTIMENT_OVERRIDE went False.
    if getattr(config, "ENABLE_SENTIMENT_OVERRIDE", True):
        # The floor is part of the rule in force, so it belongs on this line. The
        # live fear score is printed two lines up; stating the threshold beside it
        # is what makes the banner answer "is sentiment steering the regime RIGHT
        # NOW?" without reading config.
        _floor = getattr(config, "SENTIMENT_OVERRIDE_MIN_FEAR", 0)
        _fear = rep.get("fear_score")
        _heard = strategy.sentiment_participates(_fear)
        logger.info("Combine     : effective regime = MORE FEARFUL of (VIX, sentiment) "
                    "when fear >= %s, else VIX alone. Live fear=%s → sentiment %s. "
                    "Sentiment can only RAISE the regime, never lower it. A 'high' "
                    "sector blocks new long entries regardless of fear. "
                    "Counter: SENTIMENT BELOW THRESHOLD",
                    # Deliberately NOT the words "INFO ONLY" — that phrase is the
                    # established marker for the master switch being OFF, and a
                    # below-threshold score is a different state with a different
                    # fix (raise the score vs flip the switch). Reusing it would
                    # make the two indistinguishable in the log.
                    _floor, _fear,
                    "IS steering the regime" if _heard
                    else "is NOT steering (below threshold)")
    else:
        logger.info("Combine     : INFO ONLY — effective regime = VIX alone "
                    "(ENABLE_SENTIMENT_OVERRIDE=False; sentiment never raises the "
                    "regime). Sector gating is UNAFFECTED: a 'high' sector still "
                    "blocks new long entries in that sector. Counter: SENTIMENT ADVISORY")


def _render_rungs(steps) -> str:
    """One profit-floor ladder as a banner string: '15%→10%, 20%→15%, ...'.

    Rendered FROM config on both sides rather than hand-written, so the banner
    cannot drift from the ladder the way the shorting banner once did. A helper
    rather than an inline comprehension because there are now two ladders to
    render and a third caller (the analyzer) is the obvious next one.
    """
    return ", ".join("%.0f%%→%.0f%%" % (t * 100, lk * 100) for t, lk in steps)


def main() -> None:
    _acquire_singleton_lock()          # hard-stop a second instance before any API calls
    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("=" * 60)
    logger.info("TradeStation Trading Bot starting up  [mode=%s]", MODE.upper())
    if config.TS_SANDBOX:
        logger.info("Environment : SANDBOX (paper trading)")
    else:
        logger.warning("=" * 60)
        logger.warning("  !! LIVE TRADING MODE — REAL MONEY AT RISK !!")
        logger.warning("  Set TS_SANDBOX=true in .env to use paper trading.")
        logger.warning("=" * 60)
    logger.info("API URL     : %s", config.TS_BASE_URL)
    if MODE == "futures":
        contracts = {root: fmh.front_month_contract(root, roll_days=config.FUTURES_ROLL_DAYS)
                     for root in config.FUTURES_WATCHLIST}
        logger.info("Futures     : %s", config.FUTURES_WATCHLIST)
        logger.info("Front months: %s", contracts)
    else:
        logger.info("Core stocks : %s (%d)", config.CORE_WATCHLIST, len(config.CORE_WATCHLIST))
        logger.info("Momentum    : %s (dynamic, %s)",
                    watchlist._load_momentum_symbols(), config.MOMENTUM_WATCHLIST_FILE)
        # Exercise the same union the trading loop uses so startup logging proves
        # effective_stock_watchlist() is wired. Held names aren't known until the
        # account is fetched (below), so they're shown as folding in live.
        active = watchlist.effective_stock_watchlist([])
        logger.info("Active list : %s (%d; core ∪ momentum, held names fold in live)",
                    active, len(active))
        logger.info("Options     : %s", config.OPTIONS_WATCHLIST)
        logger.info("Next option exp.: %s", mh.next_monthly_expiration())
        logger.info("Stop loss   : %s (regime ATR mult — risk_on %.1fx/cautious %.1fx/"
                    "defensive %.1fx/crisis %.1fx; ATR%d, trails at armed width, file=%s)",
                    "ENABLED" if config.USE_TRAILING_STOP else "DISABLED",
                    config.ATR_MULT_RISK_ON, config.ATR_MULT_CAUTIOUS,
                    config.ATR_MULT_DEFENSIVE, config.ATR_MULT_CRISIS,
                    config.STOP_LOSS_ATR_PERIOD, config.STOP_PRICE_FILE)
        logger.info("Mom. align  : %s (one-shot/rotation, RSI<%d, file=%s)",
                    "ENABLED" if config.USE_MOMENTUM_ALIGNMENT else "DISABLED",
                    config.MOMENTUM_ALIGN_RSI_MAX, config.MOMENTUM_ENTRY_FILE)
        logger.info("Latch repair: ON — a held momentum name with no latch is "
                    "rebuilt each cycle from BROKER POSITIONS (not stop records: "
                    "the same wipe takes both)")
        # The parenthetical has to describe the ACTIVE state, not the feature. It
        # read "DISABLED (effective watchlist, death-cross entries)" on 2026-08-03,
        # which describes what shorting would do if it were on — and the banner is
        # what gets read as the source of truth for what the bot is doing.
        if config.ENABLE_SHORTING:
            logger.info("Shorting    : ENABLED (effective watchlist, death-cross entries)")
        else:
            logger.info("Shorting    : DISABLED — long-only; no NEW short entries. "
                        "ENTRY-only gate: open shorts still trail, stop and cover "
                        "normally.")
        if config.ENABLE_SHORTING and config.ENABLE_REGIME_SHORT_FILTER:
            # DERIVED, not described. This line used to hardcode "(cautious/
            # defensive only) ... risk_on and unknown blocked ... shorts fire ONLY
            # in cautious", which silently became false the moment
            # SHORT_MIN_REGIME moved to "risk_on" on 2026-08-03 — the banner then
            # contradicted itself in a single sentence. Ask the real gate instead,
            # so the banner cannot drift from the code again.
            _openable = [r for r in ("risk_on", "cautious", "defensive", "crisis",
                                     "unknown")
                         if not strategy._apply_regime_rules(r)[0]
                         and not strategy._apply_regime_rules(r)[2]]
            # "Counter: REGIME BLOCK" dropped from this line 2026-08-03. It was
            # advertising a counter that had just become unreachable at a risk_on
            # floor. The counter itself is UNCHANGED in strategy.py and still emits
            # its REGIME BLOCK log line whenever it fires — only the banner
            # advertisement is gone, so nothing observable was lost.
            logger.info("Short regime filter: ENABLED — NEW shorts require regime "
                        ">= %s, and 'unknown' (VIX outage) always blocks. Regimes a "
                        "short can actually OPEN in: %s (defensive/crisis are already "
                        "closed by block_new_entries, so they never appear). "
                        "ENTRY-only: open shorts still trail, stop and cover normally.",
                        config.SHORT_MIN_REGIME, _openable or "NONE")
        elif config.ENABLE_SHORTING:
            logger.info("Short regime filter: DISABLED (shorts gated only by "
                        "ENABLE_SHORTING and defensive/crisis)")
        if config.ENABLE_PROFIT_TAKING:
            logger.info("Profit take : ENABLED (>= +%.0f%% & RSI >= %.0f -> sell %.0f%%, one-shot, stop kept on remainder)",
                        config.PROFIT_TAKE_PCT * 100, config.PROFIT_TAKE_RSI_MIN,
                        config.PROFIT_TAKE_FRACTION * 100)
        else:
            logger.info("Profit take : DISABLED (enable via ENABLE_PROFIT_TAKING; would sell %.0f%% at +%.0f%% & RSI >= %.0f)",
                        config.PROFIT_TAKE_FRACTION * 100, config.PROFIT_TAKE_PCT * 100,
                        config.PROFIT_TAKE_RSI_MIN)
        if config.ENABLE_OPTION_EXIT_TARGETS:
            logger.info("Option exits: ENABLED (bid >= %.0f%% of entry -> target; "
                        "bid <= %.0f%% -> stop; <= %d TRADING SESSIONS to expiry -> "
                        "close, ~%d calendar days). Priced off "
                        "the BID, checked BEFORE the EMA-state exit. Adopted contracts "
                        "(entry_price 0.0) skip target/stop but keep the expiry rule. "
                        "Counters: OPTION TARGET EXIT",
                        config.OPTION_PROFIT_TARGET_PCT * 100,
                        config.OPTION_STOP_LOSS_PCT * 100,
                        config.OPTION_MIN_DAYS_TO_EXPIRY,
                        # Calendar equivalent from today, so the banner cannot be
                        # misread as calendar days — it drifts with the weekday.
                        (mh.shift_trading_days(mh.now_et().date(),
                                               config.OPTION_MIN_DAYS_TO_EXPIRY)
                         - mh.now_et().date()).days)
        else:
            logger.info("Option exits: DISABLED (enable via ENABLE_OPTION_EXIT_TARGETS) "
                        "— options exit on EMA STATE ONLY, so a contract can bleed to "
                        "zero while the underlying's EMAs stay favourable")
        try:
            _excl, _univ = momentum_screen.count_excluded_universe()
            logger.info("Sector filter: %d of %d universe excluded %s "
                        "(applied at momentum screen, not per-cycle)",
                        _excl, _univ, config.EXCLUDED_SECTORS)
        except Exception as exc:
            logger.warning("Sector filter: could not summarize (%s)", exc)

    # Both modes: state exits and the entry delay apply to stocks, options AND
    # futures, so this reports outside the mode branch. The delay anchors to each
    # mode's own session open — 9:30 ET for equities, 18:00 ET for CME.
    logger.info("Exit logic  : STATE (EMA%d</>EMA%d), not edge — exits fire on "
                "trend state; entries still need a cross",
                config.MA_SHORT_PERIOD, config.MA_LONG_PERIOD)
    logger.info("Entry delay : %d min after the %s session open (entries only; "
                "exits + stops live from the open)",
                config.CROSS_ENTRY_DELAY_MINUTES,
                "CME 18:00 ET" if MODE == "futures" else "9:30 ET")
    # Both modes: the breakeven lock lives in _check_and_trail_stop, so it reports
    # outside the mode branch (equities and futures stops both floor at entry).
    logger.info("Breakeven   : %s",
                ("ENABLED (%.1fxATR — floor stop at entry once +1 ATR in profit; "
                 "longs+shorts, retroactive w/ underwater guard). Counters: "
                 "BREAKEVEN LOCK (armed), BREAKEVEN LOCK EXIT (fired, and only "
                 "when the raw trail would NOT have). The first is a substring "
                 "of the second — a trailing space does NOT separate them — so "
                 "count armings with `grep 'BREAKEVEN LOCK' | grep -v 'LOCK "
                 "EXIT'`. Both counters are per-process and reset on restart: "
                 "to answer 'has this ever fired', grep bot.log* plus the .gz "
                 "archives, never the live counter"
                 % config.BREAKEVEN_LOCK_ATR)
                if config.ENABLE_BREAKEVEN_LOCK else "DISABLED")
    logger.info("Profit floor: %s",
                ("ENABLED — asymmetric ladders; complements the ATR trail and "
                 "breakeven lock (stop = most protective of the three), so a "
                 "rung only binds when the trail is wider. longs: %s | shorts: "
                 "%s. Broker GTC raise: %s. Counter: PROFIT FLOOR (long: %d, "
                 "short: %d this process) — split per direction because the two "
                 "ladders are different instruments (short first rung +2%%->1%% "
                 "is a 1pp gap; long is +15%%->10%%), so a combined total cannot "
                 "say whether the short micro-rungs earn their keep. Both read "
                 "0 at startup by definition; the durable per-direction record "
                 "is the log suffix `— long floors #N` / `— short floors #N`, so "
                 "grep bot.log* plus the .gz archives, never the live counter"
                 % (_render_rungs(config.PROFIT_FLOOR_STEPS_LONG),
                    _render_rungs(config.PROFIT_FLOOR_STEPS_SHORT),
                    "ON" if config.ENABLE_PROFIT_FLOOR_BROKER_RAISE else "OFF",
                    strategy._profit_floors_long,
                    strategy._profit_floors_short))
                if config.ENABLE_PROFIT_FLOOR else "DISABLED")
    logger.info("Water floor : %s",
                ("ENABLED (k=%.2f ATR behind the best excursion; equities AND "
                 "futures, one path). REPLACES rather than complements the ATR "
                 "trail: both are anchored to the same water mark and k=%.2f < "
                 "%.1fx, so once a position is >%.2f ATR past entry its effective "
                 "trail is %.2f ATR wide, at EVERY excursion — not just inside the "
                 "breakeven window. Arms at %.2f ATR, BEFORE the breakeven lock's "
                 "%.1f ATR, and is strictly more protective once armed, so the "
                 "lock now only binds on positions where the floor is blocked. "
                 "TWO GUARDS: not armed unless strictly past entry (else it would "
                 "steal the lock's label), and never armed through the market "
                 "(else a retroactive apply is an instant exit — it would have "
                 "closed ESU26 and NQU26 on 2026-08-31). Counter: WATER FLOOR "
                 "(long: %d, short: %d this process); durable record is the log "
                 "suffix `— long water floors #N` / `— short water floors #N`, so "
                 "grep bot.log* plus the .gz archives, never the live counter"
                 % (config.WATER_FLOOR_K, config.WATER_FLOOR_K,
                    config.STOP_LOSS_ATR_MULT, config.WATER_FLOOR_K,
                    config.WATER_FLOOR_K, config.WATER_FLOOR_K,
                    config.BREAKEVEN_LOCK_ATR,
                    strategy._water_floors_long,
                    strategy._water_floors_short))
                if config.ENABLE_WATER_FLOOR else "DISABLED")
    logger.info("Cross gap   : %.2f%% minimum EMA separation on ALL cross signals "
                "(long entry/exit, short entry/cover, options, futures); a "
                "suppressed EXIT is deferred, not cancelled — states re-derive "
                "every poll. Counter: CROSS GAP BLOCK",
                config.EMA_CROSS_MIN_GAP_PCT * 100)
    logger.info("Cross sustain: %s — ENTRY crosses only (long entry, short entry, "
                "options, futures); exits stay ungated by design (exit-side "
                "variants backtested NEGATIVE). Clock is in-memory: a restart "
                "re-arms it, deferring an entry, never rushing one. "
                "Counter: CROSS SUSTAIN BLOCK",
                (f"{config.CROSS_SUSTAIN_MINUTES} min minimum cross persistence "
                 f"(PROVISIONAL — fit in-sample on 25 trips)")
                if config.CROSS_SUSTAIN_MINUTES > 0 else "DISABLED")
    logger.info("Pos. guard  : a FAILED positions fetch skips the whole cycle "
                "(unknown != flat); ERROR after %d consecutive — stops are "
                "unenforced during an outage",
                _POSITIONS_FAILURE_ESCALATE_AFTER)
    # Both modes: exit submission is shared, so this reports outside the branch.
    logger.info("Exit alerts : CRITICAL on rejection (broker refused the exit — "
                "position still open) and on a floor cancel that will not "
                "confirm (exit withheld — position may be open AND unprotected). "
                "Both retry next cycle; a REPEATING one never self-clears. Goes "
                "to bot.log AND %s at the repo root (never rotated — bot.log "
                "holds ONE day and weekends produce no rotated file at all). No "
                "push channel, so nothing pages you: check %s on Mondays. "
                "Counters: EXIT ORDER REJECTED, BROKER FLOOR stuck",
                config.CRITICAL_ALERT_FILE, config.CRITICAL_ALERT_FILE)
    if config.ENABLE_VIX_FILTER:
        logger.info("VIX filter  : ENABLED — %s, %ds cache; risk_on/cautious/"
                    "defensive/crisis @ <%g/%g/%g/>=%g (crisis>=%g EXTREME); "
                    "crisis actions=%s",
                    config.VIX_SYMBOL, config.VIX_CACHE_SECONDS,
                    config.VIX_NORMAL, config.VIX_CAUTIOUS, config.VIX_DEFENSIVE,
                    config.VIX_DEFENSIVE, config.VIX_CRISIS,
                    "SHADOW" if config.VIX_CRISIS_SHADOW else "LIVE")
    else:
        logger.info("VIX filter  : DISABLED (always risk_on)")
    if config.ENABLE_SENTIMENT:
        _rep = sentiment_analyzer.current_sentiment()
        _log_sentiment_banner(_rep)
    else:
        logger.info("Sentiment   : DISABLED")

    # Current arming width: the ATR multiple the NEXT entry would arm its stop at,
    # by the effective (VIX ⊕ sentiment) regime. Fail-open — a startup VIX glitch
    # must not abort the banner (or the bot), so this is best-effort only.
    try:
        _vix, _vix_reg = strategy.current_regime()
        _eff_reg = _vix_reg
        if config.ENABLE_SENTIMENT:
            _eff_reg = strategy.effective_regime(
                _vix_reg, sentiment_analyzer.sentiment_regime(_rep),
                (_rep or {}).get("fear_score"))
        # No single number any more: width is regime x volatility band, and the
        # band is per-symbol (ATR/price at entry), which the banner cannot know.
        # Print the whole row for the effective regime instead of one figure that
        # would be wrong for every high- or low-ATR name.
        _row = config.ATR_MULT_BY_REGIME_AND_BAND.get(_eff_reg)
        if _row:
            logger.info("Stop mult   : %s — low-vol %.2fx / normal %.2fx / high-vol %.2fx "
                        "(band = ATR/price at entry: <=%.0f%% / <=%.0f%% / >%.0f%%)",
                        _eff_reg, _row[0], _row[1], _row[2],
                        config.ATR_PCT_LOW_THRESHOLD * 100,
                        config.ATR_PCT_HIGH_THRESHOLD * 100,
                        config.ATR_PCT_HIGH_THRESHOLD * 100)
        else:
            logger.info("Stop mult   : %.1fx (%s) — unknown regime, plain regime width",
                        strategy._regime_atr_mult(_eff_reg), _eff_reg)
    except Exception as exc:
        logger.warning("Stop mult   : could not resolve current regime (%s)", exc)
    logger.info("=" * 60)

    if not (config.TS_CLIENT_ID and config.TS_CLIENT_SECRET and config.TS_REFRESH_TOKEN):
        logger.error("TradeStation credentials are incomplete. "
                     "Set TS_CLIENT_ID and TS_CLIENT_SECRET in .env, then run "
                     "`python3 auth_setup.py` to obtain TS_REFRESH_TOKEN.")
        sys.exit(1)

    account_id = _get_account()
    if not account_id:
        logger.error("Could not retrieve a %s account ID. Check your API token / "
                     "account entitlements.", MODE)
        sys.exit(1)
    logger.info("Using account: %s", account_id)

    balance = tc.get_account_balance(account_id)
    if balance:
        logger.info("Balance     : equity=$%.2f  cash=$%.2f",
                    balance.get("total_equity") or 0.0,
                    balance.get("total_cash") or 0.0)
    else:
        logger.warning("Could not retrieve account balance at startup.")

    while not _shutdown.is_set():
        if not _clock.is_market_open():
            _wait_for_market_open()
            if _shutdown.is_set():
                break
            continue

        logger.info("Market is OPEN. Starting trading session.")

        # Trading loop: run until close or shutdown signal
        while not _shutdown.is_set() and _clock.is_market_open():
            try:
                _run_cycle(account_id)
            except Exception as exc:
                logger.exception("Unexpected error in run cycle: %s", exc)

            # Wait for POLL_INTERVAL, waking early on shutdown
            remaining = config.POLL_INTERVAL
            while remaining > 0 and not _shutdown.is_set():
                chunk = min(remaining, 5)
                if _shutdown.wait(chunk):
                    break
                remaining -= chunk
                if not _clock.is_market_open():
                    break

        if not _shutdown.is_set():
            logger.info("Market CLOSED for the day. Bot going to sleep.")

    logger.info("Bot shut down cleanly.")


if __name__ == "__main__":
    main()
