"""Power rankings: z-score each metric, weight, rescale to 0-100."""
from __future__ import annotations

import math
import statistics

from .league import Season
from .metrics import TeamWeek, season_metrics

METRIC_LABELS = {
    "pf": "points per game",
    "max_pf": "max points per game",
    "win_pct": "win percentage",
    "efficiency": "lineup efficiency",
    "recent_form": "recent form",
}


def zscores(values: dict[int, float]) -> dict[int, float]:
    vals = list(values.values())
    if len(vals) < 2:
        return {k: 0.0 for k in values}
    mean = statistics.mean(vals)
    sd = statistics.pstdev(vals)
    if sd == 0:
        return {k: 0.0 for k in values}
    return {k: (v - mean) / sd for k, v in values.items()}


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def rank_week(metrics: dict[int, dict], weights: dict[str, float]) -> list[dict]:
    """Rank teams from season-to-date metrics. Returns rows sorted by rank."""
    z = {m: zscores({rid: met[m] for rid, met in metrics.items()}) for m in weights}
    total_w = sum(abs(w) for w in weights.values()) or 1.0
    composite = {
        rid: sum(weights[m] * z[m][rid] for m in weights) / total_w for rid in metrics
    }
    # Score: standardize the composite, map through the normal CDF -> 0..100.
    comp_sd = statistics.pstdev(composite.values()) if len(composite) > 1 else 0.0
    rows = []
    for rid, c in composite.items():
        score = 100 * _phi(c / comp_sd) if comp_sd else 50.0
        rows.append({
            "roster_id": rid,
            "composite": c,
            "score": round(score, 1),
            "z": {m: round(z[m][rid], 3) for m in weights},
            "contrib": {m: weights[m] * z[m][rid] / total_w for m in weights},
        })
    rows.sort(key=lambda r: (-r["composite"], -metrics[r["roster_id"]]["pf"], r["roster_id"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows


def all_rankings(
    season: Season,
    weeks: dict[int, dict[int, TeamWeek]],
    weights: dict[str, float],
    decay: float,
) -> dict[int, dict]:
    """{week: {"metrics": {...}, "rows": [...]}} for every completed week."""
    out: dict[int, dict] = {}
    prev: dict[int, dict] | None = None
    for week in sorted(weeks):
        metrics = season_metrics(season, weeks, week, decay)
        rows = rank_week(metrics, weights)
        for r in rows:
            before = prev.get(r["roster_id"]) if prev else None
            r["prev_rank"] = before["rank"] if before else None
            r["change"] = (before["rank"] - r["rank"]) if before else 0
            r["driver"] = movement_driver(r, before)
        prev = {r["roster_id"]: r for r in rows}
        out[week] = {"metrics": metrics, "rows": rows}
    return out


def movement_driver(row: dict, before: dict | None) -> dict | None:
    """The metric whose weighted contribution moved the most, in the direction of travel."""
    if not before:
        return None
    deltas = {m: row["contrib"][m] - before["contrib"][m] for m in row["contrib"]}
    if row["change"] > 0:
        metric = max(deltas, key=deltas.get)
    elif row["change"] < 0:
        metric = min(deltas, key=deltas.get)
    else:
        metric = max(deltas, key=lambda m: abs(deltas[m]))
    return {
        "metric": metric,
        "label": METRIC_LABELS.get(metric, metric),
        "delta": round(deltas[metric], 4),
        "z_before": round(before["z"][metric], 2),
        "z_after": round(row["z"][metric], 2),
    }
