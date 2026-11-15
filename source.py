"""Picks the data source: The Odds API + Gemini analysis when ODDS_API_KEY is set,
otherwise Gemini + Google Search (needs a Gemini key with search-grounding quota)."""
import time

import config
import odds
import predictor
import research

_cache: dict = {}


async def get_picks(days: list):
    if not config.ODDS_API_KEY:
        return await research.get_picks(days)

    key = tuple(days)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < config.CACHE_SECONDS:
        return hit[1]

    matches = await odds.get_matches(days)
    if not matches:
        return [], {}
    preds = await predictor.predict(matches)
    result = (matches, preds)
    if preds:
        _cache[key] = (time.time(), result)
    return result
