"""Upcoming games: projected scores and win probability (team pages, teams.json "upcoming").

For each remaining regular-season week, each team's projected score is its best
lineup from Sleeper's weekly player projections (league scoring), using its
current roster minus the taxi squad:
- players on bye have no projection that week (0), so bench players fill in;
- injuries: Out/Doubtful/Questionable scale the next game only, IR/PUP/Suspended
  count 0 for the next `ir_weeks` games;
- a slot the roster can't fill well can use a waiver pickup at the level of the
  `waiver_rank`-th best unrostered player at that position.

The projection is then calibrated on this season's completed weeks, replayed with
the projections Sleeper had at the time: c = league actual / projected, and each
manager's factor = his actual / (c x projected), shrunk toward 1 with
`manager_prior_weeks` pseudo-weeks (it captures start/sit skill and how his roster
beats or trails projections). Expected score = c x factor x projected optimal
lineup; SD = the residual SD (league, shrunk per team). Win probability =
P(Normal difference > 0).
"""
from __future__ import annotations

import copy
import math
import statistics

from .dynasty import fantasy_points
from .extras import player_name
from .league import Season
from .lineup import SLOT_ELIGIBILITY, optimal_lineup, starting_slots
from .sleeper import SleeperClient

DEFAULTS = {
    "waiver_rank": 3,           # fill-in = the Nth best unrostered player at a position that week
    "manager_prior_weeks": 4,   # shrinks each manager's factor (and SD) toward the league
    "ir_weeks": 4,              # IR/PUP/Suspended players count 0 for this many upcoming games
    "injury_next_game": {"Out": 0.0, "Doubtful": 0.25, "Questionable": 0.9},
    "min_sd": 12.0,
}
LONG_TERM = {"IR", "PUP", "Sus", "NA", "COV"}


def settings(overrides: dict | None) -> dict:
    out = copy.deepcopy(DEFAULTS)
    for key, val in (overrides or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key].update(val)
        else:
            out[key] = val
    return out


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def win_prob(mu_a: float, sd_a: float, mu_b: float, sd_b: float) -> float:
    sd = math.hypot(sd_a, sd_b)
    return phi((mu_a - mu_b) / sd) if sd > 0 else (1.0 if mu_a > mu_b else 0.0 if mu_a < mu_b else 0.5)


def projected_points(proj: dict | None, scoring: dict) -> dict[str, float]:
    """{player_id: projected points} for players projected to play (byes are absent)."""
    out = {}
    for pid, line in (proj or {}).items():
        if isinstance(line, dict) and line.get("gp"):
            pts = fantasy_points(line, scoring)
            if pts is not None:
                out[str(pid)] = pts
    return out


def waiver_levels(points: dict[str, float], rostered: set[str], players: dict, rank: int) -> dict[str, float]:
    """Per position, the rank-th best projection among unrostered players."""
    by_pos: dict[str, list[float]] = {}
    for pid, pts in points.items():
        if pid in rostered:
            continue
        pos = (players.get(pid) or {}).get("position")
        if pos:
            by_pos.setdefault(pos, []).append(pts)
    return {pos: sorted(v, reverse=True)[rank - 1] for pos, v in by_pos.items() if len(v) >= rank}


def best_lineup(roster_positions: list[str], pool: list[str], points: dict[str, float], players: dict,
                waiver: dict[str, float]) -> tuple[float, list[tuple[str, str | None]], dict]:
    """Optimal projected lineup; waiver fill-ins enter as pseudo-players "FA:<pos>"."""
    slots = starting_slots(roster_positions)
    pts = {pid: points.get(pid, 0.0) for pid in pool}
    info = dict(players)
    for pos, level in waiver.items():
        # As many fill-ins as there are starting slots that position could take.
        for i in range(sum(1 for s in slots if pos in SLOT_ELIGIBILITY.get(s, {s}))):
            fid = f"FA:{pos}:{i}"
            pts[fid] = level
            info[fid] = {"position": pos, "fantasy_positions": [pos]}
    total, lineup = optimal_lineup(roster_positions, pts, info, list(pts))
    return total, lineup, pts


def injury_factor(info: dict, games_ahead: int, cfg: dict, reserve: bool) -> float:
    status = info.get("injury_status")
    if reserve or status in LONG_TERM:
        return 0.0 if games_ahead < int(cfg["ir_weeks"]) else 1.0
    if games_ahead == 0 and status in cfg["injury_next_game"]:
        return float(cfg["injury_next_game"][status])
    return 1.0


def calibrate(season: Season, proj_by_week: dict[int, dict[str, float]], players: dict, cfg: dict) -> dict:
    """Replay completed weeks with the projections of the time."""
    rows = []  # (roster_id, projected optimal, actual)
    for week in season.completed_weeks:
        if season.is_playoff(week) or week not in proj_by_week:
            continue
        points = proj_by_week[week]
        matchups = season.matchups[week]
        rostered = {str(p) for m in matchups for p in m.get("players") or []}
        waiver = waiver_levels(points, rostered, players, int(cfg["waiver_rank"]))
        for m in matchups:
            pool = [str(p) for p in m.get("players") or []]
            proj, _, _ = best_lineup(season.roster_positions, pool, points, players, waiver)
            actual = float(m.get("points") or 0.0)
            if proj > 0 and actual > 0:
                rows.append((int(m["roster_id"]), proj, actual))
    if len(rows) < 4:
        return {"c": 1.0, "sd": 25.0, "weeks": 0, "teams": {}}
    c = sum(a for _, _, a in rows) / sum(p for _, p, _ in rows)
    resid = [a - c * p for _, p, a in rows]
    sd = statistics.pstdev(resid)
    k = float(cfg["manager_prior_weeks"])
    teams = {}
    for rid in {r for r, _, _ in rows}:
        mine = [(p, a) for r, p, a in rows if r == rid]
        n = len(mine)
        raw = sum(a for _, a in mine) / sum(c * p for p, _ in mine)
        own_sd = statistics.pstdev([a - c * p * raw for p, a in mine]) if n > 1 else sd
        teams[rid] = {"factor": (n * raw + k) / (n + k), "raw": raw, "weeks": n,
                      "sd": math.sqrt((n * own_sd ** 2 + k * sd ** 2) / (n + k))}
    return {"c": c, "sd": sd, "weeks": len({w for w in season.completed_weeks if w in proj_by_week}), "teams": teams}


def fallback_points(season: Season) -> dict[str, float]:
    """Season average when Sleeper projections are unavailable (no bye information)."""
    tot: dict[str, list[float]] = {}
    for week in season.completed_weeks:
        for m in season.matchups[week]:
            for pid, pts in (m.get("players_points") or {}).items():
                tot.setdefault(str(pid), []).append(float(pts or 0.0))
    return {pid: sum(v) / len(v) for pid, v in tot.items()}


def build(season: Season, rosters: list[dict], proj_by_week: dict[int, dict[str, float]], players: dict,
          overrides: dict | None = None) -> dict:
    """{roster_id: {"games": [...], "manager": {...}, "proj_wins", "proj_losses"}} plus "_model"."""
    cfg = settings(overrides)
    cal = calibrate(season, proj_by_week, players, cfg)
    fallback = None
    roster_of = {int(r["roster_id"]): r for r in rosters}
    rostered = {str(p) for r in rosters for p in r.get("players") or []}
    weeks = sorted(w for w in season.future if w > season.last_completed)
    team_week: dict[tuple[int, int], dict] = {}
    for ahead, week in enumerate(weeks):
        points = proj_by_week.get(week)
        source = "sleeper"
        if not points:
            fallback = fallback if fallback is not None else fallback_points(season)
            points, source = fallback, "season_avg"
        waiver = waiver_levels(points, rostered, players, int(cfg["waiver_rank"])) if source == "sleeper" else {}
        playing = {(players.get(pid) or {}).get("team") for pid in points} if source == "sleeper" else None
        for rid, r in roster_of.items():
            taxi = {str(p) for p in r.get("taxi") or []}
            reserve = {str(p) for p in r.get("reserve") or []}
            pool = [str(p) for p in r.get("players") or [] if str(p) not in taxi]
            adj = {pid: points.get(pid, 0.0) * injury_factor(players.get(pid) or {}, ahead, cfg, pid in reserve)
                   for pid in pool}
            proj, lineup, pts = best_lineup(season.roster_positions, pool, adj, players, waiver)
            team = cal["teams"].get(rid, {"factor": 1.0, "sd": cal["sd"], "weeks": 0})
            mu = cal["c"] * team["factor"] * proj
            team_week[(rid, week)] = {
                "proj": proj, "mu": mu, "sd": max(float(cfg["min_sd"]), team["sd"]), "source": source,
                "lineup": [{"slot": s, "player_id": None if (p or "").startswith("FA:") else p,
                            "name": f"Waiver {p.split(':')[1]}" if p and p.startswith("FA:") else player_name(p, players) if p else None,
                            "pts": round(pts.get(p, 0.0), 2) if p else 0.0} for s, p in lineup],
                # Bye = his NFL team has nobody projected this week; otherwise unprojected = inactive/hurt.
                "byes": [pid for pid in pool if playing is not None and pid not in points
                         and (players.get(pid) or {}).get("team") not in playing],
                "hurt": [pid for pid in pool if (pid in points and adj[pid] < points[pid]) or (
                    playing is not None and pid not in points and (players.get(pid) or {}).get("team") in playing)],
            }
    # Starters that matter: a player's projection when he plays, to rank absences.
    star_value: dict[str, float] = {}
    for pts in proj_by_week.values():
        for pid, v in pts.items():
            star_value[pid] = max(star_value.get(pid, 0.0), v)

    out = {}
    for rid in roster_of:
        games, wins = [], 0.0
        for week in weeks:
            opp = next((b if a == rid else a for a, b in season.future.get(week, []) if rid in (a, b)), None)
            mine = team_week.get((rid, week))
            if opp is None or mine is None or (opp, week) not in team_week:
                continue
            theirs = team_week[(opp, week)]
            p = win_prob(mine["mu"], mine["sd"], theirs["mu"], theirs["sd"])
            wins += p

            def notable(ids):
                ranked = sorted(ids, key=lambda x: -star_value.get(x, 0.0))
                return [{"player_id": x, "name": player_name(x, players), "pos": (players.get(x) or {}).get("position"),
                         "status": (players.get(x) or {}).get("injury_status")}
                        for x in ranked if star_value.get(x, 0.0) >= 8.0][:4]

            games.append({
                "week": week, "opponent": opp, "win_prob": round(p, 4),
                "proj": round(mine["mu"], 2), "opp_proj": round(theirs["mu"], 2),
                "sd": round(mine["sd"], 2), "opp_sd": round(theirs["sd"], 2),
                "lineup_proj": round(mine["proj"], 2), "source": mine["source"],
                "byes": notable(mine["byes"]), "hurt": notable(mine["hurt"]),
                "opp_byes": notable(theirs["byes"]), "opp_hurt": notable(theirs["hurt"]),
                "fill_ins": [x["slot"] for x in mine["lineup"] if x["name"] and x["name"].startswith("Waiver")],
                "lineup": mine["lineup"] if week == weeks[0] else None,
            })
        team = cal["teams"].get(rid, {"factor": 1.0, "raw": 1.0, "weeks": 0, "sd": cal["sd"]})
        out[rid] = {"games": games, "proj_wins": round(wins, 2), "proj_losses": round(len(games) - wins, 2),
                    "manager": {"factor": round(team["factor"], 4), "raw": round(team.get("raw", 1.0), 4),
                                "weeks": team["weeks"], "sd": round(team["sd"], 2)}}
    out["_model"] = {"c": round(cal["c"], 4), "sd": round(cal["sd"], 2), "calibration_weeks": cal["weeks"],
                     "waiver_rank": int(cfg["waiver_rank"]), "ir_weeks": int(cfg["ir_weeks"])}
    return out


def load_and_build(client: SleeperClient, season: Season, players: dict, overrides: dict | None = None) -> dict:
    scoring = season.league.get("scoring_settings") or {}
    weeks = [w for w in season.completed_weeks if not season.is_playoff(w)] + \
        sorted(w for w in season.future if w > season.last_completed)
    proj_by_week = {}
    for w in weeks:
        raw = client.projections(season.season, w, completed=w in season.completed_weeks)
        pts = projected_points(raw, scoring)
        if pts:
            proj_by_week[w] = pts
    return build(season, client.rosters(season.league), proj_by_week, players, overrides)
