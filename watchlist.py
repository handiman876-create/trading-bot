"""
Effective stock watchlist — the single source of truth for which stocks the bot
trades each cycle.

The live list is assembled fresh every cycle as:

    CORE_WATCHLIST  ∪  momentum slot  ∪  currently-held symbols

- CORE_WATCHLIST: the fixed 15 (config.CORE_MEGA + CORE_GROWTH).
- momentum slot: up to MOMENTUM_SLOT_SIZE names from data/momentum_watchlist.json,
  refreshed twice monthly by momentum_screen.py. Any read failure degrades to an
  empty slot (core-only trading) rather than crashing the cycle.
- held symbols: names we still hold. This is the orphan-guard — when a name
  rotates OUT of the momentum slot while we still hold shares, keeping it in the
  list means evaluate_stock still sees its SELL cross instead of stranding the
  position. (Mirrors evaluate_future's stale-contract roll guard.)
"""

import json
import logging
from datetime import datetime, timezone

import config

logger = logging.getLogger(__name__)


def _load_momentum_symbols() -> list[str]:
    """Read the generated momentum slot. Returns [] on any problem (missing file,
    malformed JSON, wrong shape) so a failed/never-run screen degrades to
    core-only trading. Warns — but still uses — a slot older than
    MOMENTUM_MAX_AGE_DAYS so a missed rotation is visible in the logs."""
    path = config.MOMENTUM_WATCHLIST_FILE
    try:
        with open(path) as f:
            doc = json.load(f)
    except FileNotFoundError:
        logger.info("No momentum watchlist at %s yet — trading core-only.", path)
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Momentum watchlist unreadable (%s) — trading core-only.", exc)
        return []

    symbols = doc.get("symbols")
    if not isinstance(symbols, list):
        logger.warning("Momentum watchlist %s has no 'symbols' list — core-only.", path)
        return []

    generated = doc.get("generated")
    if generated:
        try:
            age_days = (datetime.now(timezone.utc)
                        - datetime.fromisoformat(generated)).days
            if age_days > config.MOMENTUM_MAX_AGE_DAYS:
                logger.warning("Momentum watchlist is %d days old (> %d) — "
                               "rotation may have missed a run.",
                               age_days, config.MOMENTUM_MAX_AGE_DAYS)
        except ValueError:
            logger.warning("Momentum watchlist 'generated' timestamp unparseable: %r",
                           generated)

    return [str(s).upper() for s in symbols]


def effective_stock_watchlist(positions: list[dict]) -> list[str]:
    """CORE ∪ momentum ∪ held, de-duplicated with a stable order
    (core first, then momentum, then any held stragglers)."""
    core = [s.upper() for s in config.CORE_WATCHLIST]
    momentum = _load_momentum_symbols()
    held = [str(p.get("symbol", "")).upper()
            for p in positions if int(p.get("quantity", 0)) != 0 and p.get("symbol")]
    # dict.fromkeys preserves first-seen order while removing duplicates.
    return list(dict.fromkeys(core + momentum + held))
