"""Future Rankings: 3-year dynasty projections for players and teams.

Per player (see CLAUDE.md for the full formulas):
  baseline   recency-weighted fantasy PPG under league scoring, shrunk toward the
             position's replacement level; young players blend in snap share and
             opportunity volume
  age curve  per-position growth / plateau / decline, compounded per year
  opportunity  depth-chart and snap-trend adjustment, ramped in over the years
Per team: the best projected starting lineup for each year (Hungarian optimizer
from lineup.py) plus a modest value for owned future draft picks.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .extras import player_name
from .league import Season
from .lineup import optimal_lineup, player_positions
from .sleeper import SleeperClient

DEFAULTS: dict = {
    "year_weights": [0.5, 0.3, 0.2],
    "recency_decay": 0.9,
    "prior_games": 2,
    "young_age": 25,
    "young_years_exp": 3,
    "blend": {"points": 0.6, "snaps": 0.2, "opportunity": 0.2},
    "qb_blend": {"points": 0.8, "snaps": 0.2},
    "snap_trend_weight": 0.5,
    "rb_rush_weight": 0.5,
    "opportunity": {
        "behind_aging_starter": 0.15,
        "behind_injured_starter": 0.10,
        "behind_young_starter": -0.10,
        "snap_trend": 0.5,
        "snap_trend_cap": 0.10,
        "max_total": 0.25,
        "year_ramp": [0.5, 1.0, 1.0],
        "rising_threshold": 0.05,
    },
    "age_curves": {
        "RB": {"growth": 0.06, "peak_age": 24, "cliff_age": 27, "decline": 0.15},
        "WR": {"growth": 0.05, "peak_age": 25, "cliff_age": 29, "decline": 0.10},
        "TE": {"growth": 0.07, "peak_age": 26, "cliff_age": 30, "decline": 0.10},
        "QB": {"growth": 0.04, "peak_age": 27, "cliff_age": 33, "decline": 0.07},
    },
    "no_team_factor": 0.25,
    "pick_values": {1: 4.0, 2: 1.5, 3: 0.5},
}

SKILL = {"RB", "WR", "TE"}
INJURED = {"Out", "IR", "PUP", "Doubtful", "Sus"}
REGULAR_GAMES = 4          # games needed to count toward position-level calibration
YOUNG_STARTER_AGE = 26
ENTRENCHED_SNAPS = 0.65
KEY_STARTERS = 5           # top lineup players checked for the aging-core warning


def settings(overrides: dict | None) -> dict:
    """DEFAULTS deep-merged with config.yaml's dynasty section."""
    out = copy.deepcopy(DEFAULTS)
    for key, val in (overrides or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key].update(val)
        else:
            out[key] = val
    out["pick_values"] = {int(k): float(v) for k, v in out["pick_values"].items()}
    out["age_curves"] = {k.upper(): v for k, v in out["age_curves"].items()}
    return out


# -- weekly stats -> games ---------------------------------------------------------

@dataclass
class Game:
    season: int
    week: int
    pts: float
    snap: float | None = None   # offensive snap share 0-1
    targets: float | None = None
    rush_att: float | None = None


def fantasy_points(stats: dict, scoring: dict) -> float | None:
    """League-scoring points from a Sleeper stat line; PPR totals as a fallback."""
    if scoring:
        keys = scoring.keys() & stats.keys()
        if keys:
            return sum(float(scoring[k]) * float(stats[k] or 0) for k in keys)
    for k in ("pts_ppr", "pts_half_ppr", "pts_std"):
        if stats.get(k) is not None:
            return float(stats[k])
    return None


def _played(stats: dict) -> bool:
    return bool(stats.get("gp") or stats.get("off_snp") or stats.get("gms_active"))


def collect_games(
    weekly_stats: list[tuple[int, int, dict | None]],
    scoring: dict,
    rostered_points: dict[tuple[int, int], dict[str, float]],
) -> dict[str, list[Game]]:
    """{player_id: [Game, ...]} in chronological order.

    weekly_stats: [(season, week, stats-by-player or None)]. Points come from the
    league's own matchup data when the player was rostered that week (exact league
    scoring), otherwise from the stat line. If a week's stats are missing, rostered
    players' nonzero matchup points still count as games.
    """
    games: dict[str, list[Game]] = {}
    for season, week, stats in sorted(weekly_stats, key=lambda x: (x[0], x[1])):
        exact = rostered_points.get((season, week), {})
        if not stats:
            for pid, pts in exact.items():
                if pts:
                    games.setdefault(pid, []).append(Game(season, week, float(pts)))
            continue
        for pid, line in stats.items():
            if not isinstance(line, dict) or not _played(line):
                continue
            pts = exact.get(pid)
            if pts is None:
                pts = fantasy_points(line, scoring)
            if pts is None:
                continue
            tm = line.get("tm_off_snp")
            snap = float(line["off_snp"]) / float(tm) if tm and line.get("off_snp") is not None else None
            games.setdefault(pid, []).append(Game(
                season, week, float(pts), snap,
                float(line["rec_tgt"]) if "rec_tgt" in line else (0.0 if snap is not None else None),
                float(line["rush_att"]) if "rush_att" in line else (0.0 if snap is not None else None),
            ))
    return games


# -- player profiles ----------------------------------------------------------------

@dataclass
class Profile:
    player_id: str
    pos: str | None
    games: int = 0
    weight: float = 0.0           # sum of recency weights
    ppg: float = 0.0              # recency-weighted raw PPG
    snap: float | None = None     # recency-weighted snap share
    snap_trend: float = 0.0       # last-3 minus previous-3 snap share
    snap_games: int = 0
    opp: float | None = None      # recency-weighted opportunities per game


def _wmean(values: list[float], weights: list[float]) -> float:
    return sum(v * w for v, w in zip(values, weights)) / sum(weights)


def profile(pid: str, pos: str | None, games: list[Game], cfg: dict) -> Profile:
    p = Profile(pid, pos)
    if not games:
        return p
    n = len(games)
    w = [cfg["recency_decay"] ** (n - 1 - i) for i in range(n)]
    p.games, p.weight = n, sum(w)
    p.ppg = _wmean([g.pts for g in games], w)
    snaps = [(g.snap, wi) for g, wi in zip(games, w) if g.snap is not None]
    if snaps:
        p.snap = _wmean([s for s, _ in snaps], [wi for _, wi in snaps])
        # Trend: last 3 vs. previous 3 games, within the latest season when it has enough games.
        latest = [g.snap for g in games if g.snap is not None and g.season == games[-1].season]
        recent = latest if len(latest) >= 4 else [s for s, _ in snaps]
        p.snap_games = len(recent)
        last, prev = recent[-3:], recent[-6:-3]
        if last and prev:
            p.snap_trend = sum(last) / len(last) - sum(prev) / len(prev)
    rb = cfg["rb_rush_weight"] if pos == "RB" else 0.0
    opps = [((g.targets or 0.0) + rb * (g.rush_att or 0.0), wi) for g, wi in zip(games, w) if g.targets is not None]
    if opps:
        p.opp = _wmean([o for o, _ in opps], [wi for _, wi in opps])
    return p


@dataclass
class Calibration:
    """Position-level conversions, from players with enough games."""
    replacement: dict[str, float] = field(default_factory=dict)  # fringe-starter PPG
    snap_fit: dict[str, tuple[float, float]] = field(default_factory=dict)  # PPG = a + b * snap share
    opp_fit: dict[str, tuple[float, float]] = field(default_factory=dict)   # PPG = a + b * opportunities

    def implied(self, fit: dict, pos: str | None, x: float) -> float | None:
        if pos not in fit:
            return None
        a, b = fit[pos]
        return max(0.0, a + b * x)


def _fit(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Least-squares line; None if x has no spread or the slope isn't positive."""
    if len(xs) < 5:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return (my - b * mx, b) if b > 0 else None


def replacement_rank(pos: str, num_teams: int, roster_positions: list[str]) -> int:
    """The fringe starter: 1.5 x the league's dedicated starting slots at the position."""
    dedicated = sum(1 for slot in roster_positions if slot == pos)
    return max(1, round(num_teams * max(1, dedicated) * 1.5))


def calibrate(profiles: dict[str, Profile], num_teams: int = 12, roster_positions: list[str] | None = None) -> Calibration:
    cal = Calibration()
    by_pos: dict[str, list[Profile]] = {}
    for p in profiles.values():
        if p.pos and p.games >= REGULAR_GAMES:
            by_pos.setdefault(p.pos, []).append(p)
    for pos, ps in by_pos.items():
        ppgs = sorted((p.ppg for p in ps), reverse=True)
        cal.replacement[pos] = ppgs[min(len(ppgs), replacement_rank(pos, num_teams, roster_positions or [])) - 1]
        regulars = [p for p in ps if p.snap is not None and p.snap >= 0.2]
        fit = _fit([p.snap for p in regulars], [p.ppg for p in regulars])
        if fit:
            cal.snap_fit[pos] = fit
        with_opp = [p for p in regulars if p.opp]
        fit = _fit([p.opp for p in with_opp], [p.ppg for p in with_opp])
        if pos in SKILL and fit:
            cal.opp_fit[pos] = fit
    return cal


def is_young(info: dict, cfg: dict) -> bool:
    age, exp = info.get("age"), info.get("years_exp")
    return (age is not None and age <= cfg["young_age"]) or (exp is not None and exp <= cfg["young_years_exp"])


def baseline(p: Profile, info: dict, cal: Calibration, cfg: dict) -> tuple[float, dict]:
    """Baseline projected PPG and its components (all in PPG units)."""
    k = float(cfg["prior_games"])
    repl = cal.replacement.get(p.pos or "", 0.0)
    shrink = lambda raw: (p.weight * raw + k * repl) / (p.weight + k) if p.weight + k > 0 else repl
    comps: dict[str, float] = {"points": shrink(p.ppg) if p.games else repl}
    if is_young(info, cfg) and p.pos in SKILL | {"QB"}:
        blend = cfg["qb_blend"] if p.pos == "QB" else cfg["blend"]
        if p.snap is not None and p.pos in cal.snap_fit and "snaps" in blend:
            adj = min(1.0, max(0.0, p.snap + cfg["snap_trend_weight"] * p.snap_trend))
            comps["snaps"] = shrink(cal.implied(cal.snap_fit, p.pos, adj))
        if p.opp is not None and p.pos in cal.opp_fit and "opportunity" in blend:
            comps["opportunity"] = shrink(cal.implied(cal.opp_fit, p.pos, p.opp))
        weights = {c: float(blend.get(c, 0.0)) for c in comps}
    else:
        weights = {"points": 1.0}
    total_w = sum(weights.values()) or 1.0
    base = sum(comps[c] * weights[c] for c in weights) / total_w
    return base, {c: round(v, 2) for c, v in comps.items()}


# -- age and opportunity ---------------------------------------------------------------

def age_in(info: dict, season: int, ref_season: int) -> float | None:
    """Age on Sept 1 of `season`."""
    bd = str(info.get("birth_date") or "")
    if len(bd) >= 10 and bd[:4].isdigit():
        y, m, d = int(bd[:4]), int(bd[5:7]), int(bd[8:10])
        return season - y - (1 if (m, d) > (9, 1) else 0)
    if info.get("age") is not None:
        return float(info["age"]) + (season - ref_season)
    return None


def age_multiplier(pos: str | None, age_now: float | None, years_ahead: int, curves: dict) -> float:
    """Compounded age-curve factor from age_now to age_now + years_ahead."""
    curve = curves.get(pos or "")
    if not curve or age_now is None:
        return 1.0
    mult = 1.0
    for step in range(1, years_ahead + 1):
        age = age_now + step
        if age <= curve["peak_age"]:
            mult *= 1 + curve["growth"]
        elif age > curve["cliff_age"]:
            mult *= max(0.0, 1 - curve["decline"])
    return mult


def past_cliff(pos: str | None, age: float | None, curves: dict) -> bool:
    curve = curves.get(pos or "")
    return bool(curve) and age is not None and age > curve["cliff_age"]


def depth_index(players: dict) -> dict[tuple[str, str, int], str]:
    """(nfl_team, depth_chart_position, depth_chart_order) -> player_id."""
    out = {}
    for pid, info in players.items():
        team, dpos, order = info.get("team"), info.get("depth_chart_position"), info.get("depth_chart_order")
        if team and dpos and order:
            out.setdefault((team, dpos, int(order)), pid)
    return out


def opportunity_adjustment(
    pid: str, players: dict, profiles: dict[str, Profile], depth: dict, cfg: dict,
) -> tuple[float, list[str]]:
    """Fractional projection adjustment and plain-language reasons."""
    o, curves = cfg["opportunity"], cfg["age_curves"]
    info = players.get(pid) or {}
    adj, reasons = 0.0, []
    team, dpos, order = info.get("team"), info.get("depth_chart_position"), info.get("depth_chart_order")
    if team and dpos and order and int(order) == 2:
        sid = depth.get((team, dpos, 1))
        starter = players.get(sid) or {}
        if sid and sid != pid:
            name, s_age, s_pos = player_name(sid, players), starter.get("age"), starter.get("position")
            injured = starter.get("injury_status") in INJURED
            aging = s_pos in curves and s_age is not None and s_age >= curves[s_pos]["cliff_age"]
            if injured:
                adj += o["behind_injured_starter"]
                reasons.append(f"backup to injured {name} ({starter.get('injury_status')})")
            if aging:
                adj += o["behind_aging_starter"]
                reasons.append(f"backup to aging {name} ({s_age})")
            s_prof = profiles.get(sid)
            if (not injured and not aging and s_age is not None and s_age <= YOUNG_STARTER_AGE
                    and s_prof and s_prof.snap is not None and s_prof.snap >= ENTRENCHED_SNAPS):
                adj += o["behind_young_starter"]
                reasons.append(f"stuck behind {name} ({s_age})")
    p = profiles.get(pid)
    if p and p.snap_games >= 4 and p.snap_trend:
        cap = o["snap_trend_cap"]
        t = max(-cap, min(cap, o["snap_trend"] * p.snap_trend))
        if abs(t) >= 0.01:
            adj += t
            reasons.append(f"snap share {'up' if t > 0 else 'down'} {abs(100 * p.snap_trend):.0f} pts lately")
    cap = o["max_total"]
    return max(-cap, min(cap, adj)), reasons


# -- projections -------------------------------------------------------------------

@dataclass
class Projection:
    player_id: str
    name: str
    pos: str | None
    nfl_team: str | None
    ages: list[float | None]
    young: bool
    games: int
    ppg: float
    base: float
    components: dict
    adj: float
    reasons: list[str]
    proj: list[float]


def project_player(
    pid: str, players: dict, profiles: dict[str, Profile], cal: Calibration, depth: dict,
    years: list[int], ref_season: int, cfg: dict,
) -> Projection:
    info = players.get(pid) or {}
    pos = info.get("position") or (next(iter(player_positions(pid, players)), None))
    prof = profiles.get(pid) or Profile(pid, pos)
    base, comps = baseline(prof, info, cal, cfg)
    adj, reasons = opportunity_adjustment(pid, players, profiles, depth, cfg)
    age_now = age_in(info, ref_season, ref_season)
    ramp = cfg["opportunity"]["year_ramp"]
    team_factor = 1.0 if (info.get("team") or pos == "DEF") else cfg["no_team_factor"]
    proj = []
    for i, year in enumerate(years):
        mult = age_multiplier(pos, age_now, year - ref_season, cfg["age_curves"])
        proj.append(round(max(0.0, base * mult * (1 + adj * ramp[min(i, len(ramp) - 1)]) * team_factor), 2))
    return Projection(
        player_id=pid, name=player_name(pid, players), pos=pos, nfl_team=info.get("team"),
        ages=[age_in(info, y, ref_season) for y in years], young=is_young(info, cfg) and pos in SKILL | {"QB"},
        games=prof.games, ppg=round(prof.ppg, 2), base=round(base, 2), components=comps,
        adj=round(adj, 3), reasons=reasons, proj=proj,
    )


def future_picks(
    roster_ids: list[int], seasons: list[str], rounds: int, traded: list[dict],
) -> dict[int, list[dict]]:
    """Owned future picks per roster: everyone owns their own unless traded."""
    owner = {(s, r, rid): rid for s in seasons for r in range(1, rounds + 1) for rid in roster_ids}
    for t in traded:
        key = (str(t.get("season")), int(t.get("round") or 0), int(t.get("roster_id") or 0))
        if key in owner and t.get("owner_id") is not None:
            owner[key] = int(t["owner_id"])
    out: dict[int, list[dict]] = {rid: [] for rid in roster_ids}
    for (season, rnd, orig), rid in sorted(owner.items()):
        if rid in out:
            out[rid].append({"season": season, "round": rnd, "original": orig})
    return out


def pick_value(picks: list[dict], year: int, values: dict[int, float]) -> float:
    return sum(values.get(p["round"], 0.0) for p in picks if int(p["season"]) <= year)


def team_year(
    roster_positions: list[str], roster: list[str], projections: dict[str, Projection],
    players: dict, year_index: int,
) -> tuple[float, list[tuple[str, str | None]]]:
    """Best projected starting lineup for one year: (points, [(slot, player_id)])."""
    points = {pid: projections[pid].proj[year_index] for pid in roster if pid in projections}
    return optimal_lineup(roster_positions, points, players, pool=list(points))


def _rank(scores: dict[int, float]) -> dict[int, int]:
    order = sorted(scores, key=lambda rid: (-scores[rid], rid))
    return {rid: i for i, rid in enumerate(order, start=1)}


def _player_ref(p: Projection, idx: int, value: float | None = None) -> dict:
    return {"player_id": p.player_id, "name": p.name, "pos": p.pos,
            "age": None if p.ages[idx] is None else int(p.ages[idx]),
            "points": round(p.proj[idx] if value is None else value, 2)}


def build_future(
    rosters: dict[int, list[str]],
    roster_positions: list[str],
    players: dict,
    games: dict[str, list[Game]],
    years: list[int],
    ref_season: int,
    traded_picks: list[dict],
    draft_rounds: int,
    power_ranks: dict[int, int],
    cfg: dict,
) -> dict:
    """Everything the Future Rankings tab needs (future.json)."""
    profiles = {pid: profile(pid, (players.get(pid) or {}).get("position"), g, cfg) for pid, g in games.items()}
    cal = calibrate(profiles, len(rosters), roster_positions)
    depth = depth_index(players)
    rostered = {pid for r in rosters.values() for pid in r if pid and pid != "0"}
    projections = {pid: project_player(pid, players, profiles, cal, depth, years, ref_season, cfg) for pid in rostered}
    picks = future_picks(sorted(rosters), [str(y) for y in years], draft_rounds, traded_picks)
    yw = list(cfg["year_weights"])[: len(years)]
    curves = cfg["age_curves"]

    teams: dict[int, dict] = {}
    for rid, roster in rosters.items():
        roster = [pid for pid in roster if pid in projections]
        per_year = []
        for i, year in enumerate(years):
            pts, lineup = team_year(roster_positions, roster, projections, players, i)
            pv = pick_value(picks[rid], year, cfg["pick_values"])
            starters = sorted((projections[pid] for _, pid in lineup if pid), key=lambda p: -p.proj[i])
            per_year.append({"score": round(pts + pv, 2), "lineup_pts": pts, "picks_pts": round(pv, 2),
                             "starters": [p.player_id for p in starters]})
        teams[rid] = {"roster_id": rid, "years": per_year, "roster": roster}

    for i in range(len(years)):
        ranks = _rank({rid: t["years"][i]["score"] for rid, t in teams.items()})
        for rid, t in teams.items():
            t["years"][i]["rank"] = ranks[rid]
    dyn = {rid: sum(w * t["years"][i]["score"] for i, w in enumerate(yw)) / (sum(yw) or 1)
           for rid, t in teams.items()}
    dyn_ranks = _rank(dyn)

    out_teams = []
    for rid, t in teams.items():
        views = {}
        for i, year in enumerate(years):
            y = t["years"][i]
            starters = [projections[pid] for pid in y["starters"]]
            views[str(year)] = {
                "score": y["score"], "rank": y["rank"], "lineup_pts": y["lineup_pts"], "picks_pts": y["picks_pts"],
                "top": [_player_ref(p, i) for p in starters[:3]],
                "aging": [_player_ref(p, i) for p in starters[:KEY_STARTERS] if past_cliff(p.pos, p.ages[i], curves)],
            }
        # Dynasty view: weighted projection over the years, among anyone who starts in any year.
        weighted = lambda p: sum(w * p.proj[i] for i, w in enumerate(yw)) / (sum(yw) or 1)
        starters_any = {pid for y in t["years"] for pid in y["starters"]}
        core = sorted((projections[pid] for pid in starters_any), key=lambda p: -weighted(p))
        views["dynasty"] = {
            "score": round(dyn[rid], 2), "rank": dyn_ranks[rid],
            "top": [_player_ref(p, 0, weighted(p)) for p in core[:3]],
            "aging": [_player_ref(p, 0) for p in sorted((projections[pid] for pid in t["years"][0]["starters"]),
                                                        key=lambda p: -p.proj[0])[:KEY_STARTERS]
                      if past_cliff(p.pos, p.ages[0], curves)],
        }
        roster_rows = sorted((projections[pid] for pid in t["roster"]), key=lambda p: -weighted(p))
        out_teams.append({
            "roster_id": rid,
            "power_rank": power_ranks.get(rid),
            "views": views,
            "rising": [{"player_id": p.player_id, "name": p.name, "pos": p.pos,
                        "age": None if p.ages[0] is None else int(p.ages[0]), "reasons": p.reasons}
                       for p in roster_rows if p.young and p.adj >= cfg["opportunity"]["rising_threshold"]],
            "picks": picks[rid],
            "players": [{
                "player_id": p.player_id, "name": p.name, "pos": p.pos, "nfl_team": p.nfl_team,
                "age": None if p.ages[0] is None else int(p.ages[0]), "young": p.young, "games": p.games,
                "ppg": p.ppg, "base": p.base, "components": p.components, "adj": p.adj, "reasons": p.reasons,
                "proj": p.proj, "starter": [p.player_id in y["starters"] for y in t["years"]],
            } for p in roster_rows],
        })
    out_teams.sort(key=lambda t: t["views"]["dynasty"]["rank"])
    return {
        "years": years,
        "year_weights": yw,
        "pick_values": {str(k): v for k, v in cfg["pick_values"].items()},
        "age_curves": curves,
        "replacement": {k: round(v, 2) for k, v in cal.replacement.items()},
        "young_age": cfg["young_age"],
        "young_years_exp": cfg["young_years_exp"],
        "teams": out_teams,
    }


# -- data loading ------------------------------------------------------------------

def projection_years(season: Season) -> list[int]:
    start = int(season.season) + (0 if season.league.get("status") in ("pre_draft", "drafting") else 1)
    return [start, start + 1, start + 2]


def load_and_build(
    client: SleeperClient, current: Season, history: list[Season], players: dict,
    power_ranks: dict[int, int], overrides: dict | None, log=print,
) -> dict:
    cfg = settings(overrides)
    season = int(current.season)
    weekly: list[tuple[int, int, dict | None]] = []
    last = current.last_completed
    for week in range(1, 19):  # previous NFL season (stats are league-independent)
        weekly.append((season - 1, week, client.stats(str(season - 1), week, completed=True)))
    for week in current.completed_weeks:
        weekly.append((season, week, client.stats(str(season), week, completed=week < last)))
    have = sum(1 for *_, s in weekly if s)
    log(f"Player stats: {have}/{len(weekly)} weeks available")

    rostered_points: dict[tuple[int, int], dict[str, float]] = {}
    for s in [current] + history[:1]:
        for week, rows in s.matchups.items():
            pts = rostered_points.setdefault((int(s.season), week), {})
            for m in rows:
                pts.update({pid: float(v) for pid, v in (m.get("players_points") or {}).items()})
    games = collect_games(weekly, current.league.get("scoring_settings") or {}, rostered_points)

    rosters = {int(r["roster_id"]): list(r.get("players") or []) for r in client.rosters(current.league)}
    rosters = {rid: r for rid, r in rosters.items() if rid in current.teams}
    years = projection_years(current)
    data = build_future(
        rosters, current.roster_positions, players, games, years, season,
        client.traded_picks(current.league), int(current.settings.get("draft_rounds") or 0),
        power_ranks, cfg,
    )
    data["stats_weeks"] = have
    data["as_of"] = {"season": current.season, "week": last}
    return data
