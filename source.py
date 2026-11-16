"""Picks the data source and returns (matches, candidates).

With ODDS_API_KEY: The Odds API gives real 1X2 odds, markets.py adds the other markets, and
Gemini chooses the best options (or market_only() ranks them if Gemini is unavailable).
Without it: Gemini + Google Search (1X2 only; needs a Gemini key with search quota)."""
import logging
import time

import config
import markets
import odds
import predictor
import research

_cache: dict = {}


def _from_research(matches: list[dict], preds: dict) -> dict:
    out = {}
    for m in matches:
        p = preds.get(m["id"])
        if p:
            out[m["id"]] = [{
                "market": p.pick,
                "label": markets.label(p.pick),
                "odds": m["odds"][p.pick],
                "real": True,
                "confidence": max(1, min(99, int(p.confidence))),
                "reasoning": p.reasoning,
            }]
    return out


async def get_picks(days: list):
    if not config.ODDS_API_KEY:
        matches, preds = await research.get_picks(days)
        return matches, _from_research(matches, preds)

    key = tuple(days)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < config.CACHE_SECONDS:
        return hit[1]

    matches = await odds.get_matches(days)
    if not matches:
        return [], {}
    for m in matches:
        m["selections"] = markets.selections(m["odds"])

    ai_ok = True
    try:
        cands = await predictor.predict(matches)
    except Exception:
        logging.exception("Gemini unavailable, falling back to market-only picks")
        cands = {}
    if not cands:
        ai_ok = False
        cands = predictor.market_only(matches)

    result = (matches, cands)
    if ai_ok:  # don't keep fallback picks for 30 minutes: try Gemini again next time
        _cache[key] = (time.time(), result)
    return result


async def available_days() -> dict:
    """Days that have matches (Odds API mode only)."""
    if not config.ODDS_API_KEY:
        return {}
    return await odds.days_with_games()
