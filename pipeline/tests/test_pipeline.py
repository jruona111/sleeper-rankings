"""End-to-end checks on the synthetic demo league (no network)."""
import json

import pytest

from pipeline import blurbs
from pipeline.config import load_config
from pipeline.rankings import rank_week, zscores
from pipeline.run import build, load_all
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
    build(cfg, current, history, players, state, client.cache, out, demo=True, use_llm=False)
    return out


def test_demo_build_writes_every_file(demo_output):
    for name in ["meta.json", "teams.json", "playoffs.json", "awards.json", "transactions.json",
                 "history.json", "backtest.json", "rankings/week-1.json"]:
        assert (demo_output / name).exists(), name


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
