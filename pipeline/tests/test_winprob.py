"""Upcoming-game projections and win probability."""
import pytest

from pipeline.league import Season
from pipeline.winprob import build, injury_factor, settings, win_prob

POSITIONS = ["QB", "RB", "WR", "K", "BN", "BN"]
PLAYERS = {
    "qa": {"position": "QB", "team": "KC"}, "ra": {"position": "RB", "team": "KC"}, "wa": {"position": "WR", "team": "DET"},
    "wa2": {"position": "WR", "team": "KC"}, "ka": {"position": "K", "team": "KC"},
    "qb": {"position": "QB", "team": "BUF"}, "rb": {"position": "RB", "team": "BUF"}, "wb": {"position": "WR", "team": "BUF"},
    "kb": {"position": "K", "team": "BUF"},
    "fa1": {"position": "WR", "team": "MIA"}, "fa2": {"position": "WR", "team": "MIA"}, "fa3": {"position": "WR", "team": "NYJ"},
}
ROSTERS = [{"roster_id": 1, "players": ["qa", "ra", "wa", "wa2", "ka"]}, {"roster_id": 2, "players": ["qb", "rb", "wb", "kb"]}]


def season(completed=(), matchups=None, future=None):
    league = {"league_id": "1", "season": "2026", "roster_positions": POSITIONS, "settings": {"playoff_week_start": 15}}
    return Season(league=league, teams={}, completed_weeks=list(completed), matchups=matchups or {},
                  future=future or {8: [(1, 2)]})


def proj(**kw):
    base = {"qa": 20.0, "ra": 15.0, "wa": 14.0, "wa2": 6.0, "ka": 8.0, "qb": 18.0, "rb": 12.0, "wb": 13.0, "kb": 8.0,
            "fa1": 11.0, "fa2": 10.0, "fa3": 9.0}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def test_win_prob_is_symmetric_and_even_for_equal_teams():
    assert win_prob(120, 25, 120, 25) == pytest.approx(0.5)
    p = win_prob(130, 25, 120, 25)
    assert p == pytest.approx(1 - win_prob(120, 25, 130, 25))
    assert 0.5 < p < 0.7          # a 10-point edge with typical spread is far from a lock


def test_bench_covers_a_starter_on_bye():
    # DET (wa) on bye in week 8: nobody from DET is projected, so the bench WR starts...
    out = build(season(), ROSTERS, {8: proj(wa=None, wa2=10.0)}, PLAYERS)
    g = out[1]["games"][0]
    assert g["lineup_proj"] == pytest.approx(20 + 15 + 10 + 8) and g["fill_ins"] == []
    # ...unless a free agent (3rd best unrostered WR: 9.0) beats him.
    g = build(season(), ROSTERS, {8: proj(wa=None)}, PLAYERS)[1]["games"][0]
    assert g["lineup_proj"] == pytest.approx(20 + 15 + 9 + 8) and g["fill_ins"] == ["WR"]
    assert [b["player_id"] for b in g["byes"]] == []          # wa has no 8+ projection on record
    out = build(season(), ROSTERS, {8: proj(wa=None), 9: proj()}, PLAYERS)
    assert [b["name"] for b in out[1]["games"][0]["byes"]] == ["Player wa"]


def test_waiver_fill_in_when_roster_cannot_cover():
    # Team 2 has one WR; with him out the 3rd-best free agent WR (9.0) fills the slot.
    players = dict(PLAYERS, wb=dict(PLAYERS["wb"], injury_status="Out"))
    g = build(season(), ROSTERS, {8: proj()}, players)[2]["games"][0]
    assert g["lineup_proj"] == pytest.approx(18 + 12 + 9 + 8)
    assert g["fill_ins"] == ["WR"]
    assert [h["status"] for h in g["hurt"]] == ["Out"]


def test_injuries_scale_next_game_and_ir_lasts_ir_weeks():
    cfg = settings({})
    assert injury_factor({"injury_status": "Out"}, 0, cfg, False) == 0.0
    assert injury_factor({"injury_status": "Out"}, 1, cfg, False) == 1.0
    assert injury_factor({"injury_status": "IR"}, 3, cfg, False) == 0.0
    assert injury_factor({"injury_status": "IR"}, 4, cfg, False) == 1.0
    assert injury_factor({}, 0, cfg, True) == 0.0            # on the IR slot


def test_manager_factor_rewards_beating_projections():
    # Completed weeks 1-4: team 1 scores 20% above its projected lineup, team 2 matches the league.
    weeks = range(1, 5)
    rows = {w: [{"roster_id": 1, "matchup_id": 1, "players": ROSTERS[0]["players"], "points": 1.2 * 49 + 10},
                {"roster_id": 2, "matchup_id": 1, "players": ROSTERS[1]["players"], "points": 51.0}] for w in weeks}
    s = season(completed=weeks, matchups=rows)
    out = build(s, ROSTERS, {**{w: proj() for w in weeks}, 8: proj()}, PLAYERS)
    assert out[1]["manager"]["factor"] > 1.0 > out[2]["manager"]["factor"]
    assert out[1]["manager"]["weeks"] == 4
    g = out[1]["games"][0]
    assert g["proj"] > g["lineup_proj"] * out["_model"]["c"]
    assert out[1]["proj_wins"] + out[2]["proj_wins"] == pytest.approx(1.0)
