"""Per-team weekly and season-to-date metrics.

Conventions
- PF-based metrics (PF, Max PF, efficiency, form, consistency) use every
  completed week the team played, playoff weeks included.
- Records, win %, all-play and luck use regular-season weeks only.
- Median leagues: each regular-season week adds a game vs. the league median.
- Ties count as half a win. A team with no opponent (bye) gets no H2H result.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .league import Season, pairings
from .lineup import biggest_mistake, optimal_lineup


@dataclass
class TeamWeek:
    week: int
    roster_id: int
    pf: float
    max_pf: float
    playoff: bool
    opponent: int | None = None
    pa: float | None = None
    result: str | None = None          # W / L / T vs. opponent
    median_result: str | None = None   # W / L / T vs. league median
    allplay_w: int = 0
    allplay_l: int = 0
    allplay_t: int = 0
    mistake: dict | None = None
    starters: list = field(default_factory=list)
    players_points: dict = field(default_factory=dict)
    players: list = field(default_factory=list)

    @property
    def bench_pts(self) -> float:
        return round(max(0.0, self.max_pf - self.pf), 2)

    @property
    def efficiency(self) -> float:
        return self.pf / self.max_pf if self.max_pf > 0 else 1.0

    def to_json(self) -> dict:
        return {
            "week": self.week,
            "pf": round(self.pf, 2),
            "max_pf": round(self.max_pf, 2),
            "bench_pts": self.bench_pts,
            "efficiency": round(self.efficiency, 4),
            "opponent": self.opponent,
            "pa": None if self.pa is None else round(self.pa, 2),
            "result": self.result,
            "median_result": self.median_result,
            "allplay": [self.allplay_w, self.allplay_l, self.allplay_t],
            "playoff": self.playoff,
            "mistake": self.mistake,
        }


def _cmp(a: float, b: float) -> str:
    if abs(a - b) < 1e-9:
        return "T"
    return "W" if a > b else "L"


def compute_weeks(season: Season, players: dict) -> dict[int, dict[int, TeamWeek]]:
    """{week: {roster_id: TeamWeek}} for every completed week."""
    out: dict[int, dict[int, TeamWeek]] = {}
    positions = season.roster_positions
    for week in season.completed_weeks:
        rows = season.matchups[week]
        playoff = season.is_playoff(week)
        tw: dict[int, TeamWeek] = {}
        for m in rows:
            rid = int(m["roster_id"])
            if rid not in season.teams:
                continue
            pts = dict(m.get("players_points") or {})
            pool = list(m.get("players") or pts.keys())
            starters = list(m.get("starters") or [])
            pf = float(m.get("custom_points") if m.get("custom_points") is not None else (m.get("points") or 0.0))
            max_pf, _ = optimal_lineup(positions, pts, players, pool)
            tw[rid] = TeamWeek(
                week=week,
                roster_id=rid,
                pf=pf,
                max_pf=max(max_pf, pf),
                playoff=playoff,
                mistake=biggest_mistake(positions, starters, pts, players, pool),
                starters=starters,
                players_points=pts,
                players=pool,
            )
        for a, b in pairings(rows):
            if a in tw and b in tw:
                tw[a].opponent, tw[a].pa = b, tw[b].pf
                tw[b].opponent, tw[b].pa = a, tw[a].pf
                tw[a].result = _cmp(tw[a].pf, tw[b].pf)
                tw[b].result = _cmp(tw[b].pf, tw[a].pf)
        if not playoff:
            scores = [t.pf for t in tw.values()]
            median = statistics.median(scores) if scores else 0.0
            for t in tw.values():
                if season.median:
                    t.median_result = _cmp(t.pf, median)
                for other in tw.values():
                    if other is t:
                        continue
                    r = _cmp(t.pf, other.pf)
                    t.allplay_w += r == "W"
                    t.allplay_l += r == "L"
                    t.allplay_t += r == "T"
        out[week] = tw
    return out


def decayed_mean(values: list[float], decay: float) -> float:
    """Weighted mean with weight decay**weeks_ago (last value = 0 weeks ago)."""
    if not values:
        return 0.0
    n = len(values)
    weights = [decay ** (n - 1 - i) for i in range(n)]
    return sum(v * w for v, w in zip(values, weights)) / sum(weights)


def streak(results: list[str]) -> str:
    if not results:
        return ""
    last, count = results[-1], 0
    for r in reversed(results):
        if r != last:
            break
        count += 1
    return f"{last}{count}"


def season_metrics(
    season: Season,
    weeks: dict[int, dict[int, TeamWeek]],
    through: int,
    decay: float,
) -> dict[int, dict]:
    """Season-to-date metrics for every team using weeks <= through."""
    played = [w for w in sorted(weeks) if w <= through]
    per_team: dict[int, list[TeamWeek]] = {rid: [] for rid in season.teams}
    for w in played:
        for rid, t in weeks[w].items():
            per_team.setdefault(rid, []).append(t)

    pf_pg = {rid: (sum(t.pf for t in ts) / len(ts) if ts else 0.0) for rid, ts in per_team.items()}
    out: dict[int, dict] = {}
    for rid, ts in per_team.items():
        reg = [t for t in ts if not t.playoff]
        wins = losses = ties = 0
        expected = 0.0
        ap_w = ap_l = ap_t = 0
        for t in reg:
            for r in (t.result, t.median_result):
                wins += r == "W"
                losses += r == "L"
                ties += r == "T"
            others = t.allplay_w + t.allplay_l + t.allplay_t
            if t.result is not None and others:
                expected += (t.allplay_w + 0.5 * t.allplay_t) / others
            if t.median_result is not None:
                # A median game has no luck: the result is the expectation.
                expected += {"W": 1.0, "T": 0.5, "L": 0.0}[t.median_result]
            ap_w, ap_l, ap_t = ap_w + t.allplay_w, ap_l + t.allplay_l, ap_t + t.allplay_t
        games = wins + losses + ties
        pf_total = sum(t.pf for t in ts)
        max_total = sum(t.max_pf for t in ts)
        scores = [t.pf for t in ts]
        opps = [t.opponent for t in ts if t.opponent is not None]
        h2h = [t.result for t in reg if t.result]
        mistakes = [dict(m, week=t.week) for t in ts if (m := t.mistake)]
        ap_games = ap_w + ap_l + ap_t
        out[rid] = {
            "games": len(ts),
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "win_pct": (wins + 0.5 * ties) / games if games else 0.0,
            "pf": pf_pg[rid],
            "pf_total": pf_total,
            "pa_total": sum(t.pa for t in ts if t.pa is not None),
            "pa": (sum(t.pa for t in ts if t.pa is not None) / len(opps)) if opps else 0.0,
            "max_pf": max_total / len(ts) if ts else 0.0,
            "max_pf_total": max_total,
            "efficiency": pf_total / max_total if max_total else 0.0,
            "recent_form": decayed_mean(scores, decay),
            "consistency": statistics.stdev(scores) if len(scores) > 1 else 0.0,
            "allplay_w": ap_w,
            "allplay_l": ap_l,
            "allplay_t": ap_t,
            "allplay_pct": (ap_w + 0.5 * ap_t) / ap_games if ap_games else 0.0,
            "expected_wins": expected,
            "luck": (wins + 0.5 * ties) - expected,
            "sos": statistics.mean(pf_pg[o] for o in opps) if opps else 0.0,
            "bench_total": sum(t.bench_pts for t in ts),
            "bench_pg": sum(t.bench_pts for t in ts) / len(ts) if ts else 0.0,
            "worst_mistake": max(mistakes, key=lambda m: m["gain"]) if mistakes else None,
            "high": max(scores) if scores else 0.0,
            "low": min(scores) if scores else 0.0,
            "streak": streak(h2h),
            "last3": [round(s, 2) for s in scores[-3:]],
        }
    return out


def remaining_sos(season: Season, metrics: dict[int, dict]) -> dict[int, float | None]:
    """Average PF/game of each team's remaining regular-season opponents."""
    opps: dict[int, list[int]] = {rid: [] for rid in season.teams}
    for pairs in season.future.values():
        for a, b in pairs:
            opps.setdefault(a, []).append(b)
            opps.setdefault(b, []).append(a)
    return {
        rid: (statistics.mean(metrics[o]["pf"] for o in os_ if o in metrics) if os_ else None)
        for rid, os_ in opps.items()
    }
