"""Dynasty projections: age curves, opportunity adjustments, projected lineups, picks."""
import pytest

from pipeline import dynasty
from pipeline.dynasty import (
    Calibration, Game, Profile, age_in, age_multiplier, baseline, build_future, collect_games,
    future_picks, opportunity_adjustment, past_cliff, pick_value, profile, settings, team_year,
)

CFG = settings({})
CURVES = CFG["age_curves"]


# -- age curves ------------------------------------------------------------------

def test_age_multiplier_grows_plateaus_then_declines():
    rb = CURVES["RB"]  # growth to 24, flat to 27, then decline
    assert age_multiplier("RB", 22, 1, CURVES) == pytest.approx(1 + rb["growth"])
    assert age_multiplier("RB", 25, 2, CURVES) == pytest.approx(1.0)
    assert age_multiplier("RB", 27, 2, CURVES) == pytest.approx((1 - rb["decline"]) ** 2)
    # Compounds across phases: 23 -> 24 grows, 24 -> 26 flat.
    assert age_multiplier("RB", 23, 3, CURVES) == pytest.approx(1 + rb["growth"])


def test_rbs_decline_first_and_qbs_last():
    at_28 = {pos: age_multiplier(pos, 28, 3, CURVES) for pos in ("RB", "WR", "TE", "QB")}
    assert at_28["RB"] < at_28["WR"] <= at_28["TE"] <= at_28["QB"] == 1.0


def test_age_curve_ignores_unknown_positions_and_ages():
    assert age_multiplier("K", 40, 3, CURVES) == 1.0
    assert age_multiplier("WR", None, 3, CURVES) == 1.0


def test_age_in_uses_birth_date_on_september_first():
    assert age_in({"birth_date": "2000-08-31"}, 2027, 2026) == 27
    assert age_in({"birth_date": "2000-09-02"}, 2027, 2026) == 26
    assert age_in({"age": 24}, 2028, 2026) == 26
    assert age_in({}, 2027, 2026) is None


def test_past_cliff():
    assert past_cliff("RB", 28, CURVES) and not past_cliff("RB", 27, CURVES)
    assert not past_cliff("K", 45, CURVES)


def test_custom_curves_from_config_override_defaults():
    cfg = settings({"age_curves": {"rb": {"growth": 0, "peak_age": 20, "cliff_age": 22, "decline": 0.5}}})
    assert age_multiplier("RB", 22, 1, cfg["age_curves"]) == pytest.approx(0.5)
    assert cfg["age_curves"]["WR"] == CURVES["WR"]  # untouched positions keep defaults


# -- opportunity -----------------------------------------------------------------

def depth_players(**starter):
    base = {"position": "RB", "team": "KC", "depth_chart_position": "RB"}
    return {
        "s": {**base, "full_name": "Starter", "depth_chart_order": 1, "age": 25, **starter},
        "b": {**base, "full_name": "Backup", "depth_chart_order": 2, "age": 22},
        "c": {**base, "full_name": "Third", "depth_chart_order": 3, "age": 22},
    }


def adjust(players, profiles=None, pid="b"):
    return opportunity_adjustment(pid, players, profiles or {}, dynasty.depth_index(players), CFG)


def test_backup_to_aging_starter_gets_a_boost():
    adj, why = adjust(depth_players(age=29))
    assert adj == pytest.approx(CFG["opportunity"]["behind_aging_starter"])
    assert "aging Starter" in why[0]


def test_backup_to_injured_starter_gets_a_boost():
    adj, why = adjust(depth_players(injury_status="IR"))
    assert adj == pytest.approx(CFG["opportunity"]["behind_injured_starter"])


def test_backup_to_young_entrenched_starter_is_penalized():
    profiles = {"s": Profile("s", "RB", games=10, snap=0.8)}
    adj, _ = adjust(depth_players(age=24), profiles)
    assert adj == pytest.approx(CFG["opportunity"]["behind_young_starter"])
    # Not entrenched (low snap share): no penalty.
    adj, _ = adjust(depth_players(age=24), {"s": Profile("s", "RB", games=10, snap=0.5)})
    assert adj == 0


def test_only_the_direct_backup_is_adjusted():
    adj, why = adjust(depth_players(age=30), pid="c")
    assert adj == 0 and why == []


def test_snap_trend_moves_projection_and_is_capped():
    players = {"x": {"position": "WR", "full_name": "X"}}
    up = {"x": Profile("x", "WR", games=8, snap=0.6, snap_trend=0.10, snap_games=8)}
    adj, why = adjust(players, up, pid="x")
    assert adj == pytest.approx(CFG["opportunity"]["snap_trend"] * 0.10) and "up" in why[0]
    down = {"x": Profile("x", "WR", games=8, snap=0.6, snap_trend=-0.9, snap_games=8)}
    adj, _ = adjust(players, down, pid="x")
    assert adj == pytest.approx(-CFG["opportunity"]["snap_trend_cap"])


def test_total_opportunity_adjustment_is_clamped():
    players = depth_players(age=30, injury_status="Out")
    profiles = {"b": Profile("b", "RB", games=8, snap=0.3, snap_trend=0.5, snap_games=8)}
    adj, _ = adjust(players, profiles)
    assert adj == pytest.approx(CFG["opportunity"]["max_total"])


# -- baseline ------------------------------------------------------------------------

def test_profile_weights_recent_games_more():
    games = [Game(2026, w, pts) for w, pts in [(1, 30.0), (2, 10.0)]]
    p = profile("x", "WR", games, CFG)
    d = CFG["recency_decay"]
    assert p.ppg == pytest.approx((30 * d + 10) / (d + 1))


def test_snap_trend_compares_last_three_to_previous_three():
    games = [Game(2026, w, 10.0, snap=s) for w, s in enumerate([0.4, 0.4, 0.4, 0.7, 0.7, 0.7], start=1)]
    assert profile("x", "WR", games, CFG).snap_trend == pytest.approx(0.3)


def test_young_players_blend_points_snaps_and_opportunity():
    cal = Calibration(replacement={"WR": 10.0}, snap_fit={"WR": (0.0, 20.0)}, opp_fit={"WR": (0.0, 2.0)})
    p = Profile("x", "WR", games=50, weight=1e6, ppg=12.0, snap=0.9, opp=8.0)
    young, comps = baseline(p, {"age": 23}, cal, CFG)
    assert comps == {"points": 12.0, "snaps": 18.0, "opportunity": 16.0}
    assert young == pytest.approx(0.6 * 12 + 0.2 * 18 + 0.2 * 16, rel=1e-4)
    veteran, comps = baseline(p, {"age": 29, "years_exp": 7}, cal, CFG)
    assert veteran == pytest.approx(12.0, rel=1e-4) and list(comps) == ["points"]


def test_small_samples_shrink_toward_replacement_level():
    cal = Calibration(replacement={"RB": 8.0})
    one_game = profile("x", "RB", [Game(2026, 1, 30.0)], CFG)
    base, _ = baseline(one_game, {"age": 28, "years_exp": 6}, cal, CFG)
    k = CFG["prior_games"]
    assert base == pytest.approx((30 + k * 8) / (1 + k))
    rookie, _ = baseline(Profile("r", "RB"), {"age": 21, "years_exp": 0}, cal, CFG)
    assert rookie == 8.0


def test_collect_games_prefers_league_points_and_skips_inactive():
    stats = {"a": {"gp": 1, "pts_ppr": 10, "rec": 5, "off_snp": 30, "tm_off_snp": 60, "rec_tgt": 7},
             "b": {"pts_ppr": 3},              # did not play
             "c": {"gp": 1, "rec": 2, "rec_yd": 30}}
    games = collect_games([(2026, 1, stats)], {"rec": 1.0, "rec_yd": 0.1}, {(2026, 1): {"a": 12.5}})
    assert games["a"][0].pts == 12.5 and games["a"][0].snap == 0.5 and games["a"][0].targets == 7
    assert "b" not in games
    assert games["c"][0].pts == pytest.approx(5.0)


def test_missing_stats_week_falls_back_to_rostered_points():
    games = collect_games([(2026, 1, None)], {}, {(2026, 1): {"a": 9.0, "z": 0.0}})
    assert [g.pts for g in games["a"]] == [9.0] and "z" not in games


# -- projected lineups and picks ---------------------------------------------------

POSITIONS = ["QB", "RB", "WR", "FLEX", "BN", "BN"]
LINEUP_PLAYERS = {
    "qb": {"position": "QB", "fantasy_positions": ["QB"]},
    "rb1": {"position": "RB", "fantasy_positions": ["RB"]},
    "rb2": {"position": "RB", "fantasy_positions": ["RB"]},
    "wr1": {"position": "WR", "fantasy_positions": ["WR"]},
    "wr2": {"position": "WR", "fantasy_positions": ["WR"]},
}


def proj(pid, values):
    return dynasty.Projection(pid, pid, LINEUP_PLAYERS[pid]["position"], "KC", [25, 26, 27], False, 10,
                              values[0], values[0], {}, 0.0, [], list(values))


def test_projected_lineup_uses_best_starters_not_roster_sum():
    projections = {"qb": proj("qb", [20, 20, 20]), "rb1": proj("rb1", [15, 10, 5]),
                   "rb2": proj("rb2", [8, 9, 12]), "wr1": proj("wr1", [14, 14, 14]), "wr2": proj("wr2", [3, 13, 3])}
    pts, lineup = team_year(POSITIONS, list(projections), projections, LINEUP_PLAYERS, 0)
    assert pts == 20 + 15 + 14 + 8  # wr2 (3) stays on the bench
    assert {pid for _, pid in lineup} == {"qb", "rb1", "rb2", "wr1"}
    # Year 2: WR2's big year takes the FLEX from RB2.
    pts, lineup = team_year(POSITIONS, list(projections), projections, LINEUP_PLAYERS, 1)
    assert pts == 20 + 14 + 13 + 10
    assert {pid for _, pid in lineup} == {"qb", "rb1", "wr1", "wr2"}
    pts, lineup = team_year(POSITIONS, list(projections), projections, LINEUP_PLAYERS, 2)
    assert pts == 20 + 12 + 14 + 5


def test_future_picks_follow_traded_picks():
    picks = future_picks([1, 2], ["2027", "2028"], 2, [
        {"season": "2027", "round": 1, "roster_id": 1, "owner_id": 2},
        {"season": 2028, "round": 2, "roster_id": 2, "owner_id": 1},
        {"season": "2031", "round": 1, "roster_id": 1, "owner_id": 2},  # outside the window
    ])
    assert len(picks[1]) == 4 - 1 + 1 and len(picks[2]) == 4 + 1 - 1
    assert {"season": "2027", "round": 1, "original": 1} in picks[2]
    assert pick_value(picks[2], 2027, {1: 4.0, 2: 1.5}) == 4.0 * 2 + 1.5
    assert pick_value(picks[2], 2028, {1: 4.0, 2: 1.5}) == 4.0 * 3 + 1.5 * 2 - 1.5


def test_build_future_ranks_by_lineup_plus_picks():
    players = {pid: {**info, "team": "KC", "age": 26} for pid, info in LINEUP_PLAYERS.items()}
    players["qb2"] = {"position": "QB", "fantasy_positions": ["QB"], "age": 24, "team": "KC"}
    games = {pid: [Game(2026, w, pts) for w in range(1, 9)]
             for pid, pts in {"qb": 20, "rb1": 15, "wr1": 14, "qb2": 18, "rb2": 9, "wr2": 9}.items()}
    rosters = {1: ["qb", "rb1", "wr1"], 2: ["qb2", "rb2", "wr2"]}
    out = build_future(rosters, POSITIONS, players, games, [2027, 2028, 2029], 2026, [], 1, {1: 2, 2: 1},
                       settings({"pick_values": {1: 0.0}}))
    first = out["teams"][0]
    assert first["roster_id"] == 1 and first["views"]["dynasty"]["rank"] == 1
    assert first["views"]["2027"]["picks_pts"] == 0
    assert [p["player_id"] for p in first["views"]["2027"]["top"]] == ["qb", "rb1", "wr1"]
    assert first["power_rank"] == 2
