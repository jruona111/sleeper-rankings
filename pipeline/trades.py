"""Full trade history with draft-pick outcomes (Trades tab, trades.json).

Every traded pick is identified by (season, round, original roster_id). Each
trade that moved it is an event in that pick's ownership chain; the draft
(matched through the draft's slot_to_roster_id, since a traded pick keeps the
original team's draft_slot) tells who finally used it and on which player.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .extras import player_name
from .league import Season
from .sleeper import SleeperClient

MAX_ROUND = 18

PickKey = tuple[str, int, int]


def _time(t: dict) -> int:
    return int(t.get("status_updated") or t.get("created") or 0)


def pick_key(p: dict) -> PickKey:
    return str(p.get("season")), int(p.get("round") or 0), int(p.get("roster_id") or 0)


def draft_results(drafts: list[dict], players: dict) -> tuple[dict[PickKey, dict], set[str], set[str]]:
    """Who used each pick and on whom.

    drafts: [{season, status, start_time, slot_to_roster_id, picks: [...], traded_picks: [...]}].
    Returns (results by pick key, seasons with a completed draft, seasons with any draft).
    Later drafts in the same season (e.g. a rookie draft after a startup draft) win.
    Without slot_to_roster_id, the draft's traded_picks identify a traded pick's
    original roster when that is unambiguous; otherwise the picking roster is used.
    """
    results: dict[PickKey, dict] = {}
    complete, any_draft = set(), set()
    for d in sorted(drafts, key=lambda d: int(d.get("start_time") or d.get("created") or 0)):
        season = str(d.get("season"))
        any_draft.add(season)
        if d.get("status") == "complete":
            complete.add(season)
        slots = {str(k): int(v) for k, v in (d.get("slot_to_roster_id") or {}).items() if v is not None}
        traded = [tp for tp in d.get("traded_picks") or [] if str(tp.get("season")) == season]
        for p in d.get("picks") or []:
            if not p.get("player_id"):
                continue
            user, rnd = int(p.get("roster_id") or 0), int(p.get("round") or 0)
            original = slots.get(str(p.get("draft_slot")))
            if original is None:
                acquired = [int(tp["roster_id"]) for tp in traded
                            if int(tp.get("round") or 0) == rnd and int(tp.get("owner_id") or 0) == user]
                gave_own = any(int(tp.get("round") or 0) == rnd and int(tp.get("roster_id") or 0) == user
                               and int(tp.get("owner_id") or 0) != user for tp in traded)
                original = acquired[0] if len(acquired) == 1 and gave_own else user
            meta = p.get("metadata") or {}
            pid = str(p["player_id"])
            name = player_name(pid, players) if pid in players else (
                " ".join(x for x in (meta.get("first_name"), meta.get("last_name")) if x) or f"Player {pid}")
            results[(season, int(p.get("round") or 0), original)] = {
                "player_id": pid, "player": name,
                "pos": (players.get(pid) or {}).get("position") or meta.get("position"),
                "roster_id": user, "pick_no": p.get("pick_no"),
            }
    return results, complete, any_draft


def pick_chains(trades: list[dict]) -> dict[PickKey, list[dict]]:
    """Every trade event per pick, oldest first: {transaction_id, time, from, to}."""
    chains: dict[PickKey, list[dict]] = {}
    for t in sorted(trades, key=lambda t: (_time(t), str(t.get("transaction_id")))):
        for p in t.get("draft_picks") or []:
            chains.setdefault(pick_key(p), []).append({
                "transaction_id": str(t.get("transaction_id")), "time": _time(t),
                "from": int(p["previous_owner_id"]) if p.get("previous_owner_id") is not None else None,
                "to": int(p["owner_id"]),
            })
    return chains


def outcome(key: PickKey, results: dict[PickKey, dict], complete: set[str], owners: dict[PickKey, int]) -> dict:
    if key in results:
        return {"status": "made", **results[key]}
    if key[0] in complete:
        return {"status": "unused"}  # the draft finished without this pick (e.g. forfeited)
    return {"status": "pending", "owner": owners.get(key)}


def resolve_trades(trades: list[dict], drafts: list[dict], players: dict,
                   traded_picks: list[dict] | None = None) -> list[dict]:
    """trades: Sleeper trade transactions, each with an added "season" field.
    drafts: see draft_results. traded_picks: the league's current owners of traded
    future picks (/league/{id}/traded_picks), used for picks not yet made.
    Returns trade cards, newest first."""
    seen, unique = set(), []
    for t in trades:
        tid = str(t.get("transaction_id"))
        if t.get("type") == "trade" and t.get("status") == "complete" and tid not in seen:
            seen.add(tid)
            unique.append(t)
    results, complete, _ = draft_results(drafts, players)
    chains = pick_chains(unique)
    owners: dict[PickKey, int] = {key: chain[-1]["to"] for key, chain in chains.items()}
    for tp in [tp for d in drafts for tp in d.get("traded_picks") or []] + list(traded_picks or []):
        if tp.get("owner_id") is not None:
            owners[pick_key(tp)] = int(tp["owner_id"])

    cards = []
    for t in unique:
        tid = str(t.get("transaction_id"))
        adds = t.get("adds") or {}
        sides = []
        for rid in [int(r) for r in (t.get("roster_ids") or [])]:
            got = [pid for pid, r in adds.items() if int(r) == rid]
            picks = []
            for p in t.get("draft_picks") or []:
                if int(p.get("owner_id") or 0) != rid:
                    continue
                key = pick_key(p)
                chain = chains.get(key, [])
                idx = next((i for i, e in enumerate(chain) if e["transaction_id"] == tid), -1)
                picks.append({
                    "season": key[0], "round": key[1], "original": key[2],
                    "later": [{"transaction_id": e["transaction_id"], "time": e["time"], "from": e["from"], "to": e["to"]}
                              for e in chain[idx + 1:]],
                    "outcome": outcome(key, results, complete, owners),
                })
            picks.sort(key=lambda p: (p["season"], p["round"]))
            faab = sum(int(b.get("amount") or 0) for b in (t.get("waiver_budget") or []) if int(b.get("receiver") or 0) == rid)
            sides.append({
                "roster_id": rid,
                "players": [{"player_id": pid, "name": player_name(pid, players),
                             "pos": (players.get(pid) or {}).get("position")} for pid in got],
                "picks": picks,
                "faab": faab,
            })
        cards.append({"transaction_id": tid, "season": str(t.get("season")), "week": t.get("leg"),
                      "time": _time(t), "roster_ids": [s["roster_id"] for s in sides], "sides": sides})
    cards.sort(key=lambda c: (-c["time"], c["transaction_id"]))
    return cards


# -- data loading ------------------------------------------------------------------

def load_and_build(client: SleeperClient, current: Season, history: list[Season], state: dict,
                   players: dict, cfg: dict | None = None) -> dict:
    from . import trade_value as tv  # trade_value imports this module
    from .dynasty import fantasy_points

    cfg = tv.settings(cfg)
    trades, everything, drafts = [], [], []
    for s in [current] + history:
        finished = s.league.get("status") == "complete" or s is not current
        if finished:
            rounds = range(0, MAX_ROUND + 1)
        else:  # current season: offseason trades live in round 1; include the week in progress
            rounds = range(0, min(MAX_ROUND, max(1, int(state.get("week") or 1))) + 1)
        for rnd in rounds:
            done = finished or rnd < s.last_completed
            for t in client.transactions(s.league_id, rnd, completed=done, final=finished):
                everything.append(dict(t, season=s.season))
                if t.get("type") == "trade":
                    trades.append(everything[-1])
        for d in client.drafts(s.league):
            full = client.draft(d)
            drafts.append({**d, **full, "picks": client.draft_picks(d), "traded_picks": client.draft_traded_picks(d)})
    cards = resolve_trades(trades, drafts, players, client.traded_picks(current.league))

    seasons = [current] + history
    results, _, _ = draft_results(drafts, players)
    draft_times: dict[str, int] = {}
    for d in drafts:
        if d.get("status") == "complete":
            season = str(d.get("season"))
            draft_times[season] = max(draft_times.get(season, 0), int(d.get("start_time") or d.get("created") or 0))
    start_weeks = {s.season: int(s.settings.get("start_week") or 1) for s in seasons}
    league_points: dict[tuple[int, int], dict[str, float]] = {}
    for s in seasons:
        finished = s.league.get("status") == "complete" or s is not current
        scoring = s.league.get("scoring_settings") or {}
        for week in s.completed_weeks:
            stats = client.stats(s.season, week, completed=finished or week < s.last_completed) if week <= MAX_ROUND else None
            if stats:
                pts = {str(pid): fantasy_points(line, scoring) for pid, line in stats.items() if isinstance(line, dict)}
                league_points[(int(s.season), week)] = {pid: v for pid, v in pts.items() if v is not None}
    book = tv.TradeBook(everything, tv.build_ledger(seasons, players, cfg, league_points), players, results,
                        draft_times, start_weeks, cfg)
    week1 = {s.season: tv.week1_start(s.season, state, cfg["week1_dates"]) for s in seasons}
    tv.annotate(cards, book, week1)
    return {"seasons": sorted({c["season"] for c in cards}, reverse=True), "trades": cards,
            "min_started_weeks": int(cfg["min_started_weeks"]), "recent_par_weeks": int(cfg["recent_par_weeks"]),
            "replacement": cfg["replacement"],
            "week1": {k: datetime.fromtimestamp(v / 1000, timezone.utc).date().isoformat() for k, v in sorted(week1.items())}}
