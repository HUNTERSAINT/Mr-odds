"""Build the accumulator whose combined odds land closest to the user's target."""
import math
from itertools import combinations

MIN_LEG_ODDS = 1.15
TOLERANCE = 0.20  # accept combined odds within +/-20% of target
MAX_LEGS = 6
POOL = 14  # top-N most confident legs considered


def build_slip(matches: list[dict], preds: dict, target: float):
    legs = []
    for m in matches:
        p = preds.get(m["id"])
        if not p:
            continue
        odds = m["odds"][p.pick]
        if odds < MIN_LEG_ODDS:
            continue
        legs.append({"match": m, "pred": p, "odds": odds})

    legs.sort(key=lambda l: l["pred"].confidence, reverse=True)
    legs = legs[:POOL]

    best, best_score = None, -1e9
    for n in range(1, MAX_LEGS + 1):
        for combo in combinations(legs, n):
            total = math.prod(l["odds"] for l in combo)
            if abs(total - target) / target > TOLERANCE:
                continue
            # prefer high confidence, then closeness to target, then fewer legs
            conf = sum(math.log(max(l["pred"].confidence, 1) / 100) for l in combo)
            score = conf - abs(total - target) / target - 0.05 * n
            if score > best_score:
                best, best_score = (combo, total), score
    return best  # (legs, total_odds) or None
