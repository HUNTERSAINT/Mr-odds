"""Gemini analyses the matches and chooses the best markets for an accumulator.

Each match comes with a list of options (1X2, double chance, over/under, GG...) and the
probability of each. Gemini ranks up to 3 options per match with a confidence; the slip
builder then combines them. If Gemini is unavailable, market_only() ranks by probability."""
from typing import Literal

from google.genai import types
from pydantic import BaseModel

from markets import KEYS
from odds import favourite_prob
from research import generate

MarketKey = Literal[KEYS]


class PickOut(BaseModel):
    market: MarketKey
    confidence: int  # 0-100, your chance that this selection wins


class MatchOut(BaseModel):
    id: str
    picks: list[PickOut]  # best first, at most 4, each a different market
    reasoning: str  # under ~20 words


SYSTEM = (
    "You are a football analyst building accumulator (parlay) legs. You get upcoming "
    "matches. Each has a list of options with the bookmaker odds and the probability of "
    "each option. Home/draw/away are real bookmaker prices. Double chance, draw no bet, "
    "over/under goals and both-teams-to-score (GG) are estimated from those odds with a "
    "goals model.\n"
    "For each match choose up to 4 DIFFERENT options (ranked best first), using only the "
    "option keys offered for that match, spread across safer and riskier ones so they can "
    "fit different target odds. For every option give a confidence from 0 to 100: your "
    "chance that it wins. Start from the probability given and change it only for real "
    "football reasons (style, derby, stakes). NEVER invent injuries, lineups or recent "
    "results you are not sure about. Give one short reasoning (under 20 words) per match. "
    "Return one item per match, using the exact match id."
)


def _prompt(matches: list[dict]) -> str:
    lines = []
    for m in matches:
        opts = ", ".join(
            f"{k} @{s['odds']} ({s['prob'] * 100:.0f}%)" for k, s in m["selections"].items()
        )
        lines.append(
            f"id={m['id']} | {m['league']} | {m['home']} vs {m['away']} | "
            f"{m['kickoff']:%a %d %b %H:%M} | options: {opts}"
        )
    return "Matches:\n" + "\n".join(lines)


def _cand(key: str, sel: dict, confidence: int, reasoning: str) -> dict:
    return {
        "market": key,
        "label": sel["label"],
        "odds": sel["odds"],
        "real": sel["real"],
        "confidence": max(1, min(99, int(confidence))),
        "reasoning": reasoning,
    }


async def predict(matches: list[dict]) -> dict[str, list[dict]]:
    """{match_id: [candidate, ...]} chosen by Gemini (validated against the offered options)."""
    # Gemini sees at most 40 matches: keep the ones with the clearest favourites.
    matches = sorted(
        (m for m in matches if m.get("selections")),
        key=lambda m: -favourite_prob(m["odds"]),
    )[:40]
    by_id = {m["id"]: m for m in matches}
    resp = await generate(
        contents=_prompt(matches),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=list[MatchOut],
            temperature=0.2,
        ),
    )
    out: dict[str, list[dict]] = {}
    for mo in resp.parsed or []:
        m = by_id.get(mo.id)
        if not m:
            continue
        seen, cands = set(), []
        for pk in mo.picks[:4]:
            sel = m["selections"].get(pk.market)
            if sel and pk.market not in seen:
                seen.add(pk.market)
                cands.append(_cand(pk.market, sel, pk.confidence, mo.reasoning))
        if cands:
            out[m["id"]] = cands
    return out


def market_only(matches: list[dict]) -> dict[str, list[dict]]:
    """No-AI fallback: per match, up to 4 options from safest to highest paying (by probability)."""
    note = "Based on bookmaker odds (AI analysis unavailable)"
    out: dict[str, list[dict]] = {}
    for m in matches:
        sels = m.get("selections") or {}
        likely = sorted(
            ((k, s) for k, s in sels.items() if s["prob"] >= 0.5), key=lambda kv: kv[1]["odds"]
        ) or sorted(sels.items(), key=lambda kv: kv[1]["odds"])
        if not likely:
            continue
        n = len(likely)
        idx = {round(i * (n - 1) / 3) for i in range(4)}  # up to 4, spread over the odds range
        out[m["id"]] = [
            _cand(k, s, round(s["prob"] * 100), note) for k, s in (likely[i] for i in sorted(idx))
        ]
    return out
