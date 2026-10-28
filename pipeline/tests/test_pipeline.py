"""End-to-end checks on the synthetic demo league (no network)."""
import json

import pytest

from pipeline import blurbs
from pipeline.config import load_config
from pipeline.rankings import rank_week, zscores
from pipeline.run import backfilled_weeks, build, load_all
from pipeline.sleeper import Cache


def test_zscores_handle_identical_values():
    assert zscores({1: 5.0, 2: 5.0}) == {1: 0.0, 2: 0.0}


def test_rank_week_orders_by_weighted_zscores():
    metrics = {
        1: {"pf": 150, "max_pf": 160, "win_pct": 1.0, "efficiency": 0.94, "recent_form": 150},
        2: {"pf": 120, "max_pf": 140, "win_pct": 0.5, "efficiency": 0.86, "recent_form": 110},
        3: {"pf": 100, "max_pf": 130, "win_pct": 0.0, "efficiency": 0.77, "recent_form": 90},
    }
    rows = rank_week(metrics, load_config().weights)
    assert [r["roster_id"] for r in rows] == [1, 2, 3]
    assert all(0 <= r["score"] <= 100 for r in rows)
    assert rows[0]["score"] > rows[1]["score"] > rows[2]["score"]


@pytest.fixture(scope="module")
def demo_output(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    cfg, current, history, players, state, client = load_all(demo=True)
    cfg.simulations = 500
    build(cfg, current, history, players, state, client.cache, out, demo=True, use_llm=False, client=client)
    return out


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_demo_build_writes_every_file(demo_output):
    for name in ["meta.json", "teams.json", "playoffs.json", "awards.json", "transactions.json",
                 "history.json", "backtest.json", "rankings/week-1.json", "future.json", "trades.json"]:
        assert (demo_output / name).exists(), name


def test_backfill_drops_writeups_and_survives_rebuilds(tmp_path):
    cfg, current, history, players, state, client = load_all(demo=True)
    cfg.simulations = 200
    build(cfg, current, history, players, state, client.cache, tmp_path, demo=True, use_llm=False, backfill={1, 2, 3})
    meta = read(tmp_path / "meta.json")
    assert meta["backfilled_weeks"] == [1, 2, 3] and meta["weeks"] == list(range(1, 9))
    for week in meta["weeks"]:
        data = read(tmp_path / f"rankings/week-{week}.json")
        assert data["backfilled"] == (week <= 3)
        assert all((r["blurb"] is None) == (week <= 3) for r in data["rankings"])
        assert all(r["rank"] and r["score"] is not None for r in data["rankings"])
    # A plain rebuild (and rerunning the backfill) keeps the same weeks write-up free.
    build(cfg, current, history, players, state, client.cache, tmp_path, demo=True, use_llm=False)
    assert backfilled_weeks(tmp_path) == {1, 2, 3}
    build(cfg, current, history, players, state, client.cache, tmp_path, demo=True, use_llm=False, backfill={1, 2, 3})
    assert read(tmp_path / "meta.json")["backfilled_weeks"] == [1, 2, 3]


def test_backfilled_rankings_use_only_data_through_that_week(tmp_path):
    """Ranking week 3 from a season that stops after week 3 gives identical results."""
    cfg, current, history, players, state, client = load_all(demo=True)
    cfg.simulations = 200
    build(cfg, current, history, players, state, client.cache, tmp_path / "full", demo=True, use_llm=False)
    current.completed_weeks = [1, 2, 3]
    current.matchups = {w: current.matchups[w] for w in (1, 2, 3)}
    build(cfg, current, history, players, state, client.cache, tmp_path / "cut", demo=True, use_llm=False)
    full = read(tmp_path / "full/rankings/week-3.json")["rankings"]
    cut = read(tmp_path / "cut/rankings/week-3.json")["rankings"]
    strip = lambda rows: [{k: r[k] for k in ("roster_id", "rank", "score", "z", "metrics", "prev_rank")} for r in rows]
    assert strip(full) == strip(cut)


def test_future_rankings_cover_every_team(demo_output):
    future = read(demo_output / "future.json")
    meta = read(demo_output / "meta.json")
    assert future["years"] == [2027, 2028, 2029]
    assert len(future["teams"]) == len(meta["teams"])
    for view in ["2027", "2028", "2029", "dynasty"]:
        ranks = sorted(t["views"][view]["rank"] for t in future["teams"])
        assert ranks == list(range(1, len(meta["teams"]) + 1))
    for t in future["teams"]:
        assert len(t["views"]["2027"]["top"]) == 3
        assert all(len(p["proj"]) == 3 for p in t["players"])
        assert t["power_rank"] is not None
    # Roster 3 got roster 10's 2027 3rd in a trade.
    picks3 = next(t for t in future["teams"] if t["roster_id"] == 3)["picks"]
    assert {"season": "2027", "round": 3, "original": 10} in picks3


def test_trade_history_resolves_pick_traded_twice(demo_output):
    trades = read(demo_output / "trades.json")
    assert trades["seasons"] == ["2026", "2025"]
    times = [t["time"] for t in trades["trades"]]
    assert times == sorted(times, reverse=True)
    first = next(t for t in trades["trades"] if t["roster_ids"] == [1, 4])
    pick = next(s for s in first["sides"] if s["roster_id"] == 4)["picks"][0]
    assert [e["to"] for e in pick["later"]] == [7]
    assert pick["outcome"]["status"] == "made" and pick["outcome"]["roster_id"] == 7
    assert next(s for s in first["sides"] if s["roster_id"] == 4)["faab"] == 15
    future_pick = [p for t in trades["trades"] for s in t["sides"] for p in s["picks"] if p["season"] == "2027"]
    assert future_pick and future_pick[0]["outcome"] == {"status": "pending", "owner": 3}


def test_trade_values_labels_and_trees(demo_output):
    trades = read(demo_output / "trades.json")["trades"]
    # Offseason trades (May) are preseason; the in-season swaps keep their week.
    assert {t["label"] for t in trades if t["preseason"]} == {"Preseason 2026"}
    assert sorted(t["label"] for t in trades if not t["preseason"]) == ["Week 5", "Week 5"]
    for t in trades:
        assert set(t["verdict"]) == {"direct", "tree"}
        for s in t["sides"]:
            d, tree = s["value"]["direct"], s["value"]["tree"]
            assert tree["par"] >= d["par"] - 1e-6 and d["par"] <= d["start_pts"] + 1e-6
            assert all("value" in p for p in s["players"] + s["picks"])
        nets = [s["value"]["direct"]["net_par"] for s in t["sides"]]
        assert abs(sum(nets)) < 0.05
    # Roster 4 flipped the 1st it got from roster 1: one tree node, the pick it got back.
    first = next(t for t in trades if t["roster_ids"] == [1, 4])
    node = next(s for s in first["sides"] if s["roster_id"] == 4)["tree"][0]
    assert node["gave"] == ["2026 1st"] and node["with"] == [7]
    assert node["assets"][0]["drafted"]["player"]


def test_rankings_are_complete_and_consistent(demo_output):
    meta = json.loads((demo_output / "meta.json").read_text(encoding="utf-8"))
    for week in meta["weeks"]:
        data = json.loads((demo_output / f"rankings/week-{week}.json").read_text(encoding="utf-8"))
        rows = data["rankings"]
        assert sorted(r["rank"] for r in rows) == list(range(1, len(meta["teams"]) + 1))
        for r in rows:
            assert r["blurb"]
            if r["prev_rank"] is not None:
                assert r["change"] == r["prev_rank"] - r["rank"]


def test_playoff_odds_sum_to_slots(demo_output):
    odds = json.loads((demo_output / "playoffs.json").read_text(encoding="utf-8"))
    assert sum(t["playoff_pct"] for t in odds["teams"]) == pytest.approx(100 * odds["playoff_teams"], abs=1)
    assert sum(t["champ_pct"] for t in odds["teams"]) == pytest.approx(100, abs=1)


def test_blurbs_fall_back_to_templates_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    facts = {1: {1: {"team": "A", "week": 1, "rank": 1, "rank_change": 0, "movement": "new",
                     "power_score": 70.0, "record": "1-0", "luck_wins": 0}}}
    out, source = blurbs.write_blurbs(facts, Cache(":memory:"), "m", "low", {1})
    assert source == "template" and "A" in out[1][1][0]


def test_blurbs_fall_back_when_api_errors(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    def boom(*args, **kwargs):
        raise RuntimeError("api down")

    monkeypatch.setattr(blurbs, "_llm_blurbs", boom)
    facts = {1: {1: {"team": "A", "week": 1, "rank": 1, "rank_change": 0, "movement": "new",
                     "power_score": 70.0, "record": "1-0", "luck_wins": 0}}}
    out, source = blurbs.write_blurbs(facts, Cache(":memory:"), "m", "low", {1})
    assert out[1][1][1] == "template"


def test_llm_blurbs_are_cached(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    calls = []

    def fake(batch, model, effort):
        calls.append(batch)
        return {rid: f"AI blurb for {f['team']}" for rid, f in batch.items()}

    monkeypatch.setattr(blurbs, "_llm_blurbs", fake)
    cache = Cache(":memory:")
    facts = {1: {1: {"team": "A", "week": 1, "rank": 1, "rank_change": 0, "movement": "new",
                     "power_score": 70.0, "record": "1-0", "luck_wins": 0}}}
    out1, src = blurbs.write_blurbs(facts, cache, "m", "low", {1})
    out2, _ = blurbs.write_blurbs(facts, cache, "m", "low", {1})
    assert src == "anthropic" and out1 == out2 and len(calls) == 1


def test_pickups_rank_by_weighted_points_and_starts(demo_output):
    from pipeline.extras import pickup_value
    assert pickup_value(100.0, 50.0) == 85.0                  # 0.7 x 100 + 0.3 x 50
    assert pickup_value(100.0, 50.0, {"points": 0.5, "started": 0.5}) == 75.0
    picks = read(demo_output / "transactions.json")["best_pickups"]
    assert picks and all(p["value"] == pickup_value(p["points"], p["started_points"]) for p in picks)
    assert [p["value"] for p in picks] == sorted((p["value"] for p in picks), reverse=True)
