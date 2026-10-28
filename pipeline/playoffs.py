"""Monte Carlo playoff odds.

Each team's weekly score is drawn from Normal(mean, sd) of its season scoring.
With very few games the estimates are noisy, so both are blended with the
league-wide values using SHRINK pseudo-games of league-average data.

Seeding follows Sleeper's defaults: division winners first (when the league
has divisions), then by wins (ties = half), then by points for. Brackets use
fixed seeding unless the league re-seeds (settings.playoff_seed_type == 1).
"""
from __future__ import annotations

import math
import random
import statistics

from .league import Season
from .metrics import TeamWeek

SHRINK = 2


def score_model(season: Season, weeks: dict[int, dict[int, TeamWeek]]) -> dict[int, tuple[float, float]]:
    scores: dict[int, list[float]] = {rid: [] for rid in season.teams}
    for tw in weeks.values():
        for rid, t in tw.items():
            scores.setdefault(rid, []).append(t.pf)
    everything = [s for ss in scores.values() for s in ss]
    if not everything:
        return {rid: (100.0, 20.0) for rid in scores}
    lg_mean = statistics.mean(everything)
    lg_sd = statistics.pstdev(everything) or 20.0
    model = {}
    for rid, ss in scores.items():
        n = len(ss)
        mean = (sum(ss) + SHRINK * lg_mean) / (n + SHRINK)
        var = ((statistics.pvariance(ss) if n > 1 else 0.0) * n + SHRINK * lg_sd ** 2) / (n + SHRINK)
        model[rid] = (mean, math.sqrt(var))
    return model


def bracket_order(size: int) -> list[int]:
    order = [1]
    while len(order) < size:
        n = len(order) * 2
        order = [x for s in order for x in (s, n + 1 - s)]
    return order


def seed_teams(season: Season, wins: dict[int, float], pf: dict[int, float]) -> list[int]:
    key = lambda rid: (-wins[rid], -pf[rid], rid)
    divisions: dict[int, list[int]] = {}
    for rid, team in season.teams.items():
        if team.division:
            divisions.setdefault(team.division, []).append(rid)
    if int(season.settings.get("divisions") or 0) > 1 and len(divisions) > 1:
        leaders = sorted((min(ids, key=key) for ids in divisions.values()), key=key)
        rest = sorted((r for r in season.teams if r not in leaders), key=key)
        return leaders + rest
    return sorted(season.teams, key=key)


def play(a: int, b: int, model, rng: random.Random) -> int:
    sa = rng.gauss(*model[a])
    sb = rng.gauss(*model[b])
    return a if sa >= sb else b


def sim_bracket(seeds: list[int], n_playoff: int, model, rng: random.Random, reseed: bool) -> int:
    if n_playoff < 2:
        return seeds[0]
    size = 2 ** math.ceil(math.log2(n_playoff))
    seed_of = {rid: i + 1 for i, rid in enumerate(seeds[:n_playoff])}
    alive: list[int | None] = [seeds[s - 1] if s <= n_playoff else None for s in bracket_order(size)]
    while len(alive) > 1:
        if reseed and len(alive) < size:
            ranked = sorted([t for t in alive if t is not None], key=seed_of.get)
            alive = [x for i in range(len(ranked) // 2) for x in (ranked[i], ranked[-1 - i])]
        nxt = []
        for a, b in zip(alive[::2], alive[1::2]):
            if a is None or b is None:
                nxt.append(a if b is None else b)
            else:
                nxt.append(play(a, b, model, rng))
        alive = nxt
    return alive[0]


def sim_sleeper_bracket(bracket: list[dict], model, rng: random.Random) -> int | None:
    """Play out the remaining games of Sleeper's winners_bracket."""
    won: dict[int, int] = {}
    lost: dict[int, int] = {}
    champion = None
    for match in sorted(bracket, key=lambda m: (m.get("r", 0), m.get("m", 0))):
        teams = []
        for side in ("t1", "t2"):
            team = match.get(side)
            src = match.get(f"{side}_from") or {}
            if team is None and "w" in src:
                team = won.get(src["w"])
            if team is None and "l" in src:
                team = lost.get(src["l"])
            teams.append(team)
        a, b = teams
        if a is None or b is None:
            continue
        winner = match.get("w") or play(a, b, model, rng)
        won[match["m"]], lost[match["m"]] = winner, (b if winner == a else a)
        if match.get("p") == 1:
            champion = winner
    return champion


def simulate(
    season: Season,
    weeks: dict[int, dict[int, TeamWeek]],
    metrics: dict[int, dict],
    n_sims: int,
    seed: int | None = None,
) -> dict:
    rng = random.Random(seed)
    model = score_model(season, weeks)
    teams = list(season.teams)
    n_playoff = min(season.playoff_teams or 0, len(teams))
    size = 2 ** math.ceil(math.log2(n_playoff)) if n_playoff > 1 else 1
    n_byes = size - n_playoff if n_playoff > 1 else 0
    reseed = int(season.settings.get("playoff_seed_type") or 0) == 1
    base_wins = {r: metrics[r]["wins"] + 0.5 * metrics[r]["ties"] for r in teams}
    base_pf = {r: metrics[r]["pf_total"] for r in teams}
    future = sorted(season.future.items())
    in_playoffs = season.last_completed >= season.playoff_week_start
    status = "complete" if season.league.get("status") == "complete" else (
        "playoffs" if in_playoffs else "regular_season")

    counts = {r: {"playoff": 0, "bye": 0, "first": 0, "champ": 0, "wins": 0.0,
                  "seeds": [0] * len(teams)} for r in teams}
    sims = n_sims if status != "complete" else 1
    bracket_teams = {t for m in season.bracket for t in (m.get("t1"), m.get("t2")) if t}
    round1 = {t for m in season.bracket if m.get("r") == 1 for t in (m.get("t1"), m.get("t2")) if t}

    for _ in range(sims):
        wins, pf = dict(base_wins), dict(base_pf)
        if not in_playoffs:
            for _week, pairs in future:
                scores = {r: rng.gauss(*model[r]) for r in teams}
                for r, s in scores.items():
                    pf[r] += s
                for a, b in pairs:
                    if a in scores and b in scores:
                        wins[a if scores[a] > scores[b] else b] += 1
                if season.median:
                    med = statistics.median(scores.values())
                    for r, s in scores.items():
                        wins[r] += 1 if s > med else 0
        seeds = seed_teams(season, wins, pf)
        for i, r in enumerate(seeds):
            counts[r]["seeds"][i] += 1
            counts[r]["wins"] += wins[r]
        if season.bracket:
            champ = sim_sleeper_bracket(season.bracket, model, rng)
            playoff_set = bracket_teams or set(seeds[:n_playoff])
            bye_set = (bracket_teams - round1) if round1 else set(seeds[:n_byes])
        else:
            champ = sim_bracket(seeds, n_playoff, model, rng, reseed) if n_playoff else None
            playoff_set, bye_set = set(seeds[:n_playoff]), set(seeds[:n_byes])
        for r in playoff_set:
            counts[r]["playoff"] += 1
        for r in bye_set:
            counts[r]["bye"] += 1
        counts[seeds[0]]["first"] += 1
        if champ is not None:
            counts[champ]["champ"] += 1

    rows = []
    for r, c in counts.items():
        rows.append({
            "roster_id": r,
            "playoff_pct": round(100 * c["playoff"] / sims, 1),
            "bye_pct": round(100 * c["bye"] / sims, 1),
            "first_seed_pct": round(100 * c["first"] / sims, 1),
            "champ_pct": round(100 * c["champ"] / sims, 1),
            "proj_wins": round(c["wins"] / sims, 2),
            "avg_seed": round(sum((i + 1) * n for i, n in enumerate(c["seeds"])) / sims, 2),
            "seed_dist": [round(100 * n / sims, 1) for n in c["seeds"]],
            "mean": round(model[r][0], 2),
            "sd": round(model[r][1], 2),
            "record": [metrics[r]["wins"], metrics[r]["losses"], metrics[r]["ties"]],
        })
    rows.sort(key=lambda x: (-x["playoff_pct"], -x["champ_pct"], x["avg_seed"]))
    return {
        "status": status,
        "as_of_week": season.last_completed,
        "simulations": sims,
        "remaining_weeks": [w for w, _ in future] if not in_playoffs else [],
        "playoff_teams": n_playoff,
        "byes": n_byes,
        "median_games": season.median,
        "teams": rows,
    }
