"""Trade value: preseason labels, replacement level, PAR, and trade trees."""
from collections import Counter
from datetime import datetime, timezone

import pytest

from pipeline.trade_value import (DAY_MS, Ledger, Roster, TradeBook, annotate, flex_usage, pool_sizes,
                                  production, replacement_levels, settings, trade_period, week1_start)
from pipeline.trades import resolve_trades

PLAYERS = {
    "star": {"full_name": "Star Back", "position": "RB"},
    "mid1": {"full_name": "Mid One", "position": "RB"},
    "mid2": {"full_name": "Mid Two", "position": "RB"},
    "x": {"full_name": "Player X", "position": "WR"},
    "y": {"full_name": "Player Y", "position": "WR"},
    "z": {"full_name": "Player Z", "position": "WR"},
    "w": {"full_name": "Player W", "position": "WR"},
    "rook": {"full_name": "Rookie R", "position": "WR"},
    "qb": {"full_name": "Quarter Back", "position": "QB"},
}
CFG = settings({})
WEEKS = list(range(1, 11))
REPL = 12.0


def make_ledger(holdings, points, starters=None, repl=REPL):
    """holdings: {pid: {week: rid}}; points: {pid: pts per week}; everyone rostered is
    started unless starters={pid: set of weeks started}."""
    rows, pts = {}, {}
    for w in WEEKS:
        key = (2026, w)
        rows[key] = {}
        for pid, by_week in holdings.items():
            rid = by_week.get(w)
            if rid is None:
                continue
            r = rows[key].setdefault(rid, Roster(set(), set(), {}))
            r.players.add(pid)
            if starters is None or w in starters.get(pid, set(WEEKS)):
                r.starters.add(pid)
            r.points[pid] = points[pid]
        pts[key] = {pid: points[pid] for pid, by in holdings.items() if by.get(w) is not None}
    return Ledger([(2026, w) for w in WEEKS], rows, pts, {k: {"RB": repl, "WR": repl, "QB": repl} for k in rows})


def owned(rid, start, end=10):
    return {w: rid for w in range(start, end + 1)}


def trade(tid, time, leg, rosters, adds=None, drops=None, picks=()):
    return {"transaction_id": tid, "type": "trade", "status": "complete", "status_updated": time, "leg": leg,
            "season": "2026", "roster_ids": rosters, "adds": adds or {}, "drops": drops or {},
            "draft_picks": [dict(season=s, round=r, roster_id=orig, owner_id=to, previous_owner_id=frm)
                            for s, r, orig, frm, to in picks]}


def drop(tid, time, leg, pid, rid):
    return {"transaction_id": tid, "type": "free_agent", "status": "complete", "status_updated": time, "leg": leg,
            "season": "2026", "roster_ids": [rid], "adds": {}, "drops": {pid: rid}, "draft_picks": []}


def book(txs, ledger, results=None, draft_times=None, cfg=CFG):
    return TradeBook(txs, ledger, PLAYERS, results or {}, draft_times or {}, {"2026": 1}, cfg)


def cards_for(txs, ledger, **kw):
    b = book(txs, ledger, **kw)
    cards = resolve_trades([t for t in txs if t["type"] == "trade"], [], PLAYERS)
    return {c["transaction_id"]: c for c in annotate(cards, b, {"2026": 0})}, b


def side(card, rid):
    return next(s for s in card["sides"] if s["roster_id"] == rid)


# -- preseason ---------------------------------------------------------------------

def test_preseason_cutoff_is_seven_days_before_week_one():
    start = week1_start("2026", {"season": "2026", "season_start_date": "2026-09-10"}, {})
    assert trade_period(start - 8 * DAY_MS, 1, start) is None          # Preseason 2026
    assert trade_period(start - 6 * DAY_MS, 1, start) == 1             # Week 1
    assert trade_period(start + 30 * DAY_MS, 5, start) == 5


def test_week_one_falls_back_to_config_then_kickoff_rule():
    state = {"season": "2026", "season_start_date": "2026-09-10"}
    configured = week1_start("2025", state, {"2025": "2025-09-05"})
    rule = week1_start("2025", state, {})  # Thursday after Labor Day 2025 = Sept 4
    assert configured - rule == DAY_MS
    assert datetime.fromtimestamp(rule / 1000, timezone.utc) == datetime(2025, 9, 4, 4, tzinfo=timezone.utc)


# -- replacement level and PAR -----------------------------------------------------

def test_replacement_level_counts_dedicated_slots_and_flex_share():
    positions = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "BN"]
    rows = [{"starters": ["qb", "r1", "r2", "w1", "w2", "t1", "r3" if i < 3 else "w3"]} for i in range(4)]
    players = {"qb": {"position": "QB"}, "t1": {"position": "TE"},
               **{f"r{i}": {"position": "RB"} for i in range(1, 4)}, **{f"w{i}": {"position": "WR"} for i in range(1, 4)}}
    usage = flex_usage(rows, positions, players)
    assert usage == {"FLEX": Counter({"RB": 3, "WR": 1})}
    pools = pool_sizes(positions, 4, usage)
    assert pools == {"QB": 4, "RB": 4 * 2.75, "WR": 4 * 2.25, "TE": 4}
    # RB pool = 11 starters, so replacement = the 12th-best RB score.
    levels = replacement_levels({"RB": [float(30 - i) for i in range(15)], "QB": [20.0, 18.0]}, pools)
    assert levels["RB"] == 19.0
    assert levels["QB"] == 0.0 and levels["TE"] == 0.0   # fewer players than the pool


def test_par_floors_each_week_at_zero_and_counts_only_started_weeks():
    led = make_ledger({"x": owned(1, 1)}, {"x": 10.0}, starters={"x": {1, 2}})
    led.rows[(2026, 2)][1].points["x"] = 20.0
    led.rows[(2026, 3)][1].points["x"] = 40.0   # benched: does not count
    v = production(led, "x", 1, (2026, 1), None, PLAYERS)
    assert v["started_weeks"] == 2 and v["weeks"] == 10
    assert v["start_pts"] == 30.0
    assert v["par"] == 0.0 + (20.0 - REPL)        # week 1: 10 - 12 floors at 0


def test_two_for_one_single_star_wins_on_par_despite_fewer_points():
    led = make_ledger({"star": owned(1, 2), "mid1": owned(2, 2), "mid2": owned(2, 2)},
                      {"star": 25.0, "mid1": 13.0, "mid2": 13.0})
    t = trade("t1", 100, 2, [1, 2], adds={"star": 1, "mid1": 2, "mid2": 2}, drops={"star": 2, "mid1": 1, "mid2": 1})
    cards, _ = cards_for([t], led)
    a, b = side(cards["t1"], 1)["value"]["direct"], side(cards["t1"], 2)["value"]["direct"]
    assert b["start_pts"] > a["start_pts"]                     # 9 weeks x 26 vs 9 x 25
    assert a["par"] == pytest.approx(9 * 13.0) and b["par"] == pytest.approx(9 * 2.0)
    v = cards["t1"]["verdict"]["direct"]
    assert v["status"] == "win" and v["roster_id"] == 1 and v["margin"] == pytest.approx(9 * 11.0)
    assert side(cards["t1"], 1)["value"]["direct"]["net_par"] == pytest.approx(99.0)


def test_value_stops_when_player_is_dropped_or_traded_away():
    # x: acquired week 2, dropped week 5 (re-signed by team 1 in week 8: not this trade's value).
    # y: acquired week 2, traded away in week 6.
    holdings = {"x": {**owned(1, 2, 4), **owned(1, 8)}, "y": {**owned(1, 2, 5), **owned(3, 6)}, "z": owned(2, 2)}
    led = make_ledger(holdings, {"x": 20.0, "y": 22.0, "z": 15.0})
    txs = [trade("t1", 100, 2, [1, 2], adds={"x": 1, "y": 1, "z": 2}, drops={"x": 2, "y": 2, "z": 1}),
           drop("d1", 200, 5, "x", 1),
           trade("t2", 300, 6, [1, 3], adds={"y": 3}, drops={"y": 1})]
    cards, _ = cards_for(txs, led)
    s1 = side(cards["t1"], 1)
    x = next(p for p in s1["players"] if p["player_id"] == "x")["value"]
    y = next(p for p in s1["players"] if p["player_id"] == "y")["value"]
    assert x["started_weeks"] == 3 and x["end"]["how"] == "dropped" and x["end"]["week"] == 5
    assert y["started_weeks"] == 4 and y["end"]["how"] == "traded" and y["end"]["to"] == [3]
    assert s1["value"]["direct"]["par"] == pytest.approx(3 * 8.0 + 4 * 10.0)


def test_too_early_to_tell_under_four_started_weeks():
    led = make_ledger({"x": owned(1, 9), "y": owned(2, 9)}, {"x": 20.0, "y": 14.0})
    cards, _ = cards_for([trade("t1", 100, 9, [1, 2], adds={"x": 1, "y": 2}, drops={"x": 2, "y": 1})], led)
    assert cards["t1"]["verdict"]["direct"] == {"status": "win", "roster_id": 1, "margin": 12.0, "started_weeks": 4}
    led = make_ledger({"x": owned(1, 10), "y": owned(2, 10)}, {"x": 20.0, "y": 14.0})
    cards, _ = cards_for([trade("t1", 100, 10, [1, 2], adds={"x": 1, "y": 2}, drops={"x": 2, "y": 1})], led)
    assert cards["t1"]["verdict"]["direct"] == {"status": "early", "started_weeks": 2}


# -- trade trees -------------------------------------------------------------------

def test_tree_follows_a_player_flipped_twice():
    holdings = {"x": {**owned(1, 1, 3), **owned(2, 4)}, "y": {**owned(2, 1, 3), **owned(1, 4, 6), **owned(3, 7)},
                "z": {**owned(3, 1, 6), **owned(1, 7)}, "w": owned(4, 1)}
    led = make_ledger(holdings, {"x": 20.0, "y": 18.0, "z": 16.0, "w": 0.0})
    txs = [trade("t0", 50, 1, [1, 4], adds={"x": 1, "w": 4}, drops={"x": 4, "w": 1}),
           trade("t1", 100, 4, [1, 2], adds={"y": 1, "x": 2}, drops={"x": 1, "y": 2}),
           trade("t2", 200, 7, [1, 3], adds={"z": 1, "y": 3}, drops={"y": 1, "z": 3})]
    cards, b = cards_for(txs, led)
    s = side(cards["t0"], 1)
    assert s["value"]["direct"]["par"] == pytest.approx(3 * 8.0)            # x, weeks 1-3
    assert s["value"]["tree"]["par"] == pytest.approx(3 * 8.0 + 3 * 6.0 + 4 * 4.0)
    child = s["tree"][0]
    assert child["transaction_id"] == "t1" and child["gave"] == ["Player X"] and child["share"] == 1.0
    assert child["children"][0]["transaction_id"] == "t2"
    assert child["children"][0]["assets"][0]["label"] == "Player Z"


def test_tree_follows_a_pick_flipped_then_used():
    # Team 1 gets team 2's 2026 1st in t1, flips it to team 3 for y; team 3 drafts "rook".
    holdings = {"x": owned(2, 1), "y": {**owned(3, 1, 3), **owned(1, 4)}, "rook": owned(3, 5)}
    led = make_ledger(holdings, {"x": 15.0, "y": 17.0, "rook": 20.0})
    results = {("2026", 1, 2): {"player_id": "rook", "player": "Rookie R", "pos": "WR", "roster_id": 3, "pick_no": 1}}
    txs = [trade("t1", 100, 1, [1, 2], adds={"x": 2}, drops={"x": 1}, picks=[("2026", 1, 2, 2, 1)]),
           trade("t2", 200, 4, [1, 3], adds={"y": 1}, drops={"y": 3}, picks=[("2026", 1, 2, 1, 3)])]
    cards, _ = cards_for(txs, led, results=results, draft_times={"2026": 300})
    s1 = side(cards["t1"], 1)
    pick = s1["picks"][0]["value"]
    assert pick["par"] == 0.0 and pick["end"]["how"] == "traded"            # flipped before the draft
    assert s1["value"]["tree"]["par"] == pytest.approx(7 * 5.0)             # y, weeks 4-10
    # Team 3's side of the flip gets the drafted player's value (weeks he was on the roster).
    s3 = side(cards["t2"], 3)
    assert s3["picks"][0]["value"]["drafted"]["player"] == "Rookie R"
    assert s3["value"]["direct"]["par"] == pytest.approx(6 * 8.0)


def test_mixed_flip_splits_return_by_recent_par():
    # Team 1 flips tree player x (recent PAR 4 x 18) with its own w (4 x 6) for z.
    holdings = {"x": {**owned(1, 1, 5), **owned(3, 6)}, "w": {**owned(1, 1, 5), **owned(3, 6)},
                "z": {**owned(3, 1, 5), **owned(1, 6)}, "y": owned(2, 1)}
    led = make_ledger(holdings, {"x": 30.0, "w": 18.0, "z": 22.0, "y": 0.0})
    txs = [trade("t1", 100, 1, [1, 2], adds={"x": 1, "y": 2}, drops={"x": 2, "y": 1}),
           trade("t2", 200, 6, [1, 3], adds={"z": 1, "x": 3, "w": 3}, drops={"x": 1, "w": 1, "z": 3})]
    cards, _ = cards_for(txs, led)
    s = side(cards["t1"], 1)
    child = s["tree"][0]
    assert child["share"] == pytest.approx(18 / 24)
    assert child["gave"] == ["Player X"] and child["gave_other"] == ["Player W"]
    assert child["par"] == pytest.approx(0.75 * 5 * 10.0)
    assert s["value"]["tree"]["par"] == pytest.approx(5 * 18.0 + 0.75 * 50.0)


def test_mixed_flip_without_recent_par_splits_evenly():
    holdings = {"x": {**owned(1, 1, 5), **owned(3, 6)}, "w": {**owned(1, 1, 5), **owned(3, 6)},
                "z": {**owned(3, 1, 5), **owned(1, 6)}, "y": owned(2, 1)}
    led = make_ledger(holdings, {"x": 5.0, "w": 5.0, "z": 22.0, "y": 0.0})
    txs = [trade("t1", 100, 1, [1, 2], adds={"x": 1, "y": 2}, drops={"x": 2, "y": 1}),
           trade("t2", 200, 6, [1, 3], adds={"z": 1, "x": 3, "w": 3}, drops={"x": 1, "w": 1, "z": 3})]
    cards, _ = cards_for(txs, led)
    assert side(cards["t1"], 1)["tree"][0]["share"] == pytest.approx(0.5)


def test_tree_never_counts_the_same_production_twice():
    # Team 1 gets x and y in t1 and flips both together for z: one child node, z counted once.
    holdings = {"x": {**owned(1, 1, 4), **owned(3, 5)}, "y": {**owned(1, 1, 4), **owned(3, 5)},
                "z": {**owned(3, 1, 4), **owned(1, 5)}, "w": owned(2, 1)}
    led = make_ledger(holdings, {"x": 20.0, "y": 20.0, "z": 30.0, "w": 0.0})
    txs = [trade("t1", 100, 1, [1, 2], adds={"x": 1, "y": 1, "w": 2}, drops={"x": 2, "y": 2, "w": 1}),
           trade("t2", 200, 5, [1, 3], adds={"z": 1, "x": 3, "y": 3}, drops={"x": 1, "y": 1, "z": 3})]
    cards, b = cards_for(txs, led)
    s = side(cards["t1"], 1)
    assert len(s["tree"]) == 1 and s["tree"][0]["share"] == 1.0
    assert sorted(s["tree"][0]["gave"]) == ["Player X", "Player Y"]
    assert s["value"]["tree"]["par"] == pytest.approx(2 * 4 * 8.0 + 6 * 18.0)
    # Every started week of z belongs to exactly one node across the whole tree.
    nodes, stack = [], list(s["tree"])
    while stack:
        n = stack.pop()
        nodes.append(n)
        stack.extend(n["children"])
    z_weeks = sum(a["started_weeks"] for n in nodes for a in n["assets"] if a["label"] == "Player Z")
    assert z_weeks == 6
