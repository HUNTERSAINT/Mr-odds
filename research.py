"""Gemini + Google Search does everything: finds fixtures and odds for the chosen
days, then predicts. Two steps, because Gemini can't use Google Search and
structured JSON output in the same call:

1. Grounded call: search the web, write research notes (fixtures, odds, form, injuries).
2. Structured call: turn the notes into validated JSON picks.

Because web-sourced odds can be wrong, every item is sanity-checked in code."""
import logging
import time
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

import config

LOCAL = ZoneInfo(config.TIMEZONE)
client = genai.Client(api_key=config.GEMINI_API_KEY)
_cache: dict = {}


async def generate(**kwargs):
    """generate_content with automatic fallback across config.MODELS."""
    last = None
    for model in config.MODELS:
        try:
            return await client.aio.models.generate_content(model=model, **kwargs)
        except errors.APIError as e:
            if e.code in (404, 429, 500, 503):
                logging.warning("model %s unavailable (%s), trying next", model, e.code)
                last = e
                continue
            raise
    raise last

LEAGUES = (
    "Premier League, La Liga, Serie A, Bundesliga, Ligue 1, "
    "UEFA Champions League, UEFA Europa League"
)


class Pick(BaseModel):
    league: str
    home: str
    away: str
    kickoff: str  # ISO 8601, Africa/Lagos time, e.g. 2026-11-21T16:00:00+01:00
    odds_home: float
    odds_draw: float
    odds_away: float
    pick: Literal["home", "draw", "away"]
    confidence: int  # 0-100
    reasoning: str  # under 20 words


def _research_prompt(days: list[date]) -> str:
    ds = "\n".join(f"- {d:%A %d %B %Y}" for d in days)
    return (
        f"Use Google Search to find football matches on these dates (Africa/Lagos time):\n{ds}\n\n"
        f"Only these competitions: {LEAGUES}. Only well-known teams that every major "
        "bookmaker lists.\n"
        "For each match give: league, home team, away team, kickoff time (Lagos time), "
        "current DECIMAL odds for home win / draw / away win as shown by major bookmakers "
        "(use the typical or average value), plus recent form, key injuries or suspensions, "
        "and head-to-head if you find them.\n"
        "STRICT RULES: only report odds you actually saw on a web page. If you cannot find "
        "all three odds for a match, write 'odds not found' and skip that match. Never "
        "estimate or invent odds, times, injuries or results. Cover up to 25 matches."
    )


STRUCTURE_SYSTEM = (
    "You convert football research notes into JSON. Include ONLY matches whose home, draw "
    "and away decimal odds are all explicitly present in the notes. Do not invent anything. "
    "For each match choose the most likely outcome (home/draw/away) using the odds as the "
    "main evidence plus any form/injury info in the notes, give a confidence 0-100 and "
    "reasoning under 20 words. Kickoff must be ISO 8601 with the +01:00 offset."
)


def _check(p: Pick, wanted: set[date]):
    """Reject picks that look hallucinated. Returns kickoff datetime or None."""
    try:
        ko = datetime.fromisoformat(p.kickoff)
    except ValueError:
        return None
    if ko.tzinfo is None:
        ko = ko.replace(tzinfo=LOCAL)
    ko = ko.astimezone(LOCAL)
    if ko.date() not in wanted:
        return None
    odds = (p.odds_home, p.odds_draw, p.odds_away)
    if not all(1.01 <= o <= 30 for o in odds):
        return None
    margin = sum(1 / o for o in odds)  # real 1X2 markets sit around 1.02-1.12
    if not 1.0 <= margin <= 1.20:
        return None
    return ko


async def get_picks(days: list[date]):
    """Returns (matches, preds) in the shape slip.build_slip expects."""
    key = tuple(days)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < config.CACHE_SECONDS:
        return hit[1]

    notes = await generate(
        contents=_research_prompt(days),
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.1,
        ),
    )
    if not notes.text:
        return [], {}

    structured = await generate(
        contents=f"Research notes:\n{notes.text}",
        config=types.GenerateContentConfig(
            system_instruction=STRUCTURE_SYSTEM,
            response_mime_type="application/json",
            response_schema=list[Pick],
            temperature=0.1,
        ),
    )

    wanted = set(days)
    matches, preds, seen = [], {}, set()
    for p in structured.parsed or []:
        ko = _check(p, wanted)
        dup = (p.home.lower(), p.away.lower())
        if ko is None or dup in seen:
            continue
        seen.add(dup)
        mid = str(len(matches))
        matches.append(
            {
                "id": mid,
                "league": p.league,
                "home": p.home,
                "away": p.away,
                "kickoff": ko,
                "odds": {"home": p.odds_home, "draw": p.odds_draw, "away": p.odds_away},
            }
        )
        preds[mid] = p
    matches.sort(key=lambda m: m["kickoff"])

    _cache[key] = (time.time(), (matches, preds))
    return matches, preds
