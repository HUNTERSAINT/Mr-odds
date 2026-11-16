"""Betting markets beyond 1X2 (over/under, GG, double chance, draw no bet).

Real bookmaker prices for these markets cost extra Odds API credits per match, which the
free plan (500 credits/month) cannot afford at scale. So only 1X2 prices are real here.
Everything else is estimated from the 1X2 odds:

- double chance and draw no bet follow exactly from the 1X2 probabilities,
- goals markets and GG come from a Poisson goals model (with the Dixon-Coles low-score
  correction) fitted to the 1X2 probabilities.

Estimated odds include an assumed bookmaker margin so they land near real prices, but real
prices will differ a little. Output marks them `real: False` so the bot can label them."""
import math
from functools import lru_cache

import config

MAXG = 10  # goals per team considered by the model
RHO = -0.10  # Dixon-Coles correction (slightly more 0-0 and 1-1 than plain Poisson)

LABELS = {
    "home": "Home win",
    "draw": "Draw",
    "away": "Away win",
    "dc_1x": "Home or Draw (1X)",
    "dc_x2": "Draw or Away (X2)",
    "dc_12": "Home or Away (12)",
    "dnb_home": "Draw no bet: Home",
    "dnb_away": "Draw no bet: Away",
    "over_0.5": "Over 0.5 goals",
    "over_1.5": "Over 1.5 goals",
    "over_2.5": "Over 2.5 goals",
    "over_3.5": "Over 3.5 goals",
    "under_0.5": "Under 0.5 goals",
    "under_1.5": "Under 1.5 goals",
    "under_2.5": "Under 2.5 goals",
    "under_3.5": "Under 3.5 goals",
    "btts_yes": "Both teams to score: Yes (GG)",
    "btts_no": "Both teams to score: No (NG)",
}
KEYS = tuple(LABELS)
REAL_KEYS = ("home", "draw", "away")


def label(key: str) -> str:
    return LABELS.get(key, key)


def is_real(key: str) -> bool:
    return key in REAL_KEYS


# ---------------------------------------------------------------- goals model
def _pmf(lam: float) -> list[float]:
    out = [math.exp(-lam)]
    for k in range(1, MAXG + 1):
        out.append(out[-1] * lam / k)
    return out


def _hda(lh: float, la: float) -> tuple[float, float, float]:
    """Home/draw/away probabilities for expected goals lh, la (fast, O(MAXG))."""
    ph, pa = _pmf(lh), _pmf(la)
    h = d = 0.0
    cum_a = 0.0  # P(away goals < i)
    for i in range(MAXG + 1):
        h += ph[i] * cum_a
        d += ph[i] * pa[i]
        cum_a += pa[i]
    tot = sum(ph) * sum(pa)
    a = tot - h - d
    d00 = ph[0] * pa[0] * (-lh * la * RHO)
    d01 = ph[0] * pa[1] * (lh * RHO)
    d10 = ph[1] * pa[0] * (la * RHO)
    d11 = ph[1] * pa[1] * (-RHO)
    z = tot + d00 + d01 + d10 + d11
    return (h + d10) / z, (d + d00 + d11) / z, (a + d01) / z


@lru_cache(maxsize=8192)
def fit(p_home: float, p_draw: float, p_away: float) -> tuple[float, float]:
    """Expected goals (home, away) whose model 1X2 matches the given probabilities."""

    def err(lh: float, la: float) -> float:
        h, d, a = _hda(lh, la)
        return (h - p_home) ** 2 + (d - p_draw) ** 2 + (a - p_away) ** 2

    lh, la = 1.4, 1.1
    best = err(lh, la)
    step = 0.5
    moves = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1))
    for _ in range(400):
        if step < 1e-3:
            break
        improved = False
        for dh, da in moves:
            nh, na = lh + dh * step, la + da * step
            if not (0.05 <= nh <= 6 and 0.05 <= na <= 6):
                continue
            v = err(nh, na)
            if v < best - 1e-12:
                lh, la, best, improved = nh, na, v, True
        if not improved:
            step /= 2
    return lh, la


def _grid(lh: float, la: float) -> list[list[float]]:
    ph, pa = _pmf(lh), _pmf(la)
    g = [[ph[i] * pa[j] for j in range(MAXG + 1)] for i in range(MAXG + 1)]
    g[0][0] *= 1 - lh * la * RHO
    g[0][1] *= 1 + lh * RHO
    g[1][0] *= 1 + la * RHO
    g[1][1] *= 1 - RHO
    z = sum(map(sum, g))
    return [[c / z for c in row] for row in g]


def _goal_markets(lh: float, la: float) -> tuple[dict, float]:
    """({line: P(total goals > line)}, P(both teams score))."""
    totals = [0.0] * (2 * MAXG + 1)
    btts = 0.0
    for i, row in enumerate(_grid(lh, la)):
        for j, c in enumerate(row):
            totals[i + j] += c
            if i and j:
                btts += c
    over = {line: sum(totals[int(line) + 1:]) for line in (0.5, 1.5, 2.5, 3.5)}
    return over, btts


# ------------------------------------------------------------------ selections
def _est_odds(prob: float) -> float:
    return max(1.01, round(1 / (prob * (1 + config.MARKET_MARGIN)), 2))


def selections(odds: dict) -> dict:
    """All usable selections for a match, from its 1X2 odds.

    Returns {key: {"label", "odds", "prob", "real"}}, keeping only selections that are
    likely enough (CAND_MIN_PROB) and pay enough (LEG_MIN_ODDS) to be accumulator legs."""
    inv = {k: 1 / v for k, v in odds.items()}
    s = sum(inv.values())
    ph, pd, pa = (inv[k] / s for k in ("home", "draw", "away"))

    lh, la = fit(round(ph, 4), round(pd, 4), round(pa, 4))
    over, btts = _goal_markets(lh, la)

    table = {
        "home": (ph, odds["home"]),
        "draw": (pd, odds["draw"]),
        "away": (pa, odds["away"]),
        "dc_1x": (ph + pd, None),
        "dc_x2": (pd + pa, None),
        "dc_12": (ph + pa, None),
        "dnb_home": (ph / (ph + pa), None),
        "dnb_away": (pa / (ph + pa), None),
        "btts_yes": (btts, None),
        "btts_no": (1 - btts, None),
    }
    for line, p in over.items():
        table[f"over_{line}"] = (p, None)
        table[f"under_{line}"] = (1 - p, None)

    out = {}
    for key, (prob, real_odds) in table.items():
        o = real_odds if real_odds is not None else _est_odds(prob)
        if prob >= config.CAND_MIN_PROB and o >= config.LEG_MIN_ODDS:
            out[key] = {"label": LABELS[key], "odds": o, "prob": round(prob, 3), "real": is_real(key)}
    return out


# ------------------------------------------------------------------ settlement
def evaluate(key: str, hg: int, ag: int) -> str:
    """'won', 'lost' or 'void' for a selection given the full-time score."""
    total = hg + ag
    if key == "home":
        ok = hg > ag
    elif key == "draw":
        ok = hg == ag
    elif key == "away":
        ok = hg < ag
    elif key == "dc_1x":
        ok = hg >= ag
    elif key == "dc_x2":
        ok = ag >= hg
    elif key == "dc_12":
        ok = hg != ag
    elif key in ("dnb_home", "dnb_away"):
        if hg == ag:
            return "void"
        ok = hg > ag if key == "dnb_home" else ag > hg
    elif key == "btts_yes":
        ok = hg > 0 and ag > 0
    elif key == "btts_no":
        ok = not (hg > 0 and ag > 0)
    elif key.startswith(("over_", "under_")):
        side, line = key.split("_", 1)
        ok = total > float(line) if side == "over" else total < float(line)
    else:
        return "void"
    return "won" if ok else "lost"
