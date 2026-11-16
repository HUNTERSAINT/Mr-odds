"""Settle pending picks. With ODDS_API_KEY the final scores come from The Odds API
(only leagues that have waiting picks are queried, to save credits); otherwise Gemini +
Google Search finds them."""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from google.genai import types
from pydantic import BaseModel

import config
import db
import odds
from research import generate

# A match is checked once kickoff was this long ago (90 min + half-time + stoppage + buffer).
FINISHED_AFTER = timedelta(hours=3)
# Give up on a pick if no score is found this long after kickoff (Odds API scores go back 3 days).
GIVE_UP_AFTER = timedelta(days=3)


class Score(BaseModel):
    idx: int
    finished: bool
    home_goals: int | None = None
    away_goals: int | None = None


async def _scores_oddsapi(rows) -> dict:
    """Returns {(home, away): (home_goals, away_goals)} for completed matches."""
    leagues = {r["league"] for r in rows}
    keys = [k for k, name in odds.LEAGUES.items() if name in leagues]
    out = {}
    async with httpx.AsyncClient(timeout=20) as c:
        for k in keys:
            r = await c.get(
                f"{odds.BASE}/sports/{k}/scores/",
                params={"apiKey": config.ODDS_API_KEY, "daysFrom": 3, "dateFormat": "iso"},
            )
            r.raise_for_status()
            odds.note_credits(r.headers)
            for ev in r.json():
                if not ev.get("completed") or not ev.get("scores"):
                    continue
                try:
                    sc = {s["name"]: int(s["score"]) for s in ev["scores"]}
                    out[(ev["home_team"], ev["away_team"])] = (
                        sc[ev["home_team"]], sc[ev["away_team"]]
                    )
                except (KeyError, ValueError, TypeError):
                    continue
    return out


async def _scores_search(rows) -> dict:
    listing = "\n".join(
        f"{i}. {r['home']} vs {r['away']} (kickoff {r['kickoff_utc']} UTC)"
        for i, r in enumerate(rows)
    )
    notes = await generate(
        contents=(
            "Use Google Search to find the FINAL full-time score (90 minutes plus stoppage "
            "time; ignore extra time and penalty shootouts) for each football match below. "
            "If a match is postponed, abandoned, not yet finished or you cannot find a "
            "reliable score, say so. Never guess a score.\n\n" + listing
        ),
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0.0
        ),
    )
    if not notes.text:
        return {}
    structured = await generate(
        contents=f"Matches:\n{listing}\n\nResearch notes:\n{notes.text}",
        config=types.GenerateContentConfig(
            system_instruction=(
                "Return one item per match index. finished=true only if the notes clearly "
                "give a final 90-minute score; then fill home_goals and away_goals. "
                "Otherwise finished=false and leave goals null. Do not invent scores."
            ),
            response_mime_type="application/json",
            response_schema=list[Score],
            temperature=0.0,
        ),
    )
    out = {}
    for sc in structured.parsed or []:
        if 0 <= sc.idx < len(rows) and sc.finished and sc.home_goals is not None \
                and sc.away_goals is not None:
            r = rows[sc.idx]
            out[(r["home"], r["away"])] = (sc.home_goals, sc.away_goals)
    return out


async def settle_pending() -> int:
    """Returns the number of matches settled."""
    now = datetime.now(timezone.utc)
    cutoff = (now - FINISHED_AFTER).isoformat(timespec="seconds")
    rows = db.matches_to_settle(cutoff)
    settled = 0
    if rows:
        found = await (_scores_oddsapi(rows) if config.ODDS_API_KEY else _scores_search(rows))
        for r in rows:
            goals = found.get((r["home"], r["away"]))
            if not goals:
                continue
            hg, ag = goals
            db.settle_match(r["home"], r["away"], r["kickoff_utc"], hg, ag)
            settled += 1
    db.void_stale((now - GIVE_UP_AFTER).isoformat(timespec="seconds"))
    logging.info("settlement run: %s matches settled, %s checked", settled, len(rows))
    return settled
