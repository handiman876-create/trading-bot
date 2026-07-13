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
STOP_LOSS_ATR_MULT   = 2.5    # stop sits this many ATRs below the high-water mark
STOP_LOSS_ATR_PERIOD = 14     # ATR lookback (Wilder), computed once at entry
STOP_PRICE_FILE      = "data/stop_prices.json"   # generated (gitignored)

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

# ── Polygon.io (momentum-screen data source; free tier) ───────────────────────
POLYGON_API_KEY  = os.environ.get("POLYGON_API_KEY", "")
POLYGON_BASE_URL = "https://api.polygon.io"
POLYGON_MAX_CALLS_PER_MIN = 5    # free-tier rate limit; the screen self-throttles

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
