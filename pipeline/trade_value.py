"""Trade value: what each side actually got out of a trade (Trades tab, trades.json).

Direct value follows every asset a team received while it stays on that team's
roster: a player until he is dropped or traded away, a pick until it is flipped
or, once used, the drafted player until he leaves. Only weeks the player was in
the team's starting lineup count. The main measure is points above replacement
(PAR): each started week adds max(0, points - that week's replacement level at
his position), so volume from near-replacement players adds little.

Trade-tree value also follows assets the team flipped in later trades: what came
back in the flip counts toward the original trade, and so on down the chain.
When a flip mixes tree assets with other assets, the return is split by each
outgoing asset's recent PAR. Each holding belongs to exactly one tree node and
each flip trade becomes one node, so no production is counted twice.
"""
from __future__ import annotations

import copy
import heapq
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .extras import player_name
from .lineup import DB, DL, LB, SLOT_ELIGIBILITY, starting_slots
from .trades import PickKey, pick_key

DEFAULTS = {
    "week1_dates": {},          # {"2025": "2025-09-04"}; used when Sleeper has no season_start_date
    "preseason_days": 7,        # trades more than this many days before week 1 are "Preseason"
    "min_started_weeks": 4,     # fewer started player-weeks than this -> "Too early to tell"
    "recent_par_weeks": 4,      # trade-tree split: outgoing assets weighted by PAR over this many weeks
    "replacement": {
        "flex_usage": "season",  # flex slots split by position usage over the "season" or that "week"
        "pool_scale": 1.0,       # multiplies the starter pool (1.25 = replacement 25% deeper)
    },
}

FLEX_SLOTS = {"FLEX", "WRRB_FLEX", "REC_FLEX", "SUPER_FLEX", "IDP_FLEX"}
EASTERN = timezone(timedelta(hours=-4))  # EDT, in effect every September
DAY_MS = 86_400_000

Key = tuple[int, int]  # (season, week), chronological
Asset = tuple[str, object]  # ("player", player_id) or ("pick", PickKey)


def settings(overrides: dict | None) -> dict:
    out = copy.deepcopy(DEFAULTS)
    for key, val in (overrides or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key].update(val)
        else:
            out[key] = val
    out["week1_dates"] = {str(k): str(v) for k, v in (out["week1_dates"] or {}).items()}
    return out


# -- preseason ---------------------------------------------------------------------

def default_week1(season: int) -> date:
    """NFL kickoff: the Thursday after Labor Day (first Monday of September)."""
    sept1 = date(season, 9, 1)
    labor_day = sept1 + timedelta(days=(-sept1.weekday()) % 7)
    return labor_day + timedelta(days=3)


def week1_start(season: str, state: dict, configured: dict[str, str]) -> int:
    """Week 1 start (ms, midnight US Eastern): Sleeper's season_start_date for the
    current NFL season, else config.yaml trades.week1_dates, else the kickoff rule."""
    if str(state.get("season")) == str(season) and state.get("season_start_date"):
        day = date.fromisoformat(str(state["season_start_date"])[:10])
    elif str(season) in configured:
        day = date.fromisoformat(configured[str(season)][:10])
    else:
        day = default_week1(int(season))
    return int(datetime(day.year, day.month, day.day, tzinfo=EASTERN).timestamp() * 1000)


def trade_period(time_ms: int, leg: int | None, week1_ms: int, days: int = 7) -> int | None:
    """None = preseason (more than `days` days before week 1), else the week label."""
    if time_ms < week1_ms - days * DAY_MS:
        return None
    if time_ms < week1_ms:
        return 1
    return max(1, int(leg or 1))


# -- replacement level -------------------------------------------------------------

def pos_group(pid: str, players: dict) -> str | None:
    info = players.get(pid) or {}
    pos = info.get("position") or next(iter(info.get("fantasy_positions") or []), None)
    if not pos and pid.isalpha():
        pos = "DEF"
    return "DL" if pos in DL else "LB" if pos in LB else "DB" if pos in DB else pos


def _slot_groups(slot: str) -> set[str]:
    return {"DL" if p in DL else "LB" if p in LB else "DB" if p in DB else p for p in SLOT_ELIGIBILITY.get(slot, {slot})}


def flex_usage(rows: list[dict], roster_positions: list[str], players: dict) -> dict[str, Counter]:
    """Position counts of the players started in each flex slot type."""
    slots = starting_slots(roster_positions)
    usage: dict[str, Counter] = {}
    for row in rows:
        for slot, pid in zip(slots, row.get("starters") or []):
            if slot in FLEX_SLOTS and pid and pid != "0":
                group = pos_group(str(pid), players)
                if group:
                    usage.setdefault(slot, Counter())[group] += 1
    return usage


def pool_sizes(roster_positions: list[str], teams: int, usage: dict[str, Counter], scale: float = 1.0) -> dict[str, float]:
    """Starters league-wide per position: dedicated slots x teams plus each flex
    slot's share by usage (split evenly across eligible positions without usage)."""
    per_team: Counter = Counter()
    for slot in starting_slots(roster_positions):
        if slot in FLEX_SLOTS:
            used = usage.get(slot) or Counter()
            total = sum(used.values())
            groups = _slot_groups(slot)
            for g in groups:
                per_team[g] += used[g] / total if total else 1 / len(groups)
        else:
            for g in _slot_groups(slot):
                per_team[g] += 1 / len(_slot_groups(slot))
    return {g: n * teams * scale for g, n in per_team.items()}


def replacement_levels(points: dict[str, list[float]], pools: dict[str, float]) -> dict[str, float]:
    """Score of the first player outside each position's starter pool (0 if the pool runs out)."""
    out = {}
    for group, pool in pools.items():
        ranked = sorted(points.get(group, []), reverse=True)
        i = int(round(pool))
        out[group] = ranked[i] if i < len(ranked) else 0.0
    return out


def week_par(pts: float, repl: float) -> float:
    return max(0.0, pts - repl)


# -- weekly production ledger ------------------------------------------------------

@dataclass
class Roster:
    players: set[str]
    starters: set[str]
    points: dict[str, float]


@dataclass
class Ledger:
    order: list[Key]
    rows: dict[Key, dict[int, Roster]]
    points: dict[Key, dict[str, float]]   # every rostered player's points that week
    repl: dict[Key, dict[str, float]]
    groups: dict[str, str | None] = field(default_factory=dict)


def build_ledger(seasons: list, players: dict, cfg: dict,
                 league_points: dict[Key, dict[str, float]] | None = None) -> Ledger:
    """seasons: Season objects (matchups per completed week). league_points: every NFL
    player's points per week (from weekly stats) so unrostered players count toward
    replacement level; rostered players' matchup points take priority."""
    rcfg = cfg["replacement"]
    order, rows, points, repl, groups = [], {}, {}, {}, {}
    for s in sorted(seasons, key=lambda s: int(s.season)):
        season_usage = flex_usage([r for w in s.completed_weeks for r in s.matchups[w]], s.roster_positions, players)
        for w in s.completed_weeks:
            key = (int(s.season), w)
            order.append(key)
            week_rows, week_pts, by_group = {}, {}, {}
            for m in s.matchups[w]:
                pts = {str(p): float(v or 0.0) for p, v in (m.get("players_points") or {}).items()}
                week_rows[int(m["roster_id"])] = Roster({str(p) for p in m.get("players") or []},
                                                        {str(p) for p in m.get("starters") or [] if p and p != "0"}, pts)
                week_pts.update(pts)
            for pid, v in {**(league_points or {}).get(key, {}), **week_pts}.items():
                g = groups.setdefault(pid, pos_group(pid, players))
                if g:
                    by_group.setdefault(g, []).append(v)
            usage = season_usage if rcfg["flex_usage"] != "week" else flex_usage(s.matchups[w], s.roster_positions, players)
            pools = pool_sizes(s.roster_positions, len(week_rows), usage, float(rcfg["pool_scale"]))
            rows[key], points[key], repl[key] = week_rows, week_pts, replacement_levels(by_group, pools)
    return Ledger(order, rows, points, repl, groups)


def production(led: Ledger, pid: str, rid: int, start: Key, end: Key | None, players: dict) -> dict:
    """A player's output for roster rid from week `start` through `end` (inclusive; None = now),
    in weeks he was on that roster."""
    group = led.groups.get(pid) or pos_group(pid, players)
    start_pts = par = 0.0
    started = weeks = 0
    for key in led.order[bisect_left(led.order, start):]:
        if end is not None and key > end:
            break
        r = led.rows[key].get(rid)
        if not r or pid not in r.players:
            continue
        weeks += 1
        if pid in r.starters:
            pts = r.points.get(pid, 0.0)
            started += 1
            start_pts += pts
            par += week_par(pts, led.repl[key].get(group, 0.0))
    return {"start_pts": start_pts, "par": par, "started_weeks": started, "weeks": weeks}


def recent_par(led: Ledger, pid: str, before: Key, n: int, players: dict) -> float:
    """Points above replacement over the n weeks before `before`, started or not."""
    group = led.groups.get(pid) or pos_group(pid, players)
    i = bisect_left(led.order, before)
    return sum(week_par(led.points[k][pid], led.repl[k].get(group, 0.0))
               for k in led.order[max(0, i - n):i] if pid in led.points[k])


# -- following assets --------------------------------------------------------------

def _time(t: dict) -> int:
    return int(t.get("status_updated") or t.get("created") or 0)


@dataclass
class Node:
    trade: dict
    share: float
    gave: list[str]
    gave_other: list[str]
    assets: list[dict] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)

    def own(self) -> dict:
        return {k: sum(a[k] for a in self.assets) for k in ("start_pts", "par", "started_weeks")}

    def total(self) -> dict:
        own = self.own()
        out = {"start_pts": self.share * own["start_pts"], "par": self.share * own["par"],
               "started_weeks": own["started_weeks"]}
        for c in self.children:
            sub = c.total()
            for k in out:
                out[k] += sub[k]
        return out


class TradeBook:
    """Every transaction, draft and weekly lineup needed to value trades."""

    def __init__(self, transactions: list[dict], ledger: Ledger, players: dict, results: dict[PickKey, dict],
                 draft_times: dict[str, int], start_weeks: dict[str, int], cfg: dict):
        seen, txs = set(), []
        for t in transactions:
            tid = str(t.get("transaction_id"))
            if t.get("status") == "complete" and tid not in seen:
                seen.add(tid)
                txs.append(t)
        self.txs = sorted(txs, key=lambda t: (_time(t), str(t.get("transaction_id"))))
        self.trades = {str(t["transaction_id"]): t for t in self.txs if t.get("type") == "trade"}
        self.drops: dict[str, list[dict]] = {}
        for t in self.txs:
            for pid in t.get("drops") or {}:
                self.drops.setdefault(str(pid), []).append(t)
        self.pick_moves: dict[PickKey, list[dict]] = {}
        for t in self.txs:
            if t.get("type") == "trade":
                for p in t.get("draft_picks") or []:
                    self.pick_moves.setdefault(pick_key(p), []).append(t)
        self.led, self.players, self.results = ledger, players, results
        self.draft_times, self.start_weeks, self.cfg = draft_times, start_weeks, cfg

    def key(self, t: dict) -> Key:
        season = str(t.get("season"))
        return int(season), max(int(t.get("leg") or 0), self.start_weeks.get(season, 1))

    # Assets a roster sent / received in a trade.
    @staticmethod
    def received(t: dict, rid: int) -> list[Asset]:
        got: list[Asset] = [("player", str(pid)) for pid, r in (t.get("adds") or {}).items() if int(r) == rid]
        return got + [("pick", pick_key(p)) for p in t.get("draft_picks") or [] if int(p.get("owner_id") or 0) == rid]

    @staticmethod
    def sent(t: dict, rid: int) -> list[Asset]:
        out: list[Asset] = [("player", str(pid)) for pid, r in (t.get("drops") or {}).items()
                            if int(r) == rid and t.get("type") == "trade"]
        return out + [("pick", pick_key(p)) for p in t.get("draft_picks") or []
                      if p.get("previous_owner_id") is not None and int(p["previous_owner_id"]) == rid]

    def label(self, asset: Asset) -> str:
        kind, ident = asset
        if kind == "player":
            return player_name(ident, self.players)
        season, rnd, _ = ident
        return f"{season} {_ordinal(rnd)}"

    def _end(self, t: dict | None, rid: int, how: str) -> dict:
        if t is None:
            return {"how": how}
        season, week = self.key(t)
        return {"how": how, "season": str(season), "week": week, "transaction_id": str(t["transaction_id"]),
                "to": [int(r) for r in t.get("roster_ids") or [] if int(r) != rid] if how == "traded" else []}

    def hold_player(self, pid: str, rid: int, since: int, start: Key) -> tuple[dict, dict | None]:
        """Value while rid keeps pid (acquired at `since` ms, counting from week `start`);
        returns (value, the trade that sent him away or None)."""
        exit_tx = next((t for t in self.drops.get(pid, [])
                        if _time(t) > since and int((t.get("drops") or {}).get(pid, -1)) == rid), None)
        value = production(self.led, pid, rid, start, self.key(exit_tx) if exit_tx else None, self.players)
        flip = exit_tx if exit_tx is not None and exit_tx.get("type") == "trade" else None
        value["end"] = self._end(exit_tx, rid, "held" if exit_tx is None else "traded" if flip else "dropped")
        return value, flip

    def hold_pick(self, key: PickKey, rid: int, since: int) -> tuple[dict, dict | None, Asset | None]:
        """Value of a pick: zero if flipped before the draft (returns that trade), else the
        drafted player's value while he stays. Third item = the asset that may be flipped later."""
        zero = {"start_pts": 0.0, "par": 0.0, "started_weeks": 0, "weeks": 0}
        flip = next((t for t in self.pick_moves.get(key, []) if _time(t) > since and any(
            pick_key(p) == key and p.get("previous_owner_id") is not None and int(p["previous_owner_id"]) == rid
            for p in t.get("draft_picks") or [])), None)
        if flip is not None:
            return {**zero, "end": self._end(flip, rid, "traded")}, flip, ("pick", key)
        made = self.results.get(key)
        if made and int(made["roster_id"]) == rid:
            season = key[0]
            pid = str(made["player_id"])
            value, flip = self.hold_player(pid, rid, self.draft_times.get(season, since),
                                           (int(season), self.start_weeks.get(season, 1)))
            value["drafted"] = {"player_id": pid, "player": made["player"], "pos": made.get("pos")}
            return value, flip, ("player", pid)
        if made:
            return {**zero, "end": {"how": "lost"}}, None, None  # used by someone else without a recorded trade
        return {**zero, "end": {"how": "pending"}}, None, None

    def hold(self, asset: Asset, rid: int, t: dict) -> tuple[dict, dict | None, Asset | None]:
        kind, ident = asset
        if kind == "player":
            value, flip = self.hold_player(ident, rid, _time(t), self.key(t))
            return value, flip, asset
        return self.hold_pick(ident, rid, _time(t))

    def weight(self, asset: Asset, t: dict) -> float:
        if asset[0] != "player":
            return 0.0  # unused picks have no production yet
        return recent_par(self.led, asset[1], self.key(t), int(self.cfg["recent_par_weeks"]), self.players)

    def _expand(self, node: Node, rid: int, heap: list, pending: dict) -> None:
        for asset in self.received(node.trade, rid):
            value, flip, follow = self.hold(asset, rid, node.trade)
            node.assets.append({"asset": asset, "label": self.label(asset), "kind": asset[0],
                                "pos": (self.players.get(asset[1]) or {}).get("position") if asset[0] == "player" else None,
                                **value})
            if flip is not None and follow is not None:
                tid = str(flip["transaction_id"])
                if tid not in pending:
                    pending[tid] = []
                    heapq.heappush(heap, (_time(flip), tid))
                pending[tid].append((follow, node.share, node))

    def tree(self, t: dict, rid: int) -> Node:
        """The trade tree for roster rid's side of trade t (root = t itself)."""
        root = Node(t, 1.0, [], [])
        heap: list = []
        pending: dict[str, list] = {}
        self._expand(root, rid, heap, pending)
        while heap:
            _, tid = heapq.heappop(heap)
            entries = pending[tid]
            flip = self.trades[tid]
            tree_share: dict[Asset, float] = {}
            for asset, mult, _ in entries:
                tree_share[asset] = tree_share.get(asset, 0.0) + mult
            outgoing = list(dict.fromkeys(self.sent(flip, rid) + list(tree_share)))
            weights = {a: self.weight(a, flip) for a in outgoing}
            total = sum(weights.values())
            if total > 0:
                share = sum(tree_share.get(a, 0.0) * w for a, w in weights.items()) / total
            else:  # no recent production on any outgoing asset: split evenly
                share = sum(tree_share.get(a, 0.0) for a in outgoing) / len(outgoing)
            node = Node(flip, min(1.0, share), [self.label(a) for a in outgoing if a in tree_share],
                        [self.label(a) for a in outgoing if a not in tree_share])
            entries[0][2].children.append(node)
            self._expand(node, rid, heap, pending)
        return root


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 < n % 100 < 14 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# -- trade cards -------------------------------------------------------------------

def _r(x: float) -> float:
    return round(float(x), 2)


def _value_json(v: dict) -> dict:
    return {"start_pts": _r(v["start_pts"]), "par": _r(v["par"]), "started_weeks": int(v["started_weeks"])}


def node_json(node: Node, book: TradeBook, rid: int) -> dict:
    t = node.trade
    season, _ = book.key(t)
    own = node.own()
    return {
        "transaction_id": str(t["transaction_id"]), "time": _time(t), "season": str(season),
        "with": [int(r) for r in t.get("roster_ids") or [] if int(r) != rid],
        "share": round(node.share, 4), "gave": node.gave, "gave_other": node.gave_other,
        "assets": [{"label": a["label"], "kind": a["kind"], "pos": a.get("pos"), "drafted": a.get("drafted"),
                    "start_pts": _r(a["start_pts"]), "par": _r(a["par"]), "started_weeks": a["started_weeks"],
                    "end": a["end"]} for a in node.assets],
        "start_pts": _r(node.share * own["start_pts"]), "par": _r(node.share * own["par"]),
        "children": [node_json(c, book, rid) for c in node.children],
    }


def verdict(sides: list[dict], mode: str, min_weeks: int) -> dict:
    vals = sorted(((s["value"][mode]["par"], s["roster_id"]) for s in sides), reverse=True)
    started = sum(s["value"][mode]["started_weeks"] for s in sides)
    if started < min_weeks:
        return {"status": "early", "started_weeks": started}
    margin = vals[0][0] - (vals[1][0] if len(vals) > 1 else 0.0)
    if round(margin, 1) == 0:
        return {"status": "even", "started_weeks": started, "margin": 0.0}
    return {"status": "win", "roster_id": vals[0][1], "margin": _r(margin), "started_weeks": started}


def annotate(cards: list[dict], book: TradeBook, week1: dict[str, int]) -> list[dict]:
    """Add period labels, direct and trade-tree value, nets and verdicts to trade cards."""
    cfg = book.cfg
    for card in cards:
        t = book.trades.get(card["transaction_id"])
        if t is None:
            continue
        season = card["season"]
        start = week1.get(season)
        week = trade_period(card["time"], t.get("leg"), start, int(cfg["preseason_days"])) if start else t.get("leg")
        card["preseason"] = week is None
        card["week"] = week
        card["label"] = f"Preseason {season}" if week is None else f"Week {week}"
        for side in card["sides"]:
            rid = side["roster_id"]
            root = book.tree(t, rid)
            by_asset = {a["asset"]: a for a in root.assets}
            items = [(("player", p["player_id"]), p) for p in side["players"]] +                 [(("pick", (p["season"], p["round"], p["original"])), p) for p in side["picks"]]
            for asset, item in items:
                a = by_asset[asset]
                item["value"] = {**_value_json(a), "weeks": a["weeks"], "end": a["end"]}
                if a.get("drafted"):
                    item["value"]["drafted"] = a["drafted"]
            side["value"] = {"direct": _value_json(root.own()), "tree": _value_json(root.total())}
            side["tree"] = [node_json(c, book, rid) for c in root.children]
        for mode in ("direct", "tree"):
            for side in card["sides"]:
                others = [o["value"][mode] for o in card["sides"] if o is not side]
                mine = side["value"][mode]
                side["value"][mode]["net_par"] = _r(mine["par"] - sum(o["par"] for o in others) / max(1, len(others)))
                side["value"][mode]["net_start_pts"] = _r(
                    mine["start_pts"] - sum(o["start_pts"] for o in others) / max(1, len(others)))
        card["verdict"] = {m: verdict(card["sides"], m, int(cfg["min_started_weeks"])) for m in ("direct", "tree")}
    return cards
