"""Matches from EVERY in-season soccer league on The Odds API, for the /all command.

Kept separate from odds.py (which /start uses and which is not changed). It reuses odds.py's
fetch/cache/credit-guard helpers, so a league fetched by either feature is cached for both.

Credits (free plan = 500/month, 1 per league odds fetch):
- the league list and the per-league fixture lookup (/events) are free; so odds are fetched
  only for leagues that actually have games in the chosen days,
- already-cached leagues cost nothing,
- at most ALL_MAX_FETCH new leagues are fetched per request, never going below CREDIT_RESERVE,
- leagues that are skipped are served from older saved data when there is any."""
import asyncio
import json
import logging
import os
from datetime import datetime, time as dtime, timezone

import httpx

import config
import db
import odds
from research import LOCAL

MAX_FETCH = int(os.getenv("ALL_MAX_FETCH", "20"))  # new (paid) league fetches per request
EVENTS_SECONDS = 3 * 3600
_INF = float("inf")


def _credits() -> int:
    v = db.cache_get("odds:credits", _INF)
    return int(v) if v is not None else 500


def _cache_key(key: str) -> str:
    return f"odds:{key}:{config.DAYS_AHEAD}"  # same key odds._fetch_league uses


async def _sports(client: httpx.AsyncClient) -> dict:
    """{league_key: title} for every soccer league in season (free lookup, cached 6h)."""
    cached = db.cache_get("all:sports", 6 * 3600)
    if cached is not None:
        return json.loads(cached)
    r = await client.get(f"{odds.BASE}/sports", params={"apiKey": config.ODDS_API_KEY})
    r.raise_for_status()
    out = {
        s["key"]: s.get("title") or s["key"]
        for s in r.json()
        if s.get("active") and s["key"].startswith("soccer_") and not s.get("has_outrights")
    }
    db.cache_set("all:sports", json.dumps(out))
    logging.info("all-leagues: %s soccer leagues in season", len(out))
    return out


def _names(sports: dict) -> dict:
    """{league_key: unique display name}; also saved so results can be settled later."""
    names, used = {}, set(odds.LEAGUES.values())
    for key, title in sports.items():
        if key in odds.LEAGUES:
            names[key] = odds.LEAGUES[key]
            continue
        name = title if title not in used else f"{title} ({key.removeprefix('soccer_')})"
        used.add(name)
        names[key] = name
    try:
        saved = json.loads(db.cache_get("all:names", _INF) or "{}")
        saved.update({n: k for k, n in names.items()})
        db.cache_set("all:names", json.dumps(saved))
    except Exception:
        logging.exception("could not save league names")
    return names


def saved_names() -> dict:
    """{display name: league key} of every league seen by /all."""
    try:
        return json.loads(db.cache_get("all:names", _INF) or "{}")
    except Exception:
        return {}


def _bounds(days: list):
    first, last = min(days), max(days)
    start = max(
        datetime.combine(first, dtime(0, 0), tzinfo=LOCAL).astimezone(timezone.utc),
        datetime.now(timezone.utc),
    )
    end = datetime.combine(last, dtime(23, 59, 59), tzinfo=LOCAL).astimezone(timezone.utc)
    return first, last, start, end


async def _count_events(client, sem, key: str, days: list):
    """Number of fixtures a league has in the chosen days (free). None = unknown."""
    first, last, start, end = _bounds(days)
    ck = f"all:ev:{key}:{first}:{last}"
    cached = db.cache_get(ck, EVENTS_SECONDS)
    if cached is not None:
        return int(cached)
    async with sem:
        try:
            r = await client.get(
                f"{odds.BASE}/sports/{key}/events",
                params={
                    "apiKey": config.ODDS_API_KEY,
                    "dateFormat": "iso",
                    "commenceTimeFrom": start.strftime(odds._FMT),
                    "commenceTimeTo": end.strftime(odds._FMT),
                },
            )
            if r.status_code in (404, 422):
                n = 0
            else:
                r.raise_for_status()
                odds.note_credits(r.headers)
                n = len(r.json())
        except httpx.HTTPError:
            logging.warning("events lookup failed for %s", key)
            return None
    db.cache_set(ck, str(n))
    return n


async def _event_counts(client, sports: dict, days: list) -> dict:
    """{key: count or None}. If the events lookup turns out to cost credits, stop using it."""
    keys = list(sports)
    if db.cache_get("all:events_paid", _INF) is not None:
        return {k: (None if k in odds.LEAGUES else 0) for k in keys}  # known leagues only
    sem = asyncio.Semaphore(5)
    if db.cache_get("all:events_free", _INF) is None:
        # probe once, alone, to make sure this lookup really is free on this plan
        before = _credits()
        first = await _count_events(client, sem, keys[0], days)
        after = _credits()
        if after < before:
            logging.warning("events lookup costs credits: disabling it")
            db.cache_set("all:events_paid", "1")
            return {k: (None if k in odds.LEAGUES else 0) for k in keys}
        db.cache_set("all:events_free", "1")
        rest = await asyncio.gather(*(_count_events(client, sem, k, days) for k in keys[1:]))
        return dict(zip(keys, [first, *rest]))
    res = await asyncio.gather(*(_count_events(client, sem, k, days) for k in keys))
    return dict(zip(keys, res))


async def get_matches(days: list):
    """Returns (matches, info). Matches are the same dicts odds.py produces."""
    wanted, now = set(days), datetime.now(LOCAL)
    async with httpx.AsyncClient(timeout=20) as client:
        sports = await _sports(client)
        names = _names(sports)
        counts = await _event_counts(client, sports, days)

        order = {k: i for i, k in enumerate(odds.LEAGUES)}
        cand = sorted(
            (k for k, n in counts.items() if n != 0),
            key=lambda k: (order.get(k, 999), -(counts[k] or 0)),
        )
        fresh = config.SLIP_FRESH_SECONDS if _credits() > 150 else config.ODDS_CACHE_SECONDS
        free = [k for k in cand if db.cache_get(_cache_key(k), fresh) is not None]
        need = [k for k in cand if k not in free]
        budget = min(MAX_FETCH, max(0, _credits() - config.CREDIT_RESERVE))
        fetch, skipped = free + need[:budget], need[budget:]
        logging.info(
            "all-leagues: %s leagues with games, %s cached, %s to fetch, %s skipped",
            len(cand), len(free), min(len(need), budget), len(skipped),
        )

        sem = asyncio.Semaphore(3)

        async def one(key):
            async with sem:
                res = await odds._fetch_league(client, key, fresh)
                await asyncio.sleep(0.4)  # stay under the Odds API rate limit (429)
                return key, res

        got = list(await asyncio.gather(*(one(k) for k in fetch)))

    for k in skipped:  # not enough credits to refresh: use older saved data if any
        old = db.cache_get(_cache_key(k), _INF)
        if old:
            got.append((k, json.loads(old)))

    out, seen = [], set()
    for key, events in got:
        for ev in events:
            m = odds._normalise(ev, names.get(key, key))
            if m and m["id"] not in seen and m["kickoff"] > now and m["kickoff"].date() in wanted:
                seen.add(m["id"])
                out.append(m)
    out.sort(key=lambda m: m["kickoff"])
    leagues = len({m["league"] for m in out})
    logging.info("all-leagues: %s matches from %s leagues", len(out), leagues)
    return out, {"leagues": leagues, "skipped": len(skipped)}
