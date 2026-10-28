import pytest

from pipeline.league import Season, Team, last_completed_week
from pipeline.metrics import compute_weeks, decayed_mean, season_metrics, streak

PLAYERS = {f"p{i}": {"position": "RB", "fantasy_positions": ["RB"]} for i in range(20)}


def make_season(weekly_scores, median=False, playoff_week_start=15, byes=()):
    """weekly_scores: list of {roster_id: points}; teams paired (1,2), (3,4), ..."""
    teams = {rid: Team(rid, f"u{rid}", f"Team {rid}", f"Owner {rid}", None) for rid in weekly_scores[0]}
    matchups = {}
    for week, scores in enumerate(weekly_scores, start=1):
        rows = []
        for rid, pts in scores.items():
            mid = None if (week, rid) in byes else (rid + 1) // 2
            pid = f"p{rid}"
            rows.append({
                "roster_id": rid, "matchup_id": mid, "points": pts,
                "starters": [pid], "players": [pid, f"p{rid + 10}"],
                "players_points": {pid: pts, f"p{rid + 10}": 0.0},
            })
        matchups[week] = rows
    league = {
        "league_id": "1", "season": "2026", "roster_positions": ["RB", "BN"],
        "settings": {"playoff_week_start": playoff_week_start, "playoff_teams": 2,
                     "league_average_match": 1 if median else 0},
    }
    return Season(league=league, teams=teams, completed_weeks=sorted(matchups),
                  matchups=matchups, future={})


def test_decayed_mean_weights_recent_weeks_more():
    assert decayed_mean([100, 100, 100], 0.85) == pytest.approx(100)
    # last value has weight 1, previous 0.5
    assert decayed_mean([0, 90], 0.5) == pytest.approx(60)
    assert decayed_mean([], 0.85) == 0


def test_streak():
    assert streak(["W", "L", "W", "W"]) == "W2"
    assert streak(["L"]) == "L1"
    assert streak([]) == ""


def test_records_allplay_and_luck():
    s = make_season([{1: 100, 2: 90, 3: 80, 4: 70}])
    m = season_metrics(s, compute_weeks(s, PLAYERS), 1, 0.85)
    assert (m[1]["wins"], m[1]["losses"]) == (1, 0)
    assert (m[3]["wins"], m[3]["losses"]) == (1, 0)
    assert m[3]["allplay_w"] == 1 and m[3]["allplay_l"] == 2
    # Team 3 beat a weak opponent: 1 win vs 1/3 expected.
    assert m[3]["luck"] == pytest.approx(1 - 1 / 3)
    assert m[2]["luck"] == pytest.approx(0 - 2 / 3)
    assert sum(t["luck"] for t in m.values()) == pytest.approx(0)


def test_ties_count_as_half():
    s = make_season([{1: 100, 2: 100, 3: 80, 4: 70}])
    m = season_metrics(s, compute_weeks(s, PLAYERS), 1, 0.85)
    assert m[1]["ties"] == 1 and m[1]["win_pct"] == 0.5


def test_median_league_adds_a_game():
    s = make_season([{1: 100, 2: 90, 3: 80, 4: 70}], median=True)
    m = season_metrics(s, compute_weeks(s, PLAYERS), 1, 0.85)
    assert (m[1]["wins"], m[1]["losses"]) == (2, 0)
    assert (m[2]["wins"], m[2]["losses"]) == (1, 1)
    assert (m[3]["wins"], m[3]["losses"]) == (1, 1)
    assert (m[4]["wins"], m[4]["losses"]) == (0, 2)


def test_bye_week_has_no_result_but_counts_points():
    s = make_season([{1: 100, 2: 90, 3: 80, 4: 70}], byes={(1, 1), (1, 2)})
    m = season_metrics(s, compute_weeks(s, PLAYERS), 1, 0.85)
    assert m[1]["wins"] + m[1]["losses"] == 0
    assert m[1]["pf"] == 100


def test_playoff_weeks_excluded_from_record():
    s = make_season([{1: 100, 2: 90, 3: 80, 4: 70}, {1: 50, 2: 60, 3: 80, 4: 70}], playoff_week_start=2)
    m = season_metrics(s, compute_weeks(s, PLAYERS), 2, 0.85)
    assert (m[1]["wins"], m[1]["losses"]) == (1, 0)
    assert m[1]["games"] == 2 and m[1]["pf"] == 75


def test_max_pf_and_efficiency():
    s = make_season([{1: 100, 2: 90, 3: 80, 4: 70}])
    s.matchups[1][0]["players_points"]["p11"] = 120.0  # bench player outscored the starter
    weeks = compute_weeks(s, PLAYERS)
    assert weeks[1][1].max_pf == 120
    assert weeks[1][1].bench_pts == 20
    assert weeks[1][1].mistake["gain"] == 20


@pytest.mark.parametrize(
    "state,league,expected",
    [
        ({"season": "2026", "week": 8, "season_type": "regular"}, {"season": "2026", "status": "in_season"}, 7),
        ({"season": "2026", "week": 1, "season_type": "pre"}, {"season": "2026", "status": "in_season"}, 0),
        ({"season": "2026", "week": 3, "season_type": "regular"}, {"season": "2025", "status": "complete"}, 17),
    ],
)
def test_last_completed_week(state, league, expected):
    league["settings"] = {"playoff_week_start": 15, "playoff_teams": 6}
    assert last_completed_week(league, state) == expected
