"""Fixtures + odds from The Odds API, restricted to well-known leagues and to matches
priced by many bookmakers.

Credits: the free plan has 500 a month and every league fetch costs 1. So:
- only leagues that are in season are fetched (the /sports lookup is free),
- results are cached in SQLite (survives restarts) and fetched once at a time per league,
- when credits run low the bot serves saved data instead of failing."""
import asyncio
import json
import logging
import statistics
from datetime import datetime, time as dtime, timedelta, timezone

import httpx

import config
import db
from research import LOCAL

BASE = "https://api.the-odds-api.com/v4"

# In priority order. Names must be unique (results.py maps names back to keys).
LEAGUES = {
    "soccer_epl": "Premier League",
    "soccer_spain_la_liga": "La Liga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_france_ligue_one": "Ligue 1",
    "soccer_uefa_champs_league": "Champions League",
    "soccer_uefa_europa_league": "Europa League",
    "soccer_uefa_europa_conference_league": "Conference League",
    "soccer_efl_champ": "Championship",
    "soccer_netherlands_eredivisie": "Eredivisie",
    "soccer_portugal_primeira_liga": "Primeira Liga",
    "soccer_turkey_super_league": "Turkish Super Lig",
    "soccer_belgium_first_div": "Belgian Pro League",
    "soccer_spl": "Scottish Premiership",
    "soccer_brazil_campeonato": "Brazil Serie A",
    "soccer_argentina_primera_division": "Argentina Primera",
    "soccer_usa_mls": "MLS",
    "soccer_mexico_ligamx": "Liga MX",
    "soccer_conmebol_copa_libertadores": "Copa Libertadores",
    "soccer_fa_cup": "FA Cup",
    "soccer_england_efl_cup": "League Cup",
    "soccer_germany_bundesliga2": "Bundesliga 2",
    "soccer_italy_serie_b": "Serie B",
    "soccer_spain_segunda_division": "La Liga 2",
    "soccer_france_ligue_two": "Ligue 2",
}

_FMT = "%Y-%m-%dT%H:%M:%SZ"
_locks: dict[str, asyncio.Lock] = {}
_INF = float("inf")


def _window() -> tuple[str, str]:
    """Always fetch the whole look-ahead window per league, so one cached fetch serves any day choice."""
    start = datetime.now(timezone.utc)
    last_day = datetime.now(LOCAL).date() + timedelta(days=config.DAYS_AHEAD)
    end = datetime.combine(last_day, dtime(23, 59, 59), tzinfo=LOCAL).astimezone(timezone.utc)
    return start.strftime(_FMT), end.strftime(_FMT)


def note_credits(headers) -> None:
    """Remember how many credits are left (used by the low-credit guard)."""
    rem = headers.get("x-requests-remaining")
    if rem is None:
        return
    logging.info("Odds API credits remaining: %s", rem)
    try:
        db.cache_set("odds:credits", str(int(float(rem))))
    except ValueError:
        pass


def credits_low() -> bool:
    v = db.cache_get("odds:credits", _INF)
    return v is not None and int(v) < config.CREDIT_RESERVE


def _kickoff(ev: dict) -> datetime:
    return datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).astimezone(LOCAL)


async def _active_leagues(client: httpx.AsyncClient) -> dict:
    """Leagues that are in season right now (free lookup, cached 6h), best first."""
    keys = None
    cached = db.cache_get("odds:sports", 6 * 3600)
    if cached is not None:
        keys = set(json.loads(cached))
    else:
        try:
            r = await client.get(f"{BASE}/sports", params={"apiKey": config.ODDS_API_KEY})
            r.raise_for_status()
            keys = {s["key"] for s in r.json() if s.get("active") and s["key"].startswith("soccer_")}
            db.cache_set("odds:sports", json.dumps(sorted(keys)))
            logging.info("Active soccer leagues on The Odds API: %s", ", ".join(sorted(keys)))
        except Exception:
            logging.exception("could not list active leagues, trying all configured ones")
    if not keys:
        keys = None
    chosen = {k: n for k, n in LEAGUES.items() if keys is None or k in keys}
    return dict(list(chosen.items())[: config.MAX_LEAGUES])


async def _fetch_league(client: httpx.AsyncClient, key: str, max_age: float) -> list:
    """Cached events for one league; refreshes (1 credit) only if older than max_age seconds."""
    ck = f"odds:{key}:{config.DAYS_AHEAD}"
    cached = db.cache_get(ck, max_age)
    if cached is not None:
        return json.loads(cached)

    async with _locks.setdefault(key, asyncio.Lock()):
        # another request may have refreshed it while we waited for the lock
        cached = db.cache_get(ck, max_age)
        if cached is not None:
            return json.loads(cached)

        stale = db.cache_get(ck, _INF)
        if credits_low():
            logging.warning("Odds API credits low: using saved data for %s", key)
            return json.loads(stale) if stale else []

        start, end = _window()
        try:
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
            if r.status_code in (404, 422):  # league not available right now
                data = []
            else:
                r.raise_for_status()
                data = r.json()
                note_credits(r.headers)
        except httpx.HTTPError:
            logging.exception("Odds API fetch failed for %s, using saved data", key)
            return json.loads(stale) if stale else []
        db.cache_set(ck, json.dumps(data))
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
    return {
        "id": ev["id"],
        "league": league,
        "home": home,
        "away": away,
        "kickoff": _kickoff(ev),
        "odds": {k: round(statistics.median(v), 2) for k, v in prices.items()},
        "bookmakers": books,
    }


def favourite_prob(odds: dict) -> float:
    """Favourite's win probability from the odds, with the bookmaker margin removed."""
    imp = [1 / v for v in odds.values()]
    return max(imp) / sum(imp)


async def _all_matches(wanted: set | None = None) -> list[dict]:
    """Every upcoming usable match in the look-ahead window.

    Normal reads use the long cache. When `wanted` days are given (building a slip), leagues
    that have games on those days are refreshed if their data is older than SLIP_FRESH_HOURS,
    so the odds on a slip are reasonably current."""
    now = datetime.now(LOCAL)

    async with httpx.AsyncClient(timeout=20) as client:
        leagues = await _active_leagues(client)

        async def one(key: str, name: str) -> list[dict]:
            events = await _fetch_league(client, key, config.ODDS_CACHE_SECONDS)
            if wanted and any(_kickoff(ev).date() in wanted for ev in events):
                events = await _fetch_league(client, key, config.SLIP_FRESH_SECONDS)
            return [m for m in (_normalise(ev, name) for ev in events) if m and m["kickoff"] > now]

        batches = await asyncio.gather(*(one(k, n) for k, n in leagues.items()))

    out = [m for batch in batches for m in batch]
    out.sort(key=lambda m: m["kickoff"])
    logging.info("odds: %s leagues in season, %s usable matches", len(leagues), len(out))
    return out


async def get_matches(days: list) -> list[dict]:
    wanted = set(days)
    return [m for m in await _all_matches(wanted) if m["kickoff"].date() in wanted]


async def days_with_games() -> dict:
    """{date: {"games": n, "strong": k}} for the look-ahead window."""
    counts: dict = {}
    for m in await _all_matches():
        c = counts.setdefault(m["kickoff"].date(), {"games": 0, "strong": 0})
        c["games"] += 1
        if favourite_prob(m["odds"]) >= config.HIGH_PROB:
            c["strong"] += 1
    return counts
