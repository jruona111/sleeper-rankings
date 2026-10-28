"""Pipeline entry point: fetch -> metrics -> rankings -> blurbs -> JSON.

    python -m pipeline              # real league from config.yaml / LEAGUE_ID
    python -m pipeline --demo       # synthetic league, no network
    python -m pipeline --no-llm     # template blurbs even if ANTHROPIC_API_KEY is set
    python -m pipeline backfill --from 1 --to 7
                                    # (re)build and mark weeks 1-7 as backfilled: ranked and
                                    # charted like any week, but without movement write-ups
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .backtest import run_backtest
from .blurbs import write_blurbs
from . import dynasty, trades, winprob
from .config import CACHE_PATH, SITE_DATA, Config, league_id_is_set, load_config
from .extras import describe_mistake, league_history, transaction_notes, weekly_awards
from .league import Season, load_history, load_season
from .metrics import compute_weeks, remaining_sos
from .playoffs import simulate
from .rankings import METRIC_LABELS, all_rankings
from .sleeper import Cache, SleeperClient
from .storylines import build_facts


def load_all(demo: bool = False, cfg: Config | None = None):
    cfg = cfg or load_config()
    if demo:
        from .demo import CURRENT_ID, DemoLeague

        cache = Cache(":memory:")
        client = SleeperClient(cache, transport=DemoLeague().transport)
        cfg.league_id = CURRENT_ID
    else:
        if not league_id_is_set(cfg.league_id):
            sys.exit("Set league_id in config.yaml (or the LEAGUE_ID env var) to your Sleeper league ID, "
                     "or run with --demo to use sample data.")
        client = SleeperClient(Cache(CACHE_PATH))
    state = client.state()
    league = client.league(cfg.league_id)
    if not league:
        sys.exit(f"League {cfg.league_id} not found on Sleeper. Check league_id in config.yaml.")
    players = client.players()
    current = load_season(client, league, state, current=True)
    history = load_history(client, league, state, cfg.history_seasons)
    return cfg, current, history, players, state, client


# -- JSON helpers ----------------------------------------------------------------

def _r(x, nd=2):
    return round(x, nd) if isinstance(x, float) else x


METRIC_KEYS = ["games", "wins", "losses", "ties", "win_pct", "pf", "pf_total", "pa", "pa_total", "max_pf",
               "max_pf_total", "efficiency", "recent_form", "consistency", "allplay_w", "allplay_l",
               "allplay_t", "allplay_pct", "expected_wins", "luck", "sos", "bench_total", "bench_pg",
               "high", "low", "streak", "last3"]


def metrics_json(m: dict, players: dict) -> dict:
    out = {k: _r(m[k], 4 if k in ("win_pct", "efficiency", "allplay_pct") else 2) for k in METRIC_KEYS}
    out["worst_mistake"] = describe_mistake(m.get("worst_mistake"), players)
    return out


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")


def backfilled_weeks(out_dir: Path) -> set[int]:
    """Weeks an earlier backfill marked as write-up free (kept across rebuilds)."""
    weeks = set()
    for path in (out_dir / "rankings").glob("week-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("backfilled"):
            weeks.add(int(data["week"]))
    return weeks


def _optional(log, name: str, fn, fallback):
    """Build an optional tab's data; a failure there never breaks the whole build."""
    try:
        return fn()
    except Exception as err:  # noqa: BLE001
        log(f"! {name} failed: {err!r}; writing an empty file")
        return fallback


def build(cfg: Config, current: Season, history: list[Season], players: dict, state: dict,
          cache: Cache, out_dir: Path, demo: bool, use_llm: bool, client: SleeperClient | None = None,
          backfill: set[int] | None = None) -> dict:
    t0 = time.time()
    log = lambda msg: print(f"[{time.time() - t0:5.1f}s] {msg}")
    if not current.completed_weeks:
        log("No completed weeks yet - writing league info only.")
    weeks = compute_weeks(current, players)
    log(f"Season {current.season}: {len(current.teams)} teams, completed weeks {current.completed_weeks}")

    rankings = all_rankings(current, weeks, cfg.weights, cfg.decay)
    for week, data in rankings.items():
        cache.save_rankings(current.league_id, week, data["rows"])
    log(f"Ranked {len(rankings)} weeks")

    # Backfilled weeks are ranked from the same week-by-week data but get no write-ups.
    backfilled = (backfilled_weeks(out_dir) | set(backfill or ())) & set(rankings)
    facts = {w: build_facts(current, weeks, rankings, w, players) for w in rankings}
    latest = max(rankings) if rankings else 0
    llm_weeks = set(rankings) if cfg.blurb_weeks == "all" else {latest}
    blurbs, blurb_source = write_blurbs({w: f for w, f in facts.items() if w not in backfilled}, cache,
                                        cfg.blurb_model, cfg.blurb_effort, llm_weeks, use_llm)
    log(f"Blurbs written ({blurb_source}); backfilled without write-ups: {sorted(backfilled) or 'none'}")

    # Fresh output directory (keeps stale weeks from lingering).
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    for week, data in rankings.items():
        rows = []
        for r in data["rows"]:
            rid = r["roster_id"]
            text, src = blurbs[week][rid] if week in blurbs else (None, None)
            rows.append({
                "rank": r["rank"], "prev_rank": r["prev_rank"], "change": r["change"], "roster_id": rid,
                "score": r["score"], "composite": round(r["composite"], 4), "z": r["z"], "driver": r["driver"],
                "metrics": metrics_json(data["metrics"][rid], players),
                "week": weeks[week][rid].to_json() if rid in weeks[week] else None,
                "blurb": text, "blurb_source": src, "facts": facts[week][rid],
            })
        write_json(out_dir / "rankings" / f"week-{week}.json",
                   {"week": week, "backfilled": week in backfilled, "rankings": rows})

    latest_metrics = rankings[latest]["metrics"] if latest else {}
    rem_sos = remaining_sos(current, latest_metrics) if latest else {}

    seasons = [(current, weeks)] + [(s, compute_weeks(s, players)) for s in history]
    hist = league_history(seasons)
    log(f"History: {len(seasons)} season(s)")

    teams_out = {}
    for rid, team in current.teams.items():
        weekly = []
        for week in sorted(weeks):
            t = weeks[week].get(rid)
            if not t:
                continue
            row = t.to_json()
            row["mistake"] = describe_mistake(t.mistake, players)
            rk = next((x for x in rankings[week]["rows"] if x["roster_id"] == rid), None)
            row["rank"], row["score"] = (rk["rank"], rk["score"]) if rk else (None, None)
            weekly.append(row)
        schedule = [{"week": w["week"], "opponent": w["opponent"], "pf": w["pf"], "pa": w["pa"],
                     "result": w["result"], "playoff": w["playoff"], "future": False} for w in weekly]
        for week, pairs in sorted(current.future.items()):
            for a, b in pairs:
                if rid in (a, b):
                    schedule.append({"week": week, "opponent": b if a == rid else a, "future": True})
        owner = team.owner_id or f"{current.season}:{rid}"
        h2h = []
        for other_rid, other in current.teams.items():
            if other_rid == rid:
                continue
            rec = hist["h2h"].get(owner, {}).get(other.owner_id or f"{current.season}:{other_rid}")
            if rec:
                h2h.append({"opponent": other_rid, "w": rec["w"], "l": rec["l"], "t": rec["t"],
                            "pf": rec["pf"], "pa": rec["pa"], "games": rec["games"]})
        teams_out[rid] = {
            **team.to_json(),
            "season": metrics_json(latest_metrics[rid], players) if rid in latest_metrics else None,
            "remaining_sos": _r(rem_sos.get(rid)) if rem_sos.get(rid) is not None else None,
            "weekly": weekly,
            "schedule": schedule,
            "h2h": h2h,
            "alltime": next((a for a in hist["alltime"] if a["owner_id"] == owner), None),
        }
    if client is not None and current.future:
        upcoming = _optional(log, "Win probabilities", lambda: winprob.load_and_build(
            client, current, players, cfg.win_prob), {})
        for rid, data in upcoming.items():
            if rid in teams_out:
                teams_out[rid]["upcoming"] = data
        model = upcoming.get("_model")
        if model:
            log(f"Win probabilities: {sum(len(v['games']) for k, v in upcoming.items() if k != '_model')} team-games, "
                f"calibration x{model['c']} over {model['calibration_weeks']} weeks, SD {model['sd']}")
    write_json(out_dir / "teams.json", teams_out)

    if latest:
        odds = simulate(current, weeks, latest_metrics, cfg.simulations, cfg.random_seed)
        log(f"Playoff odds: {odds['simulations']} simulations, status {odds['status']}")
    else:
        odds = {"status": "preseason", "teams": []}
    write_json(out_dir / "playoffs.json", odds)

    write_json(out_dir / "awards.json", {"weeks": weekly_awards(current, weeks, players)})
    write_json(out_dir / "transactions.json", transaction_notes(current, weeks, players))
    write_json(out_dir / "history.json", {k: v for k, v in hist.items() if k != "h2h"})

    if client is not None:
        power_ranks = {r["roster_id"]: r["rank"] for r in rankings[latest]["rows"]} if latest else {}
        future = _optional(log, "Future rankings", lambda: dynasty.load_and_build(
            client, current, history, players, power_ranks, cfg.dynasty, log), {"years": [], "teams": []})
        write_json(out_dir / "future.json", future)
        log(f"Future rankings: {len(future['teams'])} teams, years {future['years']}")
        trade_data = _optional(log, "Trade history", lambda: trades.load_and_build(
            client, current, history, state, players, cfg.trades), {"seasons": [], "trades": []})
        write_json(out_dir / "trades.json", trade_data)
        log(f"Trade history: {len(trade_data['trades'])} trades")

    backtest = run_backtest(seasons, cfg.weights, cfg.decay)
    write_json(out_dir / "backtest.json", backtest)
    best = max(backtest["results"], key=lambda r: r["accuracy"] or 0)
    cur = backtest["results"][0]
    log(f"Backtest: current weights {cur['accuracy']}, best '{best['name']}' {best['accuracy']}")

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "demo": demo,
        "league": {
            "league_id": current.league_id,
            "name": current.league.get("name"),
            "season": current.season,
            "avatar": f"https://sleepercdn.com/avatars/thumbs/{current.league['avatar']}" if current.league.get("avatar") else None,
            "status": current.league.get("status"),
            "num_teams": current.num_teams,
            "playoff_teams": current.playoff_teams,
            "playoff_week_start": current.playoff_week_start,
            "median_scoring": current.median,
            "roster_positions": current.roster_positions,
        },
        "nfl_state": {k: state.get(k) for k in ("season", "week", "season_type")},
        "weeks": sorted(rankings),
        "latest_week": latest,
        "backfilled_weeks": sorted(backfilled),
        "teams": {rid: t.to_json() for rid, t in current.teams.items()},
        "weights": cfg.weights,
        "decay": cfg.decay,
        "metric_labels": METRIC_LABELS,
        "blurb_source": blurb_source,
    }
    write_json(out_dir / "meta.json", meta)
    log(f"Wrote JSON to {out_dir}")
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["build", "backfill"], default="build",
                        help="build (default) or backfill a range of past weeks")
    parser.add_argument("--from", dest="from_week", type=int, help="backfill: first week (default 1)")
    parser.add_argument("--to", dest="to_week", type=int, help="backfill: last week (default latest completed)")
    parser.add_argument("--demo", action="store_true", help="use a synthetic league (no network)")
    parser.add_argument("--no-llm", action="store_true", help="template blurbs only")
    parser.add_argument("--out", type=Path, default=SITE_DATA, help="output directory (default site/data)")
    args = parser.parse_args()
    cfg, current, history, players, state, client = load_all(demo=args.demo)
    backfill: set[int] = set()
    if args.command == "backfill":
        done = current.completed_weeks
        lo = args.from_week or (done[0] if done else 1)
        hi = args.to_week or (done[-1] if done else 0)
        missing = [w for w in range(lo, hi + 1) if w not in done]
        if not done or lo > hi or missing:
            sys.exit(f"Can only backfill completed weeks {done[0] if done else '-'}-{done[-1] if done else '-'}"
                     f"{f' (week {missing[0]} is not complete)' if missing else ''}.")
        backfill = set(range(lo, hi + 1))
        print(f"Backfilling weeks {lo}-{hi} (rankings and charts, no write-ups)")
    build(cfg, current, history, players, state, client.cache, args.out, args.demo, not args.no_llm,
          client=client, backfill=backfill)
    print(f"Sleeper API requests this run: {client.fetches}")


if __name__ == "__main__":
    main()
