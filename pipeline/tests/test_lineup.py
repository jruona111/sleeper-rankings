from functools import lru_cache

import pytest

from pipeline.lineup import biggest_mistake, eligible, optimal_lineup, starting_slots

PLAYERS = {
    "qb1": {"position": "QB", "fantasy_positions": ["QB"]},
    "qb2": {"position": "QB", "fantasy_positions": ["QB"]},
    "rb1": {"position": "RB", "fantasy_positions": ["RB"]},
    "rb2": {"position": "RB", "fantasy_positions": ["RB"]},
    "rb3": {"position": "RB", "fantasy_positions": ["RB"]},
    "wr1": {"position": "WR", "fantasy_positions": ["WR"]},
    "wr2": {"position": "WR", "fantasy_positions": ["WR"]},
    "wr3": {"position": "WR", "fantasy_positions": ["WR"]},
    "te1": {"position": "TE", "fantasy_positions": ["TE"]},
    "te2": {"position": "TE", "fantasy_positions": ["TE"]},
    "k1": {"position": "K", "fantasy_positions": ["K"]},
    "hyb": {"position": "QB", "fantasy_positions": ["QB", "TE"]},
}


def brute_force(slots, points, players):
    """Reference answer: exhaustive search over (slot, used-players bitmask)."""
    pool = list(points)
    pos = [set(players[p].get("fantasy_positions") or [players[p]["position"]]) for p in pool]

    @lru_cache(maxsize=None)
    def best(i, used):
        if i == len(slots):
            return 0.0
        result = best(i + 1, used)  # leave slot empty
        for j, pid in enumerate(pool):
            if not used & (1 << j) and eligible(slots[i], pos[j]):
                result = max(result, points[pid] + best(i + 1, used | (1 << j)))
        return result

    return round(best(0, 0), 2)


def test_ignores_bench_ir_and_taxi():
    assert starting_slots(["QB", "RB", "BN", "BN", "IR", "TAXI", "FLEX"]) == ["QB", "RB", "FLEX"]


def test_simple_lineup_takes_best_at_each_position():
    pts = {"qb1": 20, "qb2": 25, "rb1": 10, "rb2": 15, "wr1": 8}
    total, lineup = optimal_lineup(["QB", "RB", "WR", "BN", "BN"], pts, PLAYERS)
    assert total == 25 + 15 + 8
    assert ("QB", "qb2") in lineup and ("RB", "rb2") in lineup


def test_flex_takes_best_leftover_rb_wr_te():
    pts = {"rb1": 20, "rb2": 18, "wr1": 5, "te1": 9, "qb1": 30}
    total, lineup = optimal_lineup(["RB", "WR", "TE", "FLEX"], pts, PLAYERS)
    # FLEX must not take the QB.
    assert total == 20 + 5 + 9 + 18
    assert ("FLEX", "rb2") in lineup


def test_super_flex_can_take_a_second_qb():
    pts = {"qb1": 30, "qb2": 24, "rb1": 12, "wr1": 11}
    total, lineup = optimal_lineup(["QB", "SUPER_FLEX", "RB"], pts, PLAYERS)
    assert total == 30 + 24 + 12
    assert ("SUPER_FLEX", "qb2") in lineup


def test_rec_flex_excludes_rbs():
    pts = {"rb1": 40, "wr1": 10, "wr2": 9, "te1": 3}
    total, lineup = optimal_lineup(["WR", "REC_FLEX"], pts, PLAYERS)
    assert total == 10 + 9
    assert all(pid != "rb1" for _, pid in lineup)


def test_wrrb_flex_excludes_tes():
    pts = {"te1": 40, "wr1": 10, "rb1": 7}
    total, _ = optimal_lineup(["WR", "WRRB_FLEX"], pts, PLAYERS)
    assert total == 17


def test_multi_position_player_beats_greedy():
    # Greedy (QB slot first, best QB = hyb 30) leaves TE with 2 points: 32.
    # Optimal puts hyb at TE and qb1 at QB: 28 + 30 = 58.
    pts = {"hyb": 30, "qb1": 28, "te1": 2}
    total, lineup = optimal_lineup(["QB", "TE"], pts, PLAYERS)
    assert total == 58
    assert ("TE", "hyb") in lineup


def test_empty_slot_when_no_eligible_player():
    pts = {"rb1": 10}
    total, lineup = optimal_lineup(["RB", "K"], pts, PLAYERS)
    assert total == 10
    assert ("K", None) in lineup


def test_negative_scorer_is_not_forced_into_lineup():
    pts = {"rb1": 10, "k1": -2}
    total, _ = optimal_lineup(["RB", "K"], pts, PLAYERS)
    assert total == 10


def test_team_defense_uses_abbreviation_id():
    total, lineup = optimal_lineup(["DEF"], {"DET": 12}, {})
    assert total == 12 and lineup == [("DEF", "DET")]


def test_pool_limits_players_considered():
    pts = {"rb1": 10, "rb2": 30}
    total, _ = optimal_lineup(["RB"], pts, PLAYERS, pool=["rb1"])
    assert total == 10


@pytest.mark.parametrize(
    "slots",
    [
        ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX"],
        ["QB", "RB", "WR", "TE", "WRRB_FLEX", "REC_FLEX"],
        ["QB", "TE", "SUPER_FLEX", "FLEX"],
    ],
)
def test_matches_brute_force(slots):
    pts = {
        "qb1": 22.5, "hyb": 19.0, "rb1": 14.2, "rb2": 3.1, "rb3": 11.0,
        "wr1": 18.7, "wr2": 9.9, "wr3": 0.0, "te1": 7.4, "te2": 12.3,
    }
    total, _ = optimal_lineup(slots, pts, PLAYERS)
    assert total == brute_force(slots, pts, PLAYERS)


def test_biggest_mistake_finds_best_single_swap():
    positions = ["QB", "RB", "WR", "FLEX", "BN", "BN"]
    starters = ["qb1", "rb1", "wr1", "rb2"]
    pts = {"qb1": 20, "rb1": 10, "wr1": 8, "rb2": 2, "wr2": 25, "qb2": 30}
    pool = list(pts)
    m = biggest_mistake(positions, starters, pts, PLAYERS, pool)
    # wr2 (25) for rb2 (2) at FLEX gains 23; qb2 for qb1 gains only 10.
    assert m["benched"] == "wr2" and m["started"] == "rb2" and m["gain"] == 23


def test_no_mistake_when_lineup_was_optimal():
    positions = ["QB", "RB", "BN"]
    pts = {"qb1": 20, "rb1": 10, "rb2": 4}
    assert biggest_mistake(positions, ["qb1", "rb1"], pts, PLAYERS, list(pts)) is None


def test_empty_starting_slot_counts_as_zero():
    positions = ["QB", "RB", "BN"]
    pts = {"qb1": 20, "rb2": 6}
    m = biggest_mistake(positions, ["qb1", "0"], pts, PLAYERS, list(pts))
    assert m["benched"] == "rb2" and m["started"] is None and m["gain"] == 6
