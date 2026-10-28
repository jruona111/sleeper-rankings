"""Per-team "storyline facts": the only material blurbs may draw on."""
from __future__ import annotations

from .extras import describe_mistake, team_moves
from .league import Season
from .metrics import TeamWeek


def _record(m: dict) -> str:
    rec = f"{m['wins']}-{m['losses']}"
    return rec + (f"-{m['ties']}" if m["ties"] else "")


def build_facts(
    season: Season,
    weeks: dict[int, dict[int, TeamWeek]],
    rankings: dict[int, dict],
    week: int,
    players: dict,
) -> dict[int, dict]:
    ranked = rankings[week]
    metrics = ranked["metrics"]
    prev_rows = {r["roster_id"]: r for r in rankings[week - 1]["rows"]} if week - 1 in rankings else {}
    tw = weeks.get(week, {})
    scores = sorted((t.pf for t in tw.values()), reverse=True)
    out = {}
    for row in ranked["rows"]:
        rid = row["roster_id"]
        m = metrics[rid]
        team = season.teams[rid]
        t = tw.get(rid)
        facts: dict = {
            "team": team.team_name,
            "manager": team.owner_name,
            "week": week,
            "rank": row["rank"],
            "previous_rank": row["prev_rank"],
            "rank_change": row["change"],
            "movement": "up" if row["change"] > 0 else "down" if row["change"] < 0 else (
                "new" if row["prev_rank"] is None else "steady"),
            "power_score": row["score"],
            "record": _record(m),
            "season_points_per_game": round(m["pf"], 2),
            "luck_wins": round(m["luck"], 2),
            "all_play_record": f"{m['allplay_w']}-{m['allplay_l']}" + (f"-{m['allplay_t']}" if m["allplay_t"] else ""),
            "streak": m["streak"],
        }
        if row["driver"]:
            facts["biggest_ranking_factor"] = {
                "metric": row["driver"]["label"],
                "effect": "helped" if row["driver"]["delta"] > 0 else "hurt",
            }
        last3 = m["last3"]
        if len(last3) >= 2:
            avg3 = sum(last3) / len(last3)
            facts["last_3_scores"] = last3
            facts["last_3_trend"] = (
                "rising" if last3[-1] > last3[0] * 1.05 else "falling" if last3[-1] < last3[0] * 0.95 else "flat")
            facts["last_3_vs_season_avg"] = round(avg3 - m["pf"], 2)
        if t:
            this_week = {
                "points": round(t.pf, 2),
                "max_points": round(t.max_pf, 2),
                "bench_points_left": t.bench_pts,
                "weekly_score_rank": scores.index(t.pf) + 1,
                "teams": len(scores),
            }
            if t.opponent is not None:
                opp = season.teams[t.opponent]
                this_week.update({
                    "result": {"W": "win", "L": "loss", "T": "tie"}[t.result],
                    "opponent": opp.team_name,
                    "opponent_points": round(t.pa, 2),
                    "margin": round(t.pf - t.pa, 2),
                })
                if t.opponent in prev_rows:
                    this_week["opponent_rank_entering_week"] = prev_rows[t.opponent]["rank"]
            elif not t.playoff:
                this_week["result"] = "bye"
            if t.median_result:
                this_week["vs_league_median"] = {"W": "above", "L": "below", "T": "equal"}[t.median_result]
            if not t.playoff:
                this_week["would_have_beaten"] = f"{t.allplay_w} of {t.allplay_w + t.allplay_l + t.allplay_t} teams"
            if t.playoff:
                this_week["playoff_week"] = True
            if t.mistake and t.mistake["gain"] >= 5:
                mk = describe_mistake(t.mistake, players)
                this_week["lineup_mistake"] = (
                    f"started {mk['started_name']} ({mk['started_points']}) over "
                    f"{mk['benched_name']} ({mk['benched_points']})")
            if scores and t.pf == scores[0]:
                this_week["league_high_score"] = True
            if scores and t.pf == scores[-1]:
                this_week["league_low_score"] = True
            facts["this_week"] = this_week
        moves = team_moves(season, rid, week, players)
        if moves:
            facts["transactions_this_week"] = moves[:4]
        if m["sos"]:
            facts["opponents_avg_points_per_game"] = round(m["sos"], 2)
        out[rid] = facts
    return out
