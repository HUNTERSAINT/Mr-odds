"""Gemini analyses real bookmaker odds (no web search needed)."""
from typing import Literal

from google.genai import types
from pydantic import BaseModel

from odds import favourite_prob
from research import generate


class Prediction(BaseModel):
    id: str
    pick: Literal["home", "draw", "away"]
    confidence: int  # 0-100
    reasoning: str  # under ~20 words


SYSTEM = (
    "You are a football analyst. You get upcoming matches with the median bookmaker odds "
    "and the market-implied probabilities. For each match choose the most likely outcome "
    "(home, draw or away) and give a confidence score from 0 to 100. Use the odds as your "
    "main evidence plus general knowledge of the teams. NEVER invent injuries, lineups or "
    "recent results. Keep reasoning under 20 words. Return exactly one prediction per "
    "match, using the exact match id."
)


def _implied(odds: dict) -> dict:
    raw = {k: 1 / v for k, v in odds.items()}
    total = sum(raw.values())
    return {k: round(v / total * 100, 1) for k, v in raw.items()}


def _prompt(matches: list[dict]) -> str:
    lines = []
    for m in matches:
        imp = _implied(m["odds"])
        lines.append(
            f"id={m['id']} | {m['league']} | {m['home']} vs {m['away']} | "
            f"{m['kickoff']:%a %d %b %H:%M} | odds H {m['odds']['home']} "
            f"D {m['odds']['draw']} A {m['odds']['away']} | implied% H {imp['home']} "
            f"D {imp['draw']} A {imp['away']}"
        )
    return "Matches:\n" + "\n".join(lines)


async def predict(matches: list[dict]) -> dict[str, Prediction]:
    # Gemini sees at most 40 matches: keep the ones with the clearest favourites.
    matches = sorted(matches, key=lambda m: -favourite_prob(m['odds']))[:40]
    resp = await generate(
        contents=_prompt(matches),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=list[Prediction],
            temperature=0.2,
        ),
    )
    valid = {m["id"] for m in matches}
    return {p.id: p for p in (resp.parsed or []) if p.id in valid}
