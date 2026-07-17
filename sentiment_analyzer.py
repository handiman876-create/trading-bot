"""
Morning market-sentiment analysis (Feature 2 of the VIX + sentiment risk overlay).

Run once each weekday at 08:00 ET by deploy/sentiment-analysis.timer. Pulls the
last 24h of broad-market (SPY) headlines from Polygon, asks Claude to score
market fear 1-10 and rate per-sector risk, and writes data/sentiment_report.json.

The bot (strategy.py) reads that report every cycle via current_sentiment() and
combines it with the VIX regime (belt & suspenders — the MORE fearful of the two
wins) plus per-sector entry gating (a "high"-risk sector blocks NEW long entries
into its symbols).

DESIGN — this NEVER blocks trading on its own failure:
  * main() always exits 0 and always leaves a VALID report behind; any failure
    (Polygon down, Claude down, bad JSON, cost overrun) writes a NEUTRAL report
    (risk_on / all sectors low / fallback=true) instead of crashing or omitting.
  * current_sentiment() treats a missing / stale / corrupt report as NEUTRAL too.
    "Stale" = older than config.SENTIMENT_MAX_AGE_HOURS. Because the timer is
    weekdays-only, Friday's report ages ~72h by Monday's pre-open — well past the
    window — so Monday runs NEUTRAL until the 08:00 timer fires. That is deliberate:
    stale sentiment must not drive Monday's decisions; the live VIX regime still does.

RUNTIME: invoked via .venv/bin/python (the venv carries `anthropic`; the bot's
system python never imports it — the heavy imports here are lazy, inside the
functions the timer job calls, so `import sentiment_analyzer` stays cheap).
"""

import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta

import config

logger = logging.getLogger("sentiment")

# ── Sector → symbols (the critical link: "tech risk HIGH" ⇒ skip NVDA/AMD/… entry).
# Consumed by strategy.py via sectors_blocked(). Energy/airlines are already removed
# from the tradable universe by the momentum sector filter, so their lists are empty
# (kept as keys for schema completeness and so a "high" reading is a harmless no-op).
SECTOR_TO_SYMBOLS = {
    "tech":        ["NVDA", "AMD", "CRWD", "ARM", "AAPL", "MSFT", "GOOGL",
                    "META", "QQQ", "DDOG", "CRL"],
    "financials":  ["JPM", "BLK", "MS"],
    "healthcare":  ["CAH", "HCA", "TMO", "DHR"],
    "industrials": ["LII", "CAT", "DHR"],
    "consumer":    ["KO", "COST", "TGT"],
    "energy":      [],   # already excluded by the momentum sector filter
    "airlines":    [],   # already excluded (Passenger Airlines sub-industry)
}

# The six sectors Claude scores (airlines is universe-excluded, so not scored).
_SECTORS = ("tech", "energy", "financials", "healthcare", "consumer", "industrials")
_RISK_LEVELS = ("low", "medium", "high")


def _regime_from_score(score: int) -> str:
    """fear_score → regime (spec): 1-3 risk_on · 4-6 cautious · 7-8 defensive · 9-10 crisis."""
    if score >= 9:
        return "crisis"
    if score >= 7:
        return "defensive"
    if score >= 4:
        return "cautious"
    return "risk_on"


def _neutral_report(reason: str) -> dict:
    """The fallback report. NEUTRAL = risk_on / all sectors low, so a failure never
    injects fear or blocks trading — the combination step then defers entirely to the
    live VIX regime. `fallback=true` marks it so the banner/logs are honest about it."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fear_score": 1,
        "regime": "risk_on",
        "top_risks": [],
        "sector_risks": {s: "low" for s in _SECTORS},
        "summary": f"neutral fallback ({reason})",
        "headlines_analyzed": 0,
        "fallback": True,
    }


def _fetch_headlines() -> list | None:
    """Last-24h SPY headlines from Polygon /v2/reference/news, or None on failure."""
    import requests  # lazy: only the timer job fetches
    if not config.POLYGON_API_KEY:
        logger.error("POLYGON_API_KEY missing — cannot fetch headlines")
        return None
    since = (datetime.now(timezone.utc)
             - timedelta(hours=config.SENTIMENT_NEWS_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        r = requests.get(
            f"{config.POLYGON_BASE_URL}/v2/reference/news",
            params={"ticker": config.SENTIMENT_NEWS_TICKER, "order": "desc",
                    "limit": config.SENTIMENT_NEWS_LIMIT, "published_utc.gte": since,
                    "apiKey": config.POLYGON_API_KEY},
            timeout=15,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as exc:
        logger.error("Polygon news fetch failed: %s", exc)
        return None
    headlines = [{"title": x.get("title", ""), "published_utc": x.get("published_utc", "")}
                 for x in results if x.get("title")]
    if not headlines:
        logger.warning("Polygon returned no headlines in the last %dh",
                       config.SENTIMENT_NEWS_HOURS)
        return None
    return headlines


def _build_messages(headlines: list):
    """(system, user) prompt. The schema is stated verbatim in the prompt and the
    model is told to emit JSON only; the current date is injected for context."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = "\n".join(f"{i + 1}. {h['title']}" for i, h in enumerate(headlines))
    system = ("You are a financial market sentiment analyzer. Respond with ONE JSON "
              "object and nothing else — no prose, no explanation, no code fences, no "
              "markdown. Use double quotes on every key and string value.")
    user = (
        f"Today is {today}. Based on these {len(headlines)} US-market headlines from "
        f"the last 24 hours, rate overall stock-market FEAR from 1 to 10 "
        f"(1 = calm/greed, 10 = panic), list the top 3 risk factors, and rate each "
        f'sector\'s risk as "low", "medium", or "high".\n\n'
        f"Headlines:\n{lines}\n\n"
        "Respond with EXACTLY this JSON shape and keys:\n"
        '{"fear_score": <integer 1-10>, '
        '"top_risks": ["risk1", "risk2", "risk3"], '
        '"sector_risks": {"tech": "low|medium|high", "energy": "low|medium|high", '
        '"financials": "low|medium|high", "healthcare": "low|medium|high", '
        '"consumer": "low|medium|high", "industrials": "low|medium|high"}, '
        '"summary": "one sentence"}'
    )
    return system, user


def _parse_json(text: str) -> dict | None:
    """Extract and parse the first {...} block from the model reply (robust to any
    stray prose/fences despite the instruction)."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _call_claude(system: str, user: str):
    """Return (parsed_dict|None, cost_usd). Thinking omitted (off) — this is a cheap
    classification. Cost is computed from usage for the guardrail."""
    import anthropic  # lazy: only the timer job (on .venv) imports this
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        logger.error("ANTHROPIC_API_KEY not set in the environment")
        return None, 0.0
    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=config.SENTIMENT_MODEL,
            max_tokens=config.SENTIMENT_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        cost = ((resp.usage.input_tokens / 1_000_000) * config.SENTIMENT_PRICE_IN
                + (resp.usage.output_tokens / 1_000_000) * config.SENTIMENT_PRICE_OUT)
        return _parse_json(text), cost
    except Exception as exc:
        logger.error("Claude sentiment call failed: %s", exc)
        return None, 0.0


def _validate(d: dict, n: int) -> dict:
    """Coerce a model reply into the strict report schema: clamp fear_score to 1-10,
    derive regime from it (ignore any model-supplied regime), fill every sector key,
    cap top_risks at 3. Never raises."""
    try:
        score = int(round(float(d.get("fear_score", 1))))
    except Exception:
        score = 1
    score = max(1, min(10, score))
    sr_in = d.get("sector_risks") or {}
    sector_risks = {}
    for s in _SECTORS:
        v = str(sr_in.get(s, "low")).lower().strip()
        sector_risks[s] = v if v in _RISK_LEVELS else "low"
    risks = [str(x) for x in (d.get("top_risks") or [])][:3]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fear_score": score,
        "regime": _regime_from_score(score),
        "top_risks": risks,
        "sector_risks": sector_risks,
        "summary": str(d.get("summary", ""))[:200],
        "headlines_analyzed": n,
        "fallback": False,
    }


def _write(report: dict) -> None:
    """Atomic write (temp + rename) so the bot never reads a half-written report."""
    path = config.SENTIMENT_REPORT_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(report, f, indent=2)
    os.replace(tmp, path)


# ── Reader side — used by strategy.py every cycle (no network, no anthropic) ───
def current_sentiment(now: datetime = None) -> dict:
    """Latest report, or a NEUTRAL fallback if disabled / missing / stale / corrupt.
    `now` is injectable for tests. Stale = older than config.SENTIMENT_MAX_AGE_HOURS."""
    if not config.ENABLE_SENTIMENT:
        return _neutral_report("sentiment disabled")
    try:
        with open(config.SENTIMENT_REPORT_FILE) as f:
            rep = json.load(f)
    except Exception:
        return _neutral_report("no report file")
    try:
        gen = datetime.fromisoformat(rep.get("generated_at", ""))
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=timezone.utc)
    except Exception:
        return _neutral_report("bad timestamp")
    ref = now or datetime.now(timezone.utc)
    age_h = (ref - gen).total_seconds() / 3600.0
    if age_h > config.SENTIMENT_MAX_AGE_HOURS:
        return _neutral_report(f"stale {age_h:.0f}h > {config.SENTIMENT_MAX_AGE_HOURS}h")
    return rep


def sentiment_regime(report: dict) -> str:
    return report.get("regime", "risk_on")


def sectors_blocked(report: dict) -> set:
    """Symbols to block from NEW entries: everything in a sector rated 'high'."""
    blocked = set()
    for sector, risk in (report.get("sector_risks") or {}).items():
        if str(risk).lower() == "high":
            blocked.update(SECTOR_TO_SYMBOLS.get(sector, []))
    return blocked


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        headlines = _fetch_headlines()
        if not headlines:
            report = _neutral_report("polygon fetch failed or empty")
        else:
            system, user = _build_messages(headlines)
            parsed, cost = _call_claude(system, user)
            if cost > config.SENTIMENT_MAX_COST_USD:
                logger.error("SENTIMENT COST ALERT: run cost $%.4f exceeded cap $%.2f",
                             cost, config.SENTIMENT_MAX_COST_USD)
            if not parsed:
                report = _neutral_report("claude call or JSON parse failed")
            else:
                report = _validate(parsed, len(headlines))
                logger.info("Sentiment: fear=%d/10 regime=%s risks=%s cost=$%.4f",
                            report["fear_score"], report["regime"],
                            report["top_risks"], cost)
    except Exception as exc:                       # never let the timer job crash
        logger.exception("sentiment run crashed: %s", exc)
        report = _neutral_report("unexpected error")
    _write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
