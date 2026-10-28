"""Backtest: do week-N power rankings predict week-N+1 results?

For every regular-season week N (current and previous seasons), rank teams
with data through week N, then predict that the higher-ranked team wins each
week N+1 matchup. Ties are skipped. Reports accuracy per weight set.

    python -m pipeline.backtest            # uses config.yaml + cache
    python -m pipeline.backtest --demo     # synthetic league
"""
from __future__ import annotations

import argparse

from .league import Season, pairings
from .metrics import TeamWeek, compute_weeks, season_metrics
from .rankings import rank_week

ALTERNATIVES = {
    "PF only": {"pf": 1.0},
    "Win % only": {"win_pct": 1.0},
    "Max PF only": {"max_pf": 1.0},
    "All-play % only": {"allplay_pct": 1.0},
    "Equal weights": {"pf": 0.2, "max_pf": 0.2, "win_pct": 0.2, "efficiency": 0.2, "recent_form": 0.2},
    "Form heavy": {"pf": 0.2, "max_pf": 0.2, "win_pct": 0.1, "efficiency": 0.1, "recent_form": 0.4},
}


def backtest_season(season: Season, weeks: dict[int, dict[int, TeamWeek]], weights: dict, decay: float) -> tuple[int, int]:
    correct = total = 0
    reg = season.regular_weeks()
    for n, nxt in zip(reg, reg[1:]):
        metrics = season_metrics(season, weeks, n, decay)
        order = {r["roster_id"]: r["rank"] for r in rank_week(metrics, weights)}
        for a, b in pairings(season.matchups[nxt]):
            if a not in weeks[nxt] or b not in weeks[nxt] or a not in order or b not in order:
                continue
            pa, pb = weeks[nxt][a].pf, weeks[nxt][b].pf
            if pa == pb:
                continue
            favorite = a if order[a] < order[b] else b
            total += 1
            correct += favorite == (a if pa > pb else b)
    return correct, total


def run_backtest(seasons: list[tuple[Season, dict]], weights: dict, decay: float) -> dict:
    sets = {"Current weights": weights, **ALTERNATIVES}
    results = []
    for name, w in sets.items():
        correct = total = 0
        by_season = []
        for season, weeks in seasons:
            c, t = backtest_season(season, weeks, w, decay)
            correct, total = correct + c, total + t
            if t:
                by_season.append({"season": season.season, "correct": c, "games": t,
                                  "accuracy": round(c / t, 4)})
        results.append({
            "name": name,
            "weights": w,
            "correct": correct,
            "games": total,
            "accuracy": round(correct / total, 4) if total else None,
            "by_season": by_season,
        })
    return {"seasons": [s.season for s, _ in seasons], "results": results}


def format_report(report: dict) -> str:
    lines = [f"Backtest over seasons: {', '.join(report['seasons'])}",
             f"{'Weight set':<20} {'Accuracy':>9} {'Games':>7}"]
    for r in sorted(report["results"], key=lambda r: -(r["accuracy"] or 0)):
        acc = f"{100 * r['accuracy']:.1f}%" if r["accuracy"] is not None else "n/a"
        lines.append(f"{r['name']:<20} {acc:>9} {r['games']:>7}")
    return "\n".join(lines)


def main() -> None:
    from .run import load_all  # local import avoids a cycle

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", action="store_true", help="use the synthetic demo league")
    args = parser.parse_args()
    cfg, current, history, players, _state, _client = load_all(demo=args.demo)
    seasons = [(s, compute_weeks(s, players)) for s in [current, *history]]
    print(format_report(run_backtest(seasons, cfg.weights, cfg.decay)))


if __name__ == "__main__":
    main()
