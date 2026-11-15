"""Fixtures + odds from The Odds API, restricted to major leagues and to matches
priced by many bookmakers. Free plan = 500 credits/month and each league fetch costs
1 credit, so responses are cached in SQLite (survives restarts) for ODDS_CACHE_HOURS."""
import json
import logging
import statistics
from datetime import datetime, time as dtime, timedelta, timezone

import httpx

import config
import db
from research import LOCAL

BASE = "https://api.the-odds-api.com/v4"

LEAGUES = {
    "soccer_epl": "Premier League",
    "soccer_spain_la_liga": "La Liga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_france_ligue_one": "Ligue 1",
    "soccer_uefa_champs_league": "Champions League",
    "soccer_uefa_europa_league": "Europa League",
}

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _window() -> tuple[str, str]:
    """Always fetch the next 7 days per league, so one cached fetch serves any day choice."""
    start = datetime.now(timezone.utc)
    last_day = datetime.now(LOCAL).date() + timedelta(days=7)
    end = datetime.combine(last_day, dtime(23, 59, 59), tzinfo=LOCAL).astimezone(timezone.utc)
    return start.strftime(_FMT), end.strftime(_FMT)


async def _fetch_league(client: httpx.AsyncClient, key: str) -> list:
    cached = db.cache_get(f"odds:{key}", config.ODDS_CACHE_SECONDS)
    if cached is not None:
        return json.loads(cached)
    start, end = _window()
    r = await client.get(
        f"{BASE}/sports/{key}/odds",
        params={
            "apiKey": config.ODDS_API_KEY,
            "regions": config.ODDS_REGIONS,
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
            "commenceTimeFrom": start,
            "commenceTimeTo": end,
        },
    )
    if r.status_code in (404, 422):  # league not in season
        data = []
    else:
        r.raise_for_status()
        data = r.json()
        logging.info("Odds API credits remaining: %s", r.headers.get("x-requests-remaining"))
    db.cache_set(f"odds:{key}", json.dumps(data))
    return data


def _normalise(ev: dict, league: str):
    home, away = ev["home_team"], ev["away_team"]
    prices = {"home": [], "draw": [], "away": []}
    for bk in ev.get("bookmakers", []):
        for m in bk.get("markets", []):
            if m["key"] != "h2h":
                continue
            for o in m["outcomes"]:
                if o["name"] == home:
                    prices["home"].append(o["price"])
                elif o["name"] == away:
                    prices["away"].append(o["price"])
                else:
                    prices["draw"].append(o["price"])
    books = len(ev.get("bookmakers", []))
    if books < config.MIN_BOOKMAKERS or not all(prices.values()):
        return None
    kickoff = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).astimezone(LOCAL)
    return {
        "id": ev["id"],
        "league": league,
        "home": home,
        "away": away,
        "kickoff": kickoff,
        "odds": {k: round(statistics.median(v), 2) for k, v in prices.items()},
        "bookmakers": books,
    }


async def get_matches(days: list) -> list[dict]:
    wanted = set(days)
    now = datetime.now(LOCAL)
    out = []
    async with httpx.AsyncClient(timeout=20) as client:
        for key, name in LEAGUES.items():
            for ev in await _fetch_league(client, key):
                m = _normalise(ev, name)
                if m and m["kickoff"].date() in wanted and m["kickoff"] > now:
                    out.append(m)
    out.sort(key=lambda m: m["kickoff"])
    return out
