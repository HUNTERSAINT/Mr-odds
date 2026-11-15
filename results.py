"""Settle pending picks: Gemini + Google Search finds final scores, code decides won/lost."""
import logging
from datetime import datetime, timedelta, timezone

from google.genai import types
from pydantic import BaseModel

import config
import db
from research import client

# A match is checked once kickoff was this long ago (90 min + half-time + stoppage + buffer).
FINISHED_AFTER = timedelta(hours=3)
# Give up on a pick if no score is found this long after kickoff.
GIVE_UP_AFTER = timedelta(days=4)


class Score(BaseModel):
    idx: int
    finished: bool
    home_goals: int | None = None
    away_goals: int | None = None


async def _find_scores(rows):
    listing = "\n".join(
        f"{i}. {r['home']} vs {r['away']} (kickoff {r['kickoff_utc']} UTC)"
        for i, r in enumerate(rows)
    )
    notes = await client.aio.models.generate_content(
        model=config.GEMINI_MODEL,
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
        return []
    structured = await client.aio.models.generate_content(
        model=config.GEMINI_MODEL,
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
    return structured.parsed or []


async def settle_pending() -> int:
    """Returns the number of matches settled."""
    now = datetime.now(timezone.utc)
    cutoff = (now - FINISHED_AFTER).isoformat(timespec="seconds")
    rows = db.matches_to_settle(cutoff)
    settled = 0
    if rows:
        for sc in await _find_scores(rows):
            if not (0 <= sc.idx < len(rows)) or not sc.finished:
                continue
            if sc.home_goals is None or sc.away_goals is None:
                continue
            r = rows[sc.idx]
            if sc.home_goals > sc.away_goals:
                outcome = "home"
            elif sc.home_goals < sc.away_goals:
                outcome = "away"
            else:
                outcome = "draw"
            db.settle_match(
                r["home"], r["away"], r["kickoff_utc"], outcome,
                f"{sc.home_goals}-{sc.away_goals}",
            )
            settled += 1
    db.void_stale((now - GIVE_UP_AFTER).isoformat(timespec="seconds"))
    logging.info("settlement run: %s matches settled, %s checked", settled, len(rows))
    return settled
