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

# Cycles abandoned because the positions fetch failed (see _run_cycle). Counted
# so the banner can report whether the guard is still firing; _consecutive is
# tracked separately because a sustained outage is a different problem from an
# isolated blip and should be louder.
_positions_fetch_failures = 0
_positions_fetch_consecutive = 0

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
    """One evaluation pass over the active watchlist.

    Abandons the cycle outright when the positions fetch fails. Everything below
    is derived from `positions` — held quantity, stop trailing, the effective
    watchlist, the performance snapshot — so a failed fetch invalidates the whole
    pass, not just the entry paths. Skipping costs one poll and loses nothing:
    with holdings unknown every symbol reads held=0, and no exit or stop check
    can fire on held=0 anyway (that is already true today). What it prevents is
    the entry paths reading held=0 as "flat" and re-entering held positions."""
    global _positions_fetch_failures, _positions_fetch_consecutive

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
    regime = strategy._more_fearful(vix_regime, sent_regime)
    blocked = sentiment_analyzer.sectors_blocked(sentiment)
    if config.ENABLE_VIX_FILTER or config.ENABLE_SENTIMENT:
        strategy.note_regime(vix, regime, vix_regime=vix_regime, sent_regime=sent_regime,
                             fear=sentiment.get("fear_score"),
                             risks=sentiment.get("top_risks"))

    balance   = tc.get_account_balance(account_id)
    equity    = balance.get("total_equity") if balance else None
    log_performance(account_id, balance, positions)

    if MODE == "futures":
        for root in config.FUTURES_WATCHLIST:
            try:
                strategy.evaluate_future(root, account_id, positions, regime)
            except Exception as exc:
                logger.error("Error evaluating future %s: %s", root, exc)
        return

    # Prune trailing-stop records for positions we no longer hold (once per cycle,
    # before per-symbol evaluation). Equities-only — the futures process shares
    # this stop file, and pruning against futures positions would wipe every
    # equity stop. Guarded internally against an empty/failed positions fetch too.
    strategy.reconcile_stops(positions)

    # Momentum slot + rotation id, read once per cycle. is_momentum drives the
    # one-shot alignment entry; generation re-arms the latch each new rotation.
    momentum_symbols, generation = watchlist.momentum_slot()
    momentum_set = set(momentum_symbols)
    strategy.reconcile_momentum_entries(momentum_symbols, positions, generation)

    # Crisis de-risking is applied per-symbol inside evaluate_stock (momentum exits
    # via the normal SELL path) and _check_and_trail_stop (breakeven-floored stops),
    # so there is no separate bulk step here — the regime flows in via evaluate_stock.
    for symbol in watchlist.effective_stock_watchlist(positions):
        try:
            strategy.evaluate_stock(symbol, account_id, positions, equity,
                                    is_momentum=(symbol in momentum_set),
                                    momentum_generation=generation,
                                    regime=regime, blocked_symbols=blocked)
        except Exception as exc:
            logger.error("Error evaluating stock %s: %s", symbol, exc)

    expiration = mh.next_monthly_expiration()
    for (symbol, opt_type) in config.OPTIONS_WATCHLIST:
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
        logger.info("Shorting    : %s (effective watchlist, death-cross entries)",
                    "ENABLED" if config.ENABLE_SHORTING else "DISABLED")
        if config.ENABLE_PROFIT_TAKING:
            logger.info("Profit take : ENABLED (>= +%.0f%% & RSI >= %.0f -> sell %.0f%%, one-shot, stop kept on remainder)",
                        config.PROFIT_TAKE_PCT * 100, config.PROFIT_TAKE_RSI_MIN,
                        config.PROFIT_TAKE_FRACTION * 100)
        else:
            logger.info("Profit take : DISABLED (enable via ENABLE_PROFIT_TAKING; would sell %.0f%% at +%.0f%% & RSI >= %.0f)",
                        config.PROFIT_TAKE_FRACTION * 100, config.PROFIT_TAKE_PCT * 100,
                        config.PROFIT_TAKE_RSI_MIN)
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
    logger.info("Pos. guard  : a FAILED positions fetch skips the whole cycle "
                "(unknown != flat); ERROR after %d consecutive — stops are "
                "unenforced during an outage",
                _POSITIONS_FAILURE_ESCALATE_AFTER)
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
        logger.info("Sentiment   : fear=%s/10 regime=%s risks=%s%s (model=%s, "
                    "weekdays 08:00 ET, stale>%dh, cap=$%.2f)",
                    _rep.get("fear_score"), _rep.get("regime"),
                    _rep.get("top_risks") or [],
                    " [FALLBACK]" if _rep.get("fallback") else "",
                    config.SENTIMENT_MODEL, config.SENTIMENT_MAX_AGE_HOURS,
                    config.SENTIMENT_MAX_COST_USD)
        logger.info("Combine     : effective regime = MORE FEARFUL of (VIX, sentiment); "
                    "sentiment 'high' sector → blocks new long entries in that sector")
    else:
        logger.info("Sentiment   : DISABLED")

    # Current arming width: the ATR multiple the NEXT entry would arm its stop at,
    # by the effective (VIX ⊕ sentiment) regime. Fail-open — a startup VIX glitch
    # must not abort the banner (or the bot), so this is best-effort only.
    try:
        _vix, _vix_reg = strategy.current_regime()
        _eff_reg = _vix_reg
        if config.ENABLE_SENTIMENT:
            _eff_reg = strategy._more_fearful(
                _vix_reg, sentiment_analyzer.sentiment_regime(_rep))
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
