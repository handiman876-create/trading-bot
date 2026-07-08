import os
from dotenv import load_dotenv

load_dotenv()

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

# ── Watchlist ─────────────────────────────────────────────────────────────────
STOCK_WATCHLIST = ["SPY", "AAPL", "TSLA", "NVDA", "AMD", "MSFT", "GOOGL",
                   "META", "ARM", "CRWV", "AVGO", "AMZN"]

# Options watchlist: list of (symbol, strike, option_type).
# The expiration is no longer hardcoded — it is computed at runtime to the next
# valid monthly expiration (3rd Friday) via market_hours.next_monthly_expiration().
OPTIONS_WATCHLIST = [
    ("SPY",  745.0, "call"),  # ATM: SPY ~$744 (2026-07-08)
    ("AAPL", 315.0, "put"),   # ATM: AAPL ~$313 (2026-07-08)
]

# ── Strategy Parameters ───────────────────────────────────────────────────────
MA_SHORT_PERIOD  = 9     # fast EMA
MA_LONG_PERIOD   = 21    # slow EMA
RSI_PERIOD       = 14
RSI_OVERSOLD     = 30
RSI_OVERBOUGHT   = 70

# ── Position Sizing ───────────────────────────────────────────────────────────
MAX_POSITION_VALUE = 1000.0   # max dollars per stock position
OPTIONS_CONTRACTS  = 1        # contracts per options trade

# ── Poll interval while market is open (seconds) ─────────────────────────────
POLL_INTERVAL = 60

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR        = "logs"
TRADE_LOG_FILE = "logs/trades.log"
PERF_LOG_FILE  = "logs/performance.log"
