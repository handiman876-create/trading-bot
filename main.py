#!/usr/bin/env python3
"""
TradeStation Paper-Trading Bot
==============================
Starts automatically at NYSE open (9:30 AM ET), evaluates each symbol on
STOCK_WATCHLIST and OPTIONS_WATCHLIST every POLL_INTERVAL seconds using
EMA-crossover + RSI signals, logs all trades and performance, then shuts
down cleanly at market close (4:00 PM ET).

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
from trade_logger import log_performance

logger = logging.getLogger("bot")

_shutdown = threading.Event()

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
    """One evaluation pass over the active watchlist."""
    positions = tc.get_positions(account_id)
    balance   = tc.get_account_balance(account_id)
    equity    = balance.get("total_equity") if balance else None
    log_performance(account_id, balance, positions)

    if MODE == "futures":
        for root in config.FUTURES_WATCHLIST:
            try:
                strategy.evaluate_future(root, account_id, positions)
            except Exception as exc:
                logger.error("Error evaluating future %s: %s", root, exc)
        return

    for symbol in config.STOCK_WATCHLIST:
        try:
            strategy.evaluate_stock(symbol, account_id, positions, equity)
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
        logger.info("Stocks      : %s", config.STOCK_WATCHLIST)
        logger.info("Options     : %s", config.OPTIONS_WATCHLIST)
        logger.info("Next option exp.: %s", mh.next_monthly_expiration())
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
