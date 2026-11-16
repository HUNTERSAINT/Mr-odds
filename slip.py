"""Build the accumulator whose combined odds land closest to the user's target.

Each match offers up to 3 candidate selections (different markets); a slip uses at most one
selection per match. The search prefers high confidence, then closeness to the target."""
import math
import time
from itertools import combinations, product

import config

TOLERANCES = (0.12, 0.25)  # accept combined odds within +/-12%, then +/-25% of the target
MAX_LEGS = 6
POOL = 12  # matches considered (those with the most confident options)
PER_MATCH = 3
TIME_BUDGET = 2.5  # seconds


def build_slip(matches: list[dict], cands: dict, target: float):
    """Returns (legs, total_odds) or None. legs = [{"match", "cand", "odds"}, ...]."""
    by_match = []
    for m in matches:
        cs = [c for c in cands.get(m["id"], []) if c["odds"] >= config.LEG_MIN_ODDS][:PER_MATCH]
        if cs:
            by_match.append((m, cs))
    by_match.sort(key=lambda x: -max(c["confidence"] for c in x[1]))
    pool = by_match[:POOL]
    deadline = time.monotonic() + TIME_BUDGET

    def search(tol: float):
        lo, hi = target * (1 - tol), target * (1 + tol)
        best, best_score = None, -1e9
        for n in range(1, MAX_LEGS + 1):
            for combo in combinations(pool, n):
                if time.monotonic() > deadline:
                    return best
                lowest = math.prod(min(c["odds"] for c in cs) for _, cs in combo)
                highest = math.prod(max(c["odds"] for c in cs) for _, cs in combo)
                if highest < lo or lowest > hi:
                    continue
                for choice in product(*(cs for _, cs in combo)):
                    total = math.prod(c["odds"] for c in choice)
                    if not lo <= total <= hi:
                        continue
                    conf = sum(math.log(c["confidence"] / 100) for c in choice)
                    score = conf - abs(total - target) / target - 0.05 * n
                    if score > best_score:
                        best, best_score = (combo, choice, total), score
        return best

    for tol in TOLERANCES:
        found = search(tol)
        if found:
            combo, choice, total = found
            legs = [
                {"match": m, "cand": c, "odds": c["odds"]} for (m, _), c in zip(combo, choice)
            ]
            legs.sort(key=lambda l: l["match"]["kickoff"])
            return legs, total
    return None
