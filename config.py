import os
from dotenv import load_dotenv

load_dotenv()

# ── Run mode ──────────────────────────────────────────────────────────────────
# "equities" (stocks + options) or "futures". Set by main.py's --mode flag (which
# exports BOT_MODE before importing this module) or directly via the environment.
# Drives the singleton lock file and log filenames below so an equities instance
# and a futures instance can run side by side without colliding.
BOT_MODE    = os.environ.get("BOT_MODE", "equities").lower()
_IS_FUTURES = BOT_MODE == "futures"
_LOG_PREFIX = "futures_" if _IS_FUTURES else ""
_PROC_SUFFIX = ".futures" if _IS_FUTURES else ""

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
    ("AAPL", "put"),
]

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
ENABLE_PROFIT_TAKING = False  # dormant insurance — enable only after live stop data
PROFIT_TAKE_PCT      = 0.12   # trigger once up this fraction from entry (+12%)
PROFIT_TAKE_FRACTION = 0.50   # sell this fraction of the held shares (half)
PROFIT_TAKE_RSI_MIN  = 60.0   # only when RSI is at least this (extended)

# ── Position Sizing ───────────────────────────────────────────────────────────
EQUITY_PER_TRADE_PCT = 0.05   # fraction of account equity deployed per stock trade
MAX_POSITIONS        = 20     # skip new stock entries once this many positions are
                              # open (0.05 × 20 = 100% fully deployed)
OPTIONS_CONTRACTS    = 1      # contracts per options trade

# ── Stop Loss (bot-managed trailing stop) ─────────────────────────────────────
# Bot-managed (not broker-native) ATR trailing stop, checked every cycle in
# strategy.evaluate_stock BEFORE the EMA-cross signal. Paper-trading choice for
# now; switch to a broker Sell Stop order when we go live. See stop_prices.json
# for the persisted per-position state (entry, ATR-at-entry, ratcheting stop).
USE_TRAILING_STOP    = True   # master switch; False = no stop checks at all
STOP_LOSS_ATR_MULT   = 2.5    # default/fallback ATR multiple (= risk_on width)
STOP_LOSS_ATR_PERIOD = 14     # ATR lookback (Wilder), computed once at entry
STOP_PRICE_FILE      = "data/stop_prices.json"   # generated (gitignored)

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
ENABLE_SHORTING = True   # master switch; False = long-only (prior behaviour)

# ── Momentum alignment entry (momentum slot only) ─────────────────────────────
# Momentum leaders are already trending when the twice-monthly screen adds them,
# so they never produce a *fresh* EMA cross for the bot to enter on. Give the
# momentum bucket a one-shot "enter on alignment" signal instead; core names keep
# the patient fresh-cross entry. One entry per symbol per rotation, latched in
# MOMENTUM_ENTRY_FILE so a stop-out can't trigger an immediate re-buy.
USE_MOMENTUM_ALIGNMENT = True    # master switch; False = fresh-cross only, all names
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

# ── Polygon.io (momentum-screen data source; free tier) ───────────────────────
POLYGON_API_KEY  = os.environ.get("POLYGON_API_KEY", "")
POLYGON_BASE_URL = "https://api.polygon.io"
POLYGON_MAX_CALLS_PER_MIN = 5    # free-tier rate limit; the screen self-throttles

# ── Claude sentiment analysis (Feature 2 of the VIX + sentiment overlay) ──────
# sentiment_analyzer.py (run weekdays 08:00 ET by systemd) scores market fear from
# Polygon SPY headlines via Claude and writes SENTIMENT_REPORT_FILE. The bot reads
# it each cycle and takes the MORE FEARFUL of {VIX regime, sentiment regime}, plus
# per-sector entry gating. OFF ⇒ the bot ignores sentiment entirely (VIX-only).
ENABLE_SENTIMENT        = True
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
# 5-day-before-expiry quarterly roll (futures_market_hours.front_month_contract).
# YM is excluded for now: the sandbox account is NOT ENTITLED to Dow data.
FUTURES_WATCHLIST = ["ES", "NQ", "RTY"]
FUTURES_CONTRACTS = 1      # contracts per futures trade (fixed size for MVP)
FUTURES_ROLL_DAYS = 5      # roll to the next quarterly this many days before expiry
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
