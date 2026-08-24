import os
import sys
from dotenv import load_dotenv

load_dotenv()

# ── Run mode ──────────────────────────────────────────────────────────────────
# "equities" (stocks + options) or "futures". Set by main.py's --mode flag (which
# exports BOT_MODE before importing this module) or directly via the environment.
# Drives the singleton lock file and log filenames below so an equities instance
# and a futures instance can run side by side without colliding.
BOT_MODE    = os.environ.get("BOT_MODE", "equities").lower()
_IS_FUTURES = BOT_MODE == "futures"
_PROC_SUFFIX = ".futures" if _IS_FUTURES else ""


def _detect_test_run() -> bool:
    """True when this import is happening inside a test run.

    WHY THIS EXISTS: trade_logger installs a logging.FileHandler on
    config.APP_LOG_FILE at MODULE IMPORT time. Under pytest that import happens
    during collection — before any fixture can run — so conftest's autouse
    redirect was structurally too late and every test's log output landed in the
    live logs/bot.log. That put 180 lines of AAA/BBB fixture chatter into the
    production log, including a fabricated "STOP-LOSS EXIT NVDA ... sell order
    failed" that reads exactly like a live incident, and it poisoned every
    grep-based counter audit (the CROSS SUSTAIN counters most of all).

    Detection has to cover BOTH entry points, because they differ:
      * `pytest ...`          -> the pytest module is imported by the runner.
      * `python3 test_x.py`   -> pytest is NOT in sys.modules (only 2 of 19 test
                                 modules import it), so fall back to argv[0].
    PYTEST_CURRENT_TEST is checked too, but note it is set per-test and is
    absent at collection time, so it can never be the primary signal.

    Deliberately NOT a substring test on the path: a live checkout under e.g.
    /root/la-test-bot/ would silently flip production into test mode and split
    the real log in two.
    """
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return True
    argv0 = os.path.basename(sys.argv[0] or "")
    return argv0.startswith("test_") and argv0.endswith(".py")


_IS_TEST = _detect_test_run()


def is_occ_symbol(symbol: str) -> bool:
    """True for an option contract symbol, e.g. "NVDA 260821C220".

    TradeStation's OCC format is "<ROOT> <YYMMDD><C|P><STRIKE>"; equity and ETF
    tickers never contain a space, so the space IS the whole test.

    WHY THIS LIVES HERE: three call sites depend on telling a contract from a
    stock (the watchlist held-fold-in, the stop bootstrap, and the stop
    reconcile), and they sit in two different modules. Both import config, so
    this is the one place both can reach. Re-implementing the check per site is
    how the three drift apart — the 2026-08-05 exit loop was caused by exactly
    one site (watchlist) not making the distinction at all.
    """
    return " " in (symbol or "").strip()


# Test runs get their own prefix so they can never append to a production log.
# conftest.py additionally redirects these into a tmpdir; this prefix is the
# floor that holds even when a test is run directly, outside pytest.
_LOG_PREFIX = "test_" if _IS_TEST else ("futures_" if _IS_FUTURES else "")

# ── TradeStation OAuth Credentials ────────────────────────────────────────────
TS_CLIENT_ID     = os.environ.get("TS_CLIENT_ID", "")
TS_CLIENT_SECRET = os.environ.get("TS_CLIENT_SECRET", "")
TS_REFRESH_TOKEN = os.environ.get("TS_REFRESH_TOKEN", "")
TS_SANDBOX       = os.environ.get("TS_SANDBOX", "true").lower() == "true"

# ── TradeStation API endpoints ────────────────────────────────────────────────
# The simulator host (sim-api) is a full paper-trading mirror of the live API.
TS_BASE_URL = (
    "https://sim-api.tradestation.com/v3" if TS_SANDBOX
    else "https://api.tradestation.com/v3"
)

# OAuth2 (Auth0-backed) — shared across live and sandbox.
TS_SIGNIN_BASE   = "https://signin.tradestation.com"
TS_AUTHORIZE_URL = f"{TS_SIGNIN_BASE}/authorize"
TS_TOKEN_URL     = f"{TS_SIGNIN_BASE}/oauth/token"
TS_AUDIENCE      = "https://api.tradestation.com"
TS_SCOPE         = "openid profile offline_access MarketData ReadAccount Trade"
# Must be registered as an allowed redirect URL for your API key.
TS_REDIRECT_URI  = os.environ.get("TS_REDIRECT_URI", "http://localhost:3000/")

# ── Tradier Credentials (legacy client) ───────────────────────────────────────
# tradestation_client is the drop-in replacement for tradier_client; these are
# retained so the legacy module still imports. Falls back to the sandbox host.
TRADIER_API_TOKEN = os.environ.get("TRADIER_API_TOKEN", "")
TRADIER_BASE_URL  = os.environ.get(
    "TRADIER_BASE_URL", "https://sandbox.tradier.com/v1"
)

# ── Market Hours (NYSE, Eastern Time) ────────────────────────────────────────
MARKET_OPEN_HOUR   = 9
MARKET_OPEN_MIN    = 30
MARKET_CLOSE_HOUR  = 16
MARKET_CLOSE_MIN   = 0
MARKET_TZ          = "America/New_York"

# ── Entry delay after the session open ───────────────────────────────────────
# Signals are computed on DAILY bars whose last bar is today's live, still-
# forming bar. At the opening bell that bar holds seconds of data, so its EMAs
# are noise: on 2026-07-15 QQQ fired a bullish cross at 9:30:05 with the EMAs
# 0.017% apart and was back below within 44 minutes. Entries wait this many
# minutes for the bar to form; exits and stops stay live from the open (an early
# exit costs little, an entry on noise commits capital).
#
# This is a confirmation window, not a skip: `prev` is pinned to yesterday's
# CLOSED bar, so a cross stays true all day while the state holds. Delaying does
# not miss the signal — it requires the signal to survive the delay.
CROSS_ENTRY_DELAY_MINUTES = 30

# ── Minimum EMA separation (cross hysteresis) ────────────────────────────────
# The delay above is a TIME filter; this is the MAGNITUDE filter for the same
# class of bug. Two EMAs a hair apart are not a trend, they are a rounding
# artefact: on 2026-07-22 CAH sold a 215-share position (-$1,370) because EMA9
# sat $0.01 below EMA21 at a price of $228 — a 0.004% separation, one poll after
# the two were exactly equal. A cross only counts when
#     abs(ema_short - ema_long) / price >= EMA_CROSS_MIN_GAP_PCT
#
# 0.001 (0.1%) is not a delicate choice. Replaying 8 sessions of logs (55,062
# polls, 27 signals) the distribution is sharply bimodal with an empty band on
# both sides: entries jump 0.020% -> 0.114%, exits jump 0.042% -> 0.159%. Any
# value from 0.05% to 0.11% classifies the same 10 noise signals identically.
#
# Applied symmetrically to all four signals (long entry/exit, short entry/cover).
# Exits keep their STATE semantics, so a suppressed exit is DEFERRED, not lost —
# the predicate is re-derived every poll and fires as soon as the gap widens.
# Residual risk: a position drifting bearish that NEVER clears 0.1% is held on
# its trailing stop alone; _cross_gap_blocks makes that visible.
EMA_CROSS_MIN_GAP_PCT = 0.001    # 0.1% of price

# ── Cross persistence (entry signals only) ────────────────────────────────────
# EMA_CROSS_MIN_GAP_PCT filters a cross by MAGNITUDE. It cannot filter one by
# PERSISTENCE, and the ledger says persistence is where the money went: of the 25
# closed round-trips to 2026-07-24, the 8 held under 30 hours lost -$10,953.34
# with ZERO winners, in BOTH directions. AVGO on 07-23 is the archetype — a clean
# 0.10%-clearing cross at 10:00, reversed and stopped out 88 minutes later.
#
# So require an entry cross to still be a cross N minutes after it first appears.
# Backtest over those same 25 trips (minute bars, daily-EMA state reconstructed
# per minute) at 30 minutes: blocks 7 trades worth -$9,525.94, 6 of the 8
# whipsaws, and ZERO of the 2 winners. 45m tested identical (no trade has a
# sustain between 30 and 45), 15m captured only -$5,014.80 — so 30 is the
# shortest setting that gets the full measured benefit.
#
# PROVISIONAL — this was fit in-sample on 25 trades with only 2 winners, so
# "blocks no winners" is evidence from a sample of 2. Re-fit against post-deploy
# trades before treating 30 as settled; _cross_sustain_blocks is the counter.
# Set to 0 to disable the rule entirely.
#
# ENTRIES ONLY. Exits are deliberately NOT gated: an exit-side version of this
# idea was backtested on the same trips and LOST money in both forms tried —
# an age-based gate cost -$3,765.53 and a losing-position gate cost -$6,928.56,
# because delaying an exit on a bad position just books a bigger loss. Do NOT
# "improve" this by extending it to exits without re-running that test.
ENABLE_CROSS_SUSTAIN  = True     # master switch; False = fire on the cross, as before
CROSS_SUSTAIN_MINUTES = 30

# ── Watchlist (fixed core) ────────────────────────────────────────────────────
# The live stock list is assembled every cycle by
# watchlist.effective_stock_watchlist() as:  CORE_WATCHLIST ∪ momentum slot ∪
# currently-held symbols. Edit the two core buckets here; the momentum slot is
# generated twice-monthly into data/momentum_watchlist.json, not hand-edited.
CORE_MEGA = ["SPY", "QQQ", "AAPL", "MSFT", "GOOGL",
             "META", "NVDA", "AMZN", "TSLA", "AMD"]
CORE_GROWTH = ["AVGO", "ARM", "CRWV", "JPM", "PLTR"]
CORE_WATCHLIST = CORE_MEGA + CORE_GROWTH

# Options watchlist: list of (symbol, option_type).
# Neither strike nor expiration is hardcoded — both are computed at runtime:
#   strike     → nearest $5 to the underlying at signal time (strategy._atm_strike)
#   expiration → next monthly 3rd Friday (market_hours.next_monthly_expiration)
OPTIONS_WATCHLIST = [
    ("SPY",  "call"),
    ("QQQ",  "call"),
    ("NVDA", "call"),
    ("AMD",  "call"),
]

# Open-contract store for the options path (generated, gitignored). Keyed by
# "<underlying>_<opt_type>" — one open contract per OPTIONS_WATCHLIST pair, which
# matches OPTIONS_CONTRACTS=1 and the single-pair loop in main.py.
#
#   {
#     "SPY_call": {
#       "occ_symbol":       "SPY 260821C540",   # EXACT symbol we transact on
#       "entry_price":      8.50,               # ask at entry (what we paid)
#       "entry_date":       "2026-08-03",
#       "expiration":       "2026-08-21",
#       "opt_type":         "call",
#       "strike":           540,
#       "contracts":        1,
#       "underlying_entry": 541.20
#     }
#   }
#
# WHY IT EXISTS: exits used to look the position up under an occ_symbol
# RECOMPUTED each cycle from _atm_strike(current price), so a move of more than
# half a strike increment silently orphaned the contract. See the block comment
# above strategy._option_key for the full failure story.
OPTIONS_POSITION_FILE = "data/options_positions.json"   # generated (gitignored)

# ── Bar-history outage reporting ─────────────────────────────────────────────
# A history fetch that returns nothing aborts the poll for that symbol BEFORE
# the trailing-stop check, so a held position goes unevaluated for that cycle.
# That was silent until now: on 2026-08-04 PLTR went 6 consecutive polls
# unchecked (14:46-15:00) during a TradeStation /barcharts outage and no log
# line said so. Consecutive misses on a HELD name escalate WARNING -> ERROR at
# this threshold; 3 polls at POLL_INTERVAL=60 is ~3 minutes of unchecked stop.
# Flat names never log — a skipped poll protects nothing when there is no
# position, and counting those would measure API weather, not exposure.
HISTORY_GAP_ERROR_STREAK = 3

# ── Strategy Parameters ───────────────────────────────────────────────────────
MA_SHORT_PERIOD  = 9     # fast EMA
MA_LONG_PERIOD   = 21    # slow EMA
RSI_PERIOD       = 14
RSI_OVERSOLD     = 30
RSI_OVERBOUGHT   = 70

# ── Profit taking (scale out of a winner) ─────────────────────────────────────
# Sell PROFIT_TAKE_FRACTION of a long once it is up >= PROFIT_TAKE_PCT from entry
# AND RSI is extended (>= PROFIT_TAKE_RSI_MIN). One-shot per position, tracked as
# "profit_taken" in stop_prices.json (a missing flag reads as False — back-compat).
# The trailing stop stays armed on the remaining shares. Like the stop and state
# exits it is DE-RISKING, so it runs ungated by regime and the entry delay.
ENABLE_PROFIT_TAKING = True   # enabled 2026-07-31; LONGS ONLY — see docs/backlog.md
                              # "Short profit taking: formula fix required" before
                              # relaxing the held<=0 guard (strategy.py:828)
PROFIT_TAKE_PCT      = 0.12   # trigger once up this fraction from entry (+12%)
PROFIT_TAKE_FRACTION = 0.50   # sell this fraction of the held shares (half)
PROFIT_TAKE_RSI_MIN  = 60.0   # only when RSI is at least this (extended)

# ── Position Sizing ───────────────────────────────────────────────────────────
EQUITY_PER_TRADE_PCT = 0.05   # fraction of account equity deployed per stock trade
MAX_POSITIONS        = 20     # skip new stock entries once this many positions are
                              # open (0.05 × 20 = 100% fully deployed)
OPTIONS_CONTRACTS    = 1      # contracts per options trade

# ── Options exit targets (premium-based, NOT ATR) ─────────────────────────────
# Equities exit on an ATR trailing stop; options had NOTHING but the EMA-state
# flip, so a contract could bleed to zero while the underlying's EMAs stayed
# bullish. These three rules are the options analogue of the stop, and they are
# checked BEFORE the state exit because all three are de-risking.
#
# Evaluated against the BID (what a sell actually receives), consistent with
# _option_fill_price(quote, "exit") — NOT the mid. Options round-trip spreads run
# ~2.1%, so a mid-based trigger reports a fill the book will not give you.
#
# The entry reference is the STORED entry_price. Adopted positions carry
# entry_price 0.0 (strategy.py: the adoption path cannot know what was paid), and
# 0.0 would make `bid >= entry * 1.50` read 0 >= 0 == True and close instantly —
# so a non-positive entry SKIPS the target/stop rules. Expiry is unaffected: it
# needs no entry price and remains armed on adopted contracts.
ENABLE_OPTION_EXIT_TARGETS = True
OPTION_PROFIT_TARGET_PCT   = 1.50   # close once bid >= entry × this (+50%)
OPTION_STOP_LOSS_PCT       = 0.50   # close once bid <= entry × this (−50%)
OPTION_MIN_DAYS_TO_EXPIRY  = 5      # close with <= this many TRADING SESSIONS left,
                                    # before gamma/theta make the exit unpriceable.
                                    # Sessions, not calendar days (changed 2026-08-12):
                                    # a calendar threshold can go true on a weekend,
                                    # deferring the close to the next session AND onto
                                    # an opening bell. 5 sessions is ~7 calendar days,
                                    # so this value did NOT change but its meaning did.

# ── Stop Loss (bot-managed trailing stop) ─────────────────────────────────────
# Bot-managed (not broker-native) ATR trailing stop, checked every cycle in
# strategy.evaluate_stock and strategy.evaluate_future BEFORE the EMA-cross
# signal. A resting broker GTC floor sits behind it (ENABLE_BROKER_STOP_FLOOR)
# for the overnight gap the bot-managed level cannot cover. See stop_prices.json
# for the persisted per-position state (entry, ATR-at-entry, ratcheting stop).
USE_TRAILING_STOP    = True   # master switch; False = no stop checks at all
STOP_LOSS_ATR_MULT   = 2.5    # default/fallback ATR multiple (= risk_on width)
STOP_LOSS_ATR_PERIOD = 14     # ATR lookback (Wilder), computed once at entry

# PER-PROCESS, not shared. The equities and futures bots are separate processes
# reading the same repo, and reconcile_stops prunes every record whose symbol is
# absent from ITS OWN positions list. On one shared file the equities cycle would
# therefore delete each futures record within a minute of it being written (and
# re-bootstrap it with a reset water-mark, silently loosening the stop). Before
# futures had stops at all, main.py papered over this by returning from the
# futures branch BEFORE reconcile_stops; now that both sides arm, the file itself
# has to be split. The suffix (not a prefix) keeps the equities path byte-for-byte
# unchanged — "data/stop_prices.json" — so the four live records and their resting
# GTC order ids survive the upgrade with no migration step.
STOP_PRICE_FILE      = f"data/stop_prices{_PROC_SUFFIX}.json"   # generated (gitignored)

# Regime-based ATR multiplier — the stop WIDTH a position is armed with depends on
# the market regime AT ENTRY. The chosen multiple is persisted per position
# ("atr_mult" in the stop record) and reused for ALL later trailing, so a
# position's stop width is fixed at entry and does NOT change as the regime moves
# — only NEW entries feel a regime shift. Legacy records with no "atr_mult" fall
# back to STOP_LOSS_ATR_MULT (2.5), so pre-existing stops are unaffected. This is
# SEPARATE from and STACKS WITH the existing defensive >3%-drawdown tightening
# (VIX_DEFENSIVE_ATR_MULT), which still overrides the trail on losing positions.
ATR_MULT_RISK_ON   = 2.5      # = STOP_LOSS_ATR_MULT (unchanged behaviour)
ATR_MULT_CAUTIOUS  = 2.0      # slightly tighter
ATR_MULT_DEFENSIVE = 1.5      # meaningfully tighter
ATR_MULT_CRISIS    = 1.0      # very tight
ATR_MULT_BY_REGIME = {
    "risk_on":   ATR_MULT_RISK_ON,
    "cautious":  ATR_MULT_CAUTIOUS,
    "defensive": ATR_MULT_DEFENSIVE,
    "crisis":    ATR_MULT_CRISIS,
}

# Volatility band — a SECOND axis on the stop width, stacked on the regime axis
# above. The regime says how afraid the market is; the band says how wide this
# particular name's daily range is, as ATR/price at entry.
#
# Why: a fixed ATR multiple is already volatility-scaled in dollars, but the
# resulting stop as a PERCENT of price still runs away on high-ATR names. CRWD at
# ATR/price = 7% arms a 2.0x cautious stop 14% from entry — on a SHORT that is a
# large loss if the name reverses. The high band pulls that back to 1.25x (8.75%).
# The low band is the mirror: a 1.5%-ATR name at 2.0x stops out 3% away, inside
# normal noise, so it gets more room.
#
# Bands are measured ONCE AT ENTRY from the same ATR that is persisted as
# "atr_at_entry", and the resulting multiple is persisted as "atr_mult" — so, like
# the regime axis, a position's width is FIXED at entry and never re-banded as its
# ATR/price drifts. A name can be "high vol" on the day it is opened and "normal"
# a week later; the stop keeps its armed width either way.
#
# Boundaries are EXCLUSIVE at the top: ratio <= 0.02 is low, 0.02 < r <= 0.05 is
# normal, r > 0.05 is high. Note DDOG sat at 4.98% on 2026-07-22 — a hair under
# the high band — so the cliff at 5% is real and deliberate, not a rounding
# artifact. Two near-identical names can land in different bands.
ATR_PCT_LOW_THRESHOLD  = 0.02   # ATR/price <= this  -> low-vol band  (wider stop)
ATR_PCT_HIGH_THRESHOLD = 0.05   # ATR/price >  this  -> high-vol band (tighter stop)

# regime -> (low_band, normal_band, high_band). The normal column is exactly
# ATR_MULT_BY_REGIME above, so an unbanded lookup and a normal-band lookup agree.
# The haircut is NOT a uniform ratio (0.60 / 0.625 / 0.667 / 0.75 high-vs-normal):
# the tighter the regime, the smaller the extra squeeze, so the crisis row does
# not collapse to nothing. Encoded as an explicit table rather than a scale factor
# so each of the 12 cells is a deliberate choice you can read off directly.
ATR_MULT_BY_REGIME_AND_BAND = {
    #             low    normal  high
    "risk_on":   (3.0,   2.5,    1.5),
    "cautious":  (2.5,   2.0,    1.25),
    "defensive": (2.0,   1.5,    1.0),
    "crisis":    (1.5,   1.0,    0.75),
}
# CAUTION (flagged at design time, accepted): crisis/high = 0.75x. On a 7%-ATR
# name that is a 5.25% stop — LESS than one average daily range, so a whipsaw exit
# is close to arithmetically certain. Kept deliberately; revisit if the
# _high_vol_stops / _low_vol_stops counters show crisis-band arms actually firing.
# stop_prices.json schema, per symbol:
#   entry_price, atr_at_entry, stop_price, opened, bootstrapped, direction
#   + "atr_mult" (float; the ATR multiple this stop was armed with, by entry
#     regime — missing reads as STOP_LOSS_ATR_MULT (2.5), so pre-atr_mult records
#     are back-compat)
#   + "profit_taken" (bool; set once a partial profit-take has fired — missing
#     reads as False, so records written before profit-taking existed are back-compat)
#   + "high_water" (longs: max price seen; stop = high_water - MULT*atr, rises)
#   OR "low_water"  (shorts: min price seen; stop = low_water + MULT*atr, falls)
# "direction" is "long" | "short"; records written before shorts existed have no
# such key and are read as "long" (rec.get("direction", "long")) — fully back-compat.

# ── Short selling (effective-watchlist names, fresh death-cross entries) ───────
# When enabled, a fresh EMA death cross on ANY effective-watchlist name (core ∪
# momentum ∪ held) with no position opens a SHORT (SELLSHORT), sized like a long
# (EQUITY_PER_TRADE_PCT) and counting toward MAX_POSITIONS. Momentum picks are
# shortable too (expanded from core-only 2026-07-18). Shorts are covered
# (BUYTOCOVER) on a bullish cross, and carry a trailing stop that sits ABOVE
# entry and ratchets DOWN with a low-water mark, using the regime ATR multiple.
# HISTORY — read before touching this pair (see also SHORT_MIN_REGIME below).
#   2026-08-03 early: DISABLED. Closed short book 0-for-7 / -$12,745.92 in the
#     reconciled ledger; with the two 08-03 covers that had not reconciled
#     (META +$1,505.60, NVDA -$2,651.88) that is 1-for-9 / about -$13,892.
#     The only short opened after the regime filter shipped (07-27) was CRWD
#     (07-28, -$3,642.08), armed by the sentiment override at fear=4.
#   2026-08-03 later: RE-ENABLED, deliberately, together with
#     SHORT_MIN_REGIME="risk_on" and ENABLE_SENTIMENT_OVERRIDE=False. This is an
#     intentional experiment on a SANDBOX account, not a claim that an edge was
#     found. It restores the pre-07-27 configuration — the one under which all 7
#     shorts in the retained window were armed in risk_on for 0-for-5 /
#     -$9,054.34 — so the null hypothesis is that shorts keep losing.
#
# WHAT TO WATCH: short trips per week and their win rate. If the next batch of
# closed shorts repeats the risk_on pattern, the filter was doing its job and the
# right response is SHORT_MIN_REGIME="cautious" again (not a wider stop or a
# different oracle). REGIME BLOCK will now stay at 0 by construction, so it is no
# longer evidence of anything.
#
# ENTRY-only, like every other gate here: read at strategy.py's short ENTRY
# branch and nowhere else, so open shorts always trail, stop, breakeven-lock and
# cover regardless of this switch.
ENABLE_SHORTING = True   # master switch; False = long-only (prior behaviour)

# Master switch for the regime short filter below. False restores pre-filter
# behaviour exactly (shorts gated only by ENABLE_SHORTING and block_new_entries),
# so this can be flipped off without editing SHORT_MIN_REGIME.
ENABLE_REGIME_SHORT_FILTER = True

# Minimum fear level required to open a NEW short. Shorting into a bullish tape
# is what the short book has actually been doing: every short entry in the
# retained window (7, 2026-07-17..07-27) was armed in risk_on, and the closed
# short trips are 0-for-5 for -$9,054.34. Existing shorts are NOT touched — this
# gates entries only, so a position already open still trails and stops normally.
#
# Ranked against _REGIME_RANK: a regime ranking BELOW this blocks new shorts.
# "unknown" ranks with risk_on, so a VIX outage blocks shorts rather than
# opening them blind — the opposite of the fail-OPEN used for longs, and
# deliberate: failing open on a short is how you get short into a rally.
#
# READ THIS BEFORE CHANGING IT. defensive and crisis already block ALL new
# entries via _apply_regime_rules, so the only regime "cautious" actually opens
# shorting in is cautious itself — i.e. only while 20 <= VIX < 25. Setting this
# to "defensive" or "crisis" does not widen that window, it disables new shorts
# entirely.
#
# "risk_on" is the OTHER end: because risk_on and unknown both rank 0, the gate
# `rank(regime) < rank(floor)` is never true, so this value makes the filter a
# NO-OP — behaviourally identical to ENABLE_REGIME_SHORT_FILTER=False. It is set
# that way deliberately as of 2026-08-03 to allow shorts in risk_on again; keep
# ENABLE_REGIME_SHORT_FILTER=True so the machinery stays wired and tightening the
# floor later is a one-word change.
#
# Note the cost of a no-op filter: "unknown" also ranks 0, so a VIX outage now
# permits shorts rather than blocking them. The fail-CLOSED behaviour described
# above only exists while the floor is above risk_on.
SHORT_MIN_REGIME = "risk_on"

# ── Momentum alignment entry (momentum slot only) ─────────────────────────────
# Momentum leaders are already trending when the twice-monthly screen adds them,
# so they never produce a *fresh* EMA cross for the bot to enter on. Give the
# momentum bucket a one-shot "enter on alignment" signal instead; core names keep
# the patient fresh-cross entry. One entry per symbol per rotation, latched in
# MOMENTUM_ENTRY_FILE so a stop-out can't trigger an immediate re-buy.
# DISABLED 2026-07-24 — no proven edge. Full ledger recompute: momentum_alignment
# closed 11 round-trips for 1 win (HCA +$2,071.28) and -$23,735.16 total, 65% of
# all realized losses. A 1-for-11 bucket would not clear the discovery pipeline's
# own ci_lower > 1.0 promotion gate, so it should not be trading live capital.
# CAVEAT for whoever re-enables this: all 11 entries were 07-14..07-17 and predate
# the 07-18 stop rework (af859e5 regime ATR tiers, fa01c36 hysteresis, 19a1d1b
# breakeven lock). This bucket has NEVER traded with the current machinery, so the
# losses are not cleanly attributable to the entry rule. Re-evaluate by backtest —
# not by flipping this back to True and watching live.
USE_MOMENTUM_ALIGNMENT = False   # master switch; False = fresh-cross only, all names

# ── Feature retirement metadata (weekly report) ───────────────────────────────
# The ON/OFF state lives in the flags themselves — these tables only record WHEN
# and WHY, which nothing else in the codebase knows. Keyed by the
# performance_analyzer feature-bucket name.
#
# WHY THIS IS NOT A HARDCODED P&L FIGURE: the report renders the loss total live
# from the ledger. A number pasted here would drift the moment a trip is
# reclassified or a correction trip is re-tagged, and a stale figure in a report
# that looks authoritative is worse than no figure at all.
FEATURE_FLAGS = {
    "momentum_alignment": "USE_MOMENTUM_ALIGNMENT",
}
FEATURE_DISABLED_NOTES = {
    "momentum_alignment": {
        "since":  "2026-07-24",
        "commit": "40a34a3",
        "reason": "no proven edge — would not clear the discovery pipeline's "
                  "own ci_lower > 1.0 promotion gate",
    },
}
# Momentum alignment only when RSI shows healthy momentum (not oversold, not
# overbought): buy trending names on a healthy pullback, not on a breakdown
# (RSI < MIN, e.g. HCA @ 35.1) or when already extended (RSI > MAX).
MOMENTUM_ALIGN_RSI_MIN = 45      # skip alignment entry when RSI is below this (weakness/breakdown)
MOMENTUM_ALIGN_RSI_MAX = 65      # skip alignment entry when RSI is above this (overbought); was 60
MOMENTUM_ENTRY_FILE    = "data/momentum_entries.json"   # generated (gitignored)

# ── Momentum Rotation (dynamic watchlist slot) ────────────────────────────────
# Twice a month (1st & 15th, pre-market) momentum_screen.py screens the S&P 500
# for momentum leaders and writes MOMENTUM_WATCHLIST_FILE; the bot folds up to
# MOMENTUM_SLOT_SIZE of them into the live list. The screen criteria below are
# shared with momentum_screen.py — one source of truth for both.
MOMENTUM_SLOT_SIZE      = 5
MOMENTUM_WATCHLIST_FILE = "data/momentum_watchlist.json"   # generated (gitignored)
MOMENTUM_UNIVERSE_FILE  = "data/sp500.json"                # vendored S&P 500 list
MOMENTUM_MAX_AGE_DAYS   = 21     # warn if the generated list is older than this

# Screen criteria (20-day momentum leaders)
MOM_LOOKBACK   = 20      # trading-day lookback for return & average volume
MOM_RETURN_MIN = 0.05    # 20-day price return must exceed +5%
MOM_RSI_MIN    = 50      # RSI(14) lower bound (uptrend, not yet overbought)
MOM_RSI_MAX    = 70      # RSI(14) upper bound

# ── Sector exclusions (momentum screen) ───────────────────────────────────────
# Names matched against BOTH the GICS Sector and GICS Sub-Industry fields stored
# per symbol in data/sp500.json (a candidate is skipped if EITHER field matches).
# A sector name and a sub-industry name never collide, so a flat list is safe.
# "Energy" (sector) == the two GICS oil/gas industries "Oil, Gas & Consumable
# Fuels" + "Energy Equipment & Services" — every S&P Energy name falls under one
# of them, and the source CSV carries no Industry column, so the sector is the
# exact, data-backed equivalent. Airlines are a sub-industry of Industrials, so
# they're excluded by sub-industry name rather than by whole sector.
EXCLUDED_SECTORS = [
    "Energy",             # Oil, Gas & Consumable Fuels + Energy Equipment & Services
    "Utilities",          # entire sector
    "Real Estate",        # entire sector (REITs)
    "Passenger Airlines", # airlines — GICS sub-industry of Industrials
]

# ── A/B screen experiment (observation only — NOT fed to the live bot) ────────
# screen_ab_tracker.py runs the live screen (Screen A) alongside an experimental
# profitability-filtered screen (Screen B) each rotation, records both to
# SCREEN_AB_TRACKING_FILE, and measures each rotation's 2-week forward returns on
# the NEXT rotation. Screen A here is the SAME 20-day ranking the live bot uses
# (MOM_LOOKBACK) so the profitability filter is the only variable between A and B.
# The tracker NEVER writes MOMENTUM_WATCHLIST_FILE — the live path is untouched.
SCREEN_AB_TRACKING_FILE   = "data/screen_ab_tracking.json"   # generated (gitignored)
SCREEN_AB_MIN_ROTATIONS   = 4        # don't declare a winner before this many rotations
# Screen B: from the top SCREEN_B_TOP_N momentum names, keep those with at least
# SCREEN_B_MIN_PROFITABLE_Q of the last SCREEN_B_QUARTERS_LOOKBACK quarters showing
# positive net income; take the first MOMENTUM_SLOT_SIZE that survive.
SCREEN_B_TOP_N            = 30
SCREEN_B_QUARTERS_LOOKBACK = 5
SCREEN_B_MIN_PROFITABLE_Q = 4
# Realized-volatility window (annualized, %) recorded alongside avg_iv as a
# supplementary premium proxy — populated even when the paid IV feed is not.
SCREEN_AB_RV_WINDOW       = 20
# Fundamentals cache: quarterly financials change ~once a quarter, so a long TTL
# keeps Screen B's profitability lookups off the shared 5-calls/min Polygon key.
FUNDAMENTALS_CACHE_FILE   = "data/fundamentals_cache.json"   # generated (gitignored)
FUNDAMENTALS_CACHE_TTL_DAYS = 30

# ── VIX fear gauge (market-regime filter) ─────────────────────────────────────
# A market-wide risk overlay driven by the CBOE Volatility Index, applied to BOTH
# equities and futures. One quote per cycle (cached VIX_CACHE_SECONDS) maps to a
# regime that gates entries and, at the extreme, tightens stops and de-risks the
# momentum slot. Master switch OFF ⇒ always "risk_on" (prior behaviour).
#
# SYMBOL: TradeStation quotes the cash index as "$VIX.X" (the "$XXX.X" index
# convention, same as "$SPX.X"). Bare "VIX"/"$VIX" return INVALID SYMBOL, and the
# index carries NO bid/ask book — only Last/Close — so get_vix_level reads Last
# with a Close fallback. Verified against sim-api 2026-07-17.
ENABLE_VIX_FILTER = True
VIX_SYMBOL        = "$VIX.X"
VIX_CACHE_SECONDS = 300     # reuse one quote for 5 min; don't refetch every 60s poll
# Each constant is the CEILING of its namesake regime — the VIX level at which
# that regime ends and the next begins — so the original 20/25/30 rule boundaries
# hold exactly:
#     risk_on   VIX < 20                  cautious   20 <= VIX < 25
#     defensive 25 <= VIX < 30            crisis     VIX >= 30
# VIX_CRISIS (35) marks an EXTREME sub-tier WITHIN crisis: same protective actions,
# tagged EXTREME in the log (so the constant is live, not decorative).
VIX_NORMAL    = 20   # top of risk_on   → cautious begins here
VIX_CAUTIOUS  = 25   # top of cautious  → defensive begins here
VIX_DEFENSIVE = 30   # top of defensive → crisis begins here
VIX_CRISIS    = 35   # within crisis    → EXTREME tag at/above here
# Defensive stop tighten: on a position already down > DRAWDOWN from entry, trail
# with this tighter ATR multiple instead of STOP_LOSS_ATR_MULT (2.5).
VIX_DEFENSIVE_ATR_MULT = 1.5
VIX_DEFENSIVE_DRAWDOWN = 0.03
# Crisis is DESTRUCTIVE (market-sells the held momentum slot, moves every stop to
# breakeven). Shadow ⇒ LOG what it would do and place nothing; flip to False to arm.
VIX_CRISIS_SHADOW = True

# Breakeven lock — once a position's best excursion (high/low-water) reaches
# +BREAKEVEN_LOCK_ATR ATR of profit from entry, floor its stop at entry so it can
# never give back principal. This is the SAME floor operation the crisis regime
# applies (strategy._check_and_trail_stop), but triggered by realized profit in
# ANY regime, not by VIX. It is gated on the position being in profit RIGHT NOW
# (price on the profit side of entry) so the floor can never be armed THROUGH the
# market and force an immediate exit — which is also what makes it safe to apply
# retroactively to positions that predate this rule. Strictly protective: it only
# ever raises a stop toward entry, never loosens one, and never past the market.
ENABLE_BREAKEVEN_LOCK = True
BREAKEVEN_LOCK_ATR    = 1.0    # favorable excursion (in entry-ATRs) required to arm

# ── Profit floor ladder (percentage stop floors that step up with the gain) ───
# A ladder of static, ENTRY-anchored floors. Each rung reads: once the position
# is up `trigger` from entry, its stop may never again fall below `lock` of
# profit. COMPLEMENTS the ATR trail and the breakeven lock rather than replacing
# either — the effective stop is the most protective of the three:
#     long  stop = max(atr_trail, breakeven_floor, profit_floor)
#     short stop = min(atr_trail, breakeven_floor, profit_floor)
# so a rung only ever BINDS when the ATR trail is wider than it. That is the
# case this exists for: a high-ATR name whose trail sits so far back that a large
# gain can round-trip to nothing. On a tight trail the ladder is inert by
# construction (all five open positions on 2026-08-13 were inert — the trail was
# already the higher floor on every one).
#
# Applies to longs AND shorts, but from SEPARATE ladders — see the short ladder
# below for why mirroring the long one is wrong. A short's rungs still mirror
# below entry geometrically; it is the trigger/lock VALUES that differ.
#
# EVERY rung MUST have lock < trigger. That gap is exactly what keeps the floor
# unreachable through the market: when a rung arms, price is at `trigger` and the
# rung sits at `lock`, strictly behind it. A rung with lock >= trigger would arm
# a stop at or beyond the current price and force an instant exit — the same
# failure the breakeven lock's underwater guard exists to prevent. Rungs are
# validated at import, so a bad edit fails the bot at boot rather than silently
# stopping out every winner.
#
# BAD — DO NOT do this:
#     (0.15, 0.15)   # lock == trigger: floor lands ON the market, instant exit
#     (0.20, 0.25)   # lock  > trigger: floor lands THROUGH the market, worse
ENABLE_PROFIT_FLOOR = True
# The trigger-to-lock gap narrows as the ladder climbs: 5pp on the early rungs
# (room to breathe through ordinary noise while a trend is still developing),
# 2pp from +30%, 1pp from +75%. A gain that large is the rare outcome the whole
# book is paid by, so the priority flips from letting it run to not giving it
# back. The narrow rungs sit far enough out that on a typical name the ATR trail
# is the binding floor long before they arm.
PROFIT_FLOOR_STEPS_LONG = [
    (0.15, 0.10),   # +15% gain  → lock +10%
    (0.20, 0.15),   # +20% gain  → lock +15%
    (0.25, 0.20),   # +25% gain  → lock +20%
    (0.30, 0.28),   # +30% gain  → lock +28%
    (0.40, 0.38),   # +40% gain  → lock +38%
    (0.50, 0.49),   # +50% gain  → lock +49%
    (0.75, 0.74),   # +75% gain  → lock +74%
    (1.00, 0.99),   # +100% gain → lock +99%
]

# Shorts get their OWN ladder rather than the long one mirrored, for two reasons.
#
# 1. A short's gain is CAPPED at 100% — that is the stock reaching zero. The long
#    ladder's top rungs (+75%, +100%) are therefore unreachable in practice, so a
#    mirrored ladder would spend its tightest rungs on outcomes that never occur
#    and leave the reachable range covered only by the loose early rungs.
# 2. Equities drift up. A short is positioned against that drift, so a given
#    excursion is likelier to be given back than the same excursion on a long.
#    Locking earlier is the response: the first rung arms at +8% instead of +15%.
#
# Consequence to be aware of: at +8% with a 3pp gap, the floor sits about 1 ATR
# behind a typical short (AAPL's entry ATR is 3.0% of entry), so it will bind over
# the ATR trail almost immediately and exit on roughly one ATR of retrace. That is
# the intended trade — shorts give back gains faster — but it is a much more
# aggressive posture than the long side, and it is why the counter matters.
#
# BAD — DO NOT do this:
#     mirroring PROFIT_FLOOR_STEPS_LONG onto shorts. The +75%/+100% rungs are
#     dead by construction (see 1), so the short side would silently run on the
#     5pp early rungs alone — looser protection than the long side, not equal.
PROFIT_FLOOR_STEPS_SHORT = [
    # MICRO-RUNGS (added 2026-08-21). These are far tighter than anything else in
    # either ladder and the gap is what makes them so, not the trigger. Measured
    # against the three open shorts, whose entry ATR runs 3.0-4.6% of entry:
    #
    #   +2% → lock +1%  = 1pp gap ≈ 0.33 ATR of room (AAPL: ATR 3.05% of entry)
    #   +5% → lock +3%  = 2pp gap ≈ 0.66 ATR
    #
    # For comparison the previous first rung (+8% → +5%) opens 3pp ≈ 1 ATR, which
    # the note below already calls "much more aggressive than the long side". A
    # third of an ATR is inside ordinary intraday noise, so once the +2% rung arms
    # the position exits on the next small retrace — and the caller's ratchet makes
    # that permanent, so a single 2% dip locks the floor for the life of the trade.
    # Deliberate: catches the give-back that the 8% rung sits too far out to see.
    # Watch the _profit_floors counter against realized short P&L to judge it.
    (0.02, 0.01),   # +2% gain  → lock +1%
    (0.05, 0.03),   # +5% gain  → lock +3%
    (0.08, 0.05),   # +8% gain  → lock +5%
    (0.12, 0.09),   # +12% gain → lock +9%
    (0.15, 0.13),   # +15% gain → lock +13%
    (0.20, 0.18),   # +20% gain → lock +18%
    (0.25, 0.24),   # +25% gain → lock +24%
    (0.30, 0.29),   # +30% gain → lock +29%
    (0.40, 0.39),   # +40% gain → lock +39%
    (0.50, 0.49),   # +50% gain → lock +49%
]


def _validate_profit_floor_steps(steps):
    """Reject rungs that would arm a stop at or through the market, and return
    the ladder sorted highest-trigger-first.

    Pre-sorting here rather than in the hot path matters: the trail runs every
    poll for every held name (~55k times over 8 sessions in the logs), and the
    ladder is a module-level constant that cannot change between polls.
    """
    bad = [(t, lk) for t, lk in steps if lk >= t]
    if bad:
        raise ValueError(
            "profit floor steps rungs must have lock < trigger (a rung with "
            "lock >= trigger arms the stop at or through the market and forces "
            "an instant exit); offending rungs: %r" % (bad,))
    return sorted(steps, reverse=True)


# Descending by trigger, so the first rung a gain clears is the highest one.
# Same validator both sides — the lock < trigger invariant is about geometry
# against the market, which does not care which way the position faces.
PROFIT_FLOOR_STEPS_LONG_DESC = _validate_profit_floor_steps(PROFIT_FLOOR_STEPS_LONG)
PROFIT_FLOOR_STEPS_SHORT_DESC = _validate_profit_floor_steps(PROFIT_FLOOR_STEPS_SHORT)

# Should an armed rung also RAISE the resting broker GTC floor to match?
#
# ON since 2026-08-13. This is what turns the ladder from a bot-managed level
# into a real resting order: the bot's stop only exists while this process is
# alive and the market is open, so before this, a locked-in gain had NO overnight
# gap protection at all — the GTC still sat at its entry-time disaster level.
#
# The cost, accepted deliberately: there is no modify/replace in the broker
# client, so raising means cancel-then-place, and there is a window with NOTHING
# resting. If the placement leg fails the position is unfloored until the next
# restart, because reconcile_broker_floors is one-shot per process (startup
# only). That failure is loud — BROKER FLOOR RAISE ... re-place FAILED, counter
# _floor_raise_failures — because nothing else will catch it.
#
# The new floor is set a buffer BELOW the rung (the same absolute gap
# BROKER_STOP_FLOOR_BUFFER opens at entry), never AT it — a GTC resting exactly
# on the bot's stop races it, and a broker stop that wins that race turns a
# managed exit into a market order at an untrailed level. That invariant is the
# whole reason _floor_price refuses to derive from the trailed stop; raising to
# the rung minus the buffer keeps it.
# Close profitable shorts before the weekend (added 2026-08-21).
#
# A short carries unbounded weekend gap risk in the direction it is exposed to,
# and equities drift up — the same asymmetry that gives shorts their own tighter
# profit-floor ladder above. Friday close is the one moment where flattening a
# winner costs only the remaining upside and removes two days of unhedgeable gap.
#
# Applies to SHORTS ONLY and only when the position is in profit by at least
# FRIDAY_SHORT_CLOSE_MIN_GAIN. A losing short is deliberately left alone: closing
# it would realize the loss to avoid a gap that is as likely to help as hurt.
#
# BAD — DO NOT do this:
#     extending this to longs. A long's weekend gap risk is bounded at -100% and
#     sits WITH the market's drift, so flattening every profitable long on a
#     Friday would just pay spread and commission to re-enter on Monday.
# Late in the session, not at the open. Without a time gate the rule fires on the
# first Friday poll at 09:30 and gives up a whole session of a working short to
# avoid a gap that is still 6.5 hours away — and it would do so on a stub daily
# bar, the same noise the entry gate exists to reject. 15:45 leaves 15 minutes to
# fill before the close while making the "weekend gap" framing actually true.
ENABLE_FRIDAY_SHORT_CLOSE = True
FRIDAY_SHORT_CLOSE_MIN_GAIN = 0.005   # +0.5% — must clear round-trip costs
FRIDAY_SHORT_CLOSE_AFTER_HOUR = 15    # ET
FRIDAY_SHORT_CLOSE_AFTER_MIN = 45     # ET  -> fires from 15:45 ET onward

ENABLE_PROFIT_FLOOR_BROKER_RAISE = True

# ── Broker-native stop floor (disaster backstop) ──────────────────────────────
# The bot's ATR stop lives in this process: it only exists while the bot is
# running and the market is open. It cannot protect against an overnight gap or
# against the bot being down, because nothing is resting at the broker.
#
# This places ONE GTC stop order at entry and never moves it. It sits BEYOND the
# initial ATR stop by BROKER_STOP_FLOOR_BUFFER, so the bot's stop — which starts
# at the ATR level and only ever ratchets AWAY from the floor — always triggers
# first in normal trading. The floor is reached only if the bot is dead or price
# gaps clean through everything.
#
# The buffer must be > 1.0. At 1.0 the floor sits exactly ON the initial ATR stop
# and the two race on day one, which is the one thing this design must not do:
# a broker floor that can fire before the bot's stop turns every exit into a
# market order at an untrailed level. DO NOT set this to 1.0 or below.
#
# Honest limit: a stop-market becomes a MARKET order when triggered, so a gap
# fills at the gapped price, not at the floor. This bounds whether you are still
# in the trade, not what the gap costs.
ENABLE_BROKER_STOP_FLOOR = True    # enabled 2026-08-10; order shape validated
                                   # against orderconfirm (both SELL and
                                   # BUYTOCOVER stop/GTC) before flipping
BROKER_STOP_FLOOR_BUFFER = 1.2     # multiple of the initial ATR stop distance

# ── Polygon.io (momentum-screen data source; free tier) ───────────────────────
POLYGON_API_KEY  = os.environ.get("POLYGON_API_KEY", "")
POLYGON_BASE_URL = "https://api.polygon.io"
POLYGON_MAX_CALLS_PER_MIN = 5    # free-tier rate limit; the screen self-throttles

# ── Claude sentiment analysis (Feature 2 of the VIX + sentiment overlay) ──────
# sentiment_analyzer.py (run weekdays 08:00 ET by systemd) scores market fear from
# Polygon SPY headlines via Claude and writes SENTIMENT_REPORT_FILE. The bot reads
# it each cycle for per-sector entry gating, the banner and the historical record.
# OFF ⇒ the bot ignores sentiment entirely (VIX-only, no sector gate, no banner).
ENABLE_SENTIMENT        = True

# Does the sentiment regime participate in the effective regime? When True the
# effective regime is the MORE FEARFUL of {VIX regime, sentiment regime}; when
# False the regime is the VIX regime alone and sentiment is INFORMATIONAL only.
#
# Set False 2026-08-03. Sentiment's only demonstrated effect on entries was to
# override risk_on -> cautious on 07-28/29/30 (fear 4/4/6) while VIX read
# risk_on, and the one entry that produced was CRWD, -$3,642.08. Three sessions
# is not enough to call it wrong, but it is enough to stop it steering position
# entry while the VIX ladder is the thing being evaluated.
#
# This is the ONLY switch that gates the override. Everything else about the
# overlay is unaffected: the daily run, sector blocking via sectors_blocked(),
# the banner and the report all behave exactly as before. Turning it back on is
# a one-word change with no other edits.
#
# Re-enabled 2026-08-20, but no longer binary — see SENTIMENT_OVERRIDE_MIN_FEAR.
ENABLE_SENTIMENT_OVERRIDE = True

# Minimum fear score for sentiment to PARTICIPATE in the effective regime
# (fear_score is 1-10, higher = more fearful). Below it, sentiment is
# informational and the regime is the VIX regime alone; at or above it, the
# effective regime is the more fearful of {VIX, sentiment} exactly as before.
#
# The scale, from sentiment_analyzer._regime_from_score — note the threshold is
# on the SCORE, not the regime, because 4/5/6 all map to the same "cautious":
#     1-3 risk_on · 4-6 cautious · 7-8 defensive · 9-10 crisis
#
# 6 is chosen to sit at the TOP of the cautious band, so a merely-uneasy read
# does not steer entries but a decisive one still does. That is the CRWD case:
# the −$3,642.08 short was entered 2026-07-28 10:31:14 EDT on fear=4, and only
# the sentiment override (VIX read risk_on) put the bot in cautious, which was
# then the regime SHORT_MIN_REGIME required. At 6 that day is VIX-only and the
# entry never arms; 07-30's fear=6 still overrides.
#
# UNVALIDATED — 6 is a boundary picked from three sessions, which is fitting,
# not discovery (the same knife-edge objection this repo has already documented
# twice, at 5.04% for SHORT_MAX_ATR_PCT and 4.98% for DDOG). The threshold is
# instrumented: `_sentiment_threshold_blocks` counts cycles where sentiment WAS
# more fearful than VIX but scored below this line, i.e. the overrides this
# constant is suppressing. If that counter stays 0, the threshold is not doing
# anything and the binary switch was fine; if it is large, check whether the
# suppressed cycles were the ones worth acting on before trusting the number 6.
SENTIMENT_OVERRIDE_MIN_FEAR = 6
SENTIMENT_REPORT_FILE   = "data/sentiment_report.json"   # generated (gitignored)
SENTIMENT_MODEL         = "claude-sonnet-4-6"
SENTIMENT_MAX_TOKENS    = 500
SENTIMENT_NEWS_TICKERS  = ["SPY", "QQQ", "DIA"]  # index breadth; one Polygon call each
# Free-tier Polygon barely tags index ETFs (SPY/QQQ/DIA ≈ 1 article/48h combined), so
# also pull GENERAL market news (one no-ticker call ≈ 20 articles) and merge — that's
# what actually fills the headline count. All sources deduped by URL, capped below.
SENTIMENT_NEWS_INCLUDE_GENERAL = True
SENTIMENT_NEWS_LIMIT    = 20       # per-source fetch cap AND final cap (20 most recent)
SENTIMENT_NEWS_HOURS    = 48       # headline look-back window (catches weekend news)
# News-quality filter: PR wires and law-firm "investor alert" solicitations flood the
# general feed every day regardless of market conditions and bias sentiment toward
# fear (fraud/lawsuit language). Drop by publisher (substring, case-insensitive) or by
# title matching any keyword (compiled as case-insensitive regex — note "encourages.*
# investors").
SENTIMENT_SPAM_PUBLISHERS = ["globenewswire", "prnewswire", "businesswire",
                             "globe newswire"]
SENTIMENT_SPAM_KEYWORDS   = ["investor alert", "class action", "deadline",
                             "encourages.*investors", "inducement grants", "rosen",
                             "kirby mcinerney", "shareholder alert", "investigation",
                             "lawsuit", "securities fraud"]
# If filtering drops the count below this, supplement with mega-cap tickers (well
# covered, far less PR-wire spam than the general feed).
SENTIMENT_MIN_HEADLINES      = 10
SENTIMENT_SUPPLEMENT_TICKERS = ["AAPL", "MSFT"]
# Staleness: a report older than this is treated as absent → NEUTRAL. 48h keeps a
# weekday report valid across one missed run (resilience). The bot doesn't trade
# weekends and Monday's 08:00 timer writes a fresh report before the open, so this
# never drives a normal-week decision; on the edge where Monday's run is missed,
# Friday's report is ~72h old (> 48h) → NEUTRAL, so stale weekend sentiment can never
# drive Monday. The live VIX regime still applies throughout.
SENTIMENT_MAX_AGE_HOURS = 48
# Cost guardrail — Sonnet 4.6 is $3/$15 per 1M tok, so a run is ~$0.01. Alert (ERROR
# log) if a run somehow exceeds the cap; runaway-cost backstop.
SENTIMENT_PRICE_IN      = 3.0      # $/1M input tokens
SENTIMENT_PRICE_OUT     = 15.0     # $/1M output tokens
SENTIMENT_MAX_COST_USD  = 0.10

# ── Poll interval while market is open (seconds) ─────────────────────────────
POLL_INTERVAL = 60

# ── Futures (mode="futures") ──────────────────────────────────────────────────
# Roots only; the dated front-month contract is resolved at runtime with a
# 5-TRADING-SESSION-before-expiry quarterly roll, counted with
# market_hours.shift_trading_days (futures_market_hours.front_month_contract).
# Sessions, not calendar days, since 2026-08-12 — see FUTURES_ROLL_DAYS below.
# YM is excluded for now: the sandbox account is NOT ENTITLED to Dow data.
FUTURES_WATCHLIST = ["ES", "NQ", "RTY"]
FUTURES_CONTRACTS = 1      # contracts per futures trade (fixed size for MVP)
FUTURES_ROLL_DAYS = 5      # roll to the next quarterly this many TRADING SESSIONS
                           # before expiry (was calendar days until 2026-08-12; the
                           # value is unchanged, the unit is not — 5 sessions is ~7
                           # calendar days, so the roll now happens ~2 days earlier)
# Contract specs (multiplier / tick / $-per-tick) — for logging now, margin-based
# sizing later. Read live initial margin from tradestation_client.confirm_order().
FUTURES_SPECS = {
    "ES":  {"multiplier": 50, "tick": 0.25, "tick_value": 12.50},
    "NQ":  {"multiplier": 20, "tick": 0.25, "tick_value": 5.00},
    "YM":  {"multiplier": 5,  "tick": 1.0,  "tick_value": 5.00},
    "RTY": {"multiplier": 50, "tick": 0.10, "tick_value": 5.00},
}

# ── Process files (per-mode singleton lock + pidfile) ─────────────────────────
LOCK_FILE = f"bot{_PROC_SUFFIX}.lock"
PID_FILE  = f"bot{_PROC_SUFFIX}.pid"

# ── Logging ───────────────────────────────────────────────────────────────────
# Filenames are mode-prefixed so the two processes never interleave their logs.
LOG_DIR        = "logs"
APP_LOG_FILE   = f"logs/{_LOG_PREFIX}bot.log"
TRADE_LOG_FILE = f"logs/{_LOG_PREFIX}trades.log"
PERF_LOG_FILE  = f"logs/{_LOG_PREFIX}performance.log"

# CRITICAL-only alert sink. Deliberately at the REPO ROOT, not under logs/:
# /etc/logrotate.d/trading-bot globs `logs/*.log`, so a file there would be
# copytruncate'd daily and a Saturday CRITICAL would be gone from the live file
# by the Monday check — precisely the gap this file exists to close. The repo
# root is matched by no logrotate config on this host (verified against all 14),
# because the two root logs are named individually rather than globbed.
#
# This does NOT page anyone; it only guarantees the record survives. It is a
# durable sink, not an alert channel.
CRITICAL_ALERT_FILE = f"{_LOG_PREFIX}critical_alerts.log"

# ── Trade-note markers ────────────────────────────────────────────────────────
# The analyzer classifies exits by pattern-matching the free-text `notes` field
# of a trade record. That couples a writer (whoever places the order) to a reader
# (performance_analyzer) across process boundaries, so the marker lives here
# rather than as a literal at either end — a drifted string would silently
# reclassify trades instead of failing.
#
# CORRECTION: an exit the STRATEGY never signalled — placed by hand to repair a
# bug's damage. Excluded from per-feature stats: attributing it to the entry's
# feature would score the strategy on a trade it did not choose. First use was
# the 2026-07-16 CRL/LII trim, unwinding a 503-induced double entry.
CORRECTION_NOTE_MARKER = "duplicate-entry correction"
