"""Build the accumulator whose combined odds land closest to the user's target.

Each match offers a few candidate selections (different markets); a slip uses at most one
selection per match. We want the slip with the highest joint probability whose total odds
are close to the target. That is a knapsack problem, solved exactly by dynamic programming
over ln(odds) (odds multiply, so their logs add), which stays fast for 100x+ targets with
dozens of matches, where trying every combination would be impossible."""
import math

import config

BIN = 0.002  # resolution of the search in ln(odds): 0.2% per step
TOLERANCES = (0.03, 0.08, 0.20)  # accept totals within 3%, then 8%, then 20% of the target
PER_MATCH = 4  # candidate selections considered per match
DEV_WEIGHT = 2.0  # how strongly to prefer totals closer to the target
# Extra cost per leg, raised step by step if the best slip has more legs than MAX_LEGS.
LEG_PENALTIES = (0.0, 0.15, 0.4, 0.8, 1.5, 3.0)
NEG = -1e18


def _items(matches: list[dict], cands: dict) -> list[tuple[dict, list[dict]]]:
    items = []
    for m in matches:
        cs = [c for c in cands.get(m["id"], []) if c["odds"] >= config.LEG_MIN_ODDS][:PER_MATCH]
        if cs:
            items.append((m, cs))
    return items


def _dp(items, nb: int, kappa: float):
    """best[b] = highest sum of log-confidence reaching total ln(odds) = b * BIN.
    choices[i][b] = which candidate of match i was taken to reach b (-1 = match skipped)."""
    best = [NEG] * (nb + 1)
    best[0] = 0.0
    choices = []
    for _, cs in items:
        new = best[:]
        ch = [-1] * (nb + 1)
        for ci, c in enumerate(cs):
            w = round(math.log(c["odds"]) / BIN)
            if w <= 0:
                continue
            gain = math.log(c["confidence"] / 100) - kappa
            for b in range(nb + 1 - w):
                v = best[b]
                if v > NEG / 2:
                    nv = v + gain
                    if nv > new[b + w]:
                        new[b + w] = nv
                        ch[b + w] = ci
        best = new
        choices.append(ch)
    return best, choices


def _extract(items, choices, b: int):
    picked = []
    for i in range(len(items) - 1, -1, -1):
        ci = choices[i][b]
        if ci >= 0:
            c = items[i][1][ci]
            picked.append((items[i][0], c))
            b -= round(math.log(c["odds"]) / BIN)
    picked.reverse()
    return picked


def build_slip(matches: list[dict], cands: dict, target: float):
    """Returns (legs, total_odds) or None. legs = [{"match", "cand", "odds"}, ...]."""
    items = _items(matches, cands)
    if not items:
        return None
    t_ln = math.log(target)
    tb = round(t_ln / BIN)
    nb = int(math.log(target * 1.25) / BIN) + 2

    for kappa in LEG_PENALTIES:
        best, choices = _dp(items, nb, kappa)
        for tol in TOLERANCES:
            lo = max(1, math.floor(math.log(target * (1 - tol)) / BIN))
            hi = min(nb, math.ceil(math.log(target * (1 + tol)) / BIN))
            window = [b for b in range(lo, hi + 1) if best[b] > NEG / 2]
            if not window:
                continue
            b = max(window, key=lambda x: best[x] - DEV_WEIGHT * abs(x - tb) * BIN)
            picked = _extract(items, choices, b)
            if not picked or len(picked) > config.MAX_LEGS:
                continue
            total = math.prod(c["odds"] for _, c in picked)
            if abs(total / target - 1) > tol + 0.02:  # rounding safety net
                continue
            legs = [{"match": m, "cand": c, "odds": c["odds"]} for m, c in picked]
            legs.sort(key=lambda l: l["match"]["kickoff"])
            return legs, total
    return None


def max_reachable(matches: list[dict], cands: dict) -> float:
    """Highest total odds possible from these matches (best option per match, MAX_LEGS legs)."""
    top = sorted(
        (max(c["odds"] for c in cs) for _, cs in _items(matches, cands)), reverse=True
    )
    return math.prod(top[: config.MAX_LEGS]) if top else 0.0
