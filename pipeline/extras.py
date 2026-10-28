"""Weekly awards, transaction notes, and all-time / head-to-head history."""
from __future__ import annotations

from .league import Season, pairings
from .metrics import TeamWeek


def player_name(pid: str | None, players: dict) -> str:
    if not pid:
        return "an empty slot"
    info = players.get(pid) or {}
    if info.get("position") == "DEF" or (not info and pid.isalpha()):
        return f"{info.get('last_name') or pid} D/ST" if info.get("last_name") else f"{pid} D/ST"
    name = info.get("full_name") or " ".join(x for x in (info.get("first_name"), info.get("last_name")) if x)
    return name or f"Player {pid}"


def describe_mistake(m: dict | None, players: dict) -> dict | None:
    if not m:
        return None
    return dict(m, benched_name=player_name(m["benched"], players), started_name=player_name(m["started"], players))


# -- weekly awards -----------------------------------------------------------

def weekly_awards(season: Season, weeks: dict[int, dict[int, TeamWeek]], players: dict) -> dict[int, list[dict]]:
    out = {}
    for week, tw in weeks.items():
        if not tw:
            continue
        teams = list(tw.values())
        awards = []
        hi = max(teams, key=lambda t: t.pf)
        lo = min(teams, key=lambda t: t.pf)
        awards.append({"key": "high_score", "title": "High Score", "roster_id": hi.roster_id,
                       "value": round(hi.pf, 2), "detail": f"{hi.pf:.2f} points"})
        awards.append({"key": "low_score", "title": "Low Score", "roster_id": lo.roster_id,
                       "value": round(lo.pf, 2), "detail": f"{lo.pf:.2f} points"})
        games = []
        for a, b in pairings(season.matchups[week]):
            if a in tw and b in tw:
                w, l = (tw[a], tw[b]) if tw[a].pf >= tw[b].pf else (tw[b], tw[a])
                games.append((w.pf - l.pf, w, l))
        if games:
            margin, w, l = max(games, key=lambda g: g[0])
            awards.append({"key": "blowout", "title": "Biggest Blowout", "roster_id": w.roster_id,
                           "opponent": l.roster_id, "value": round(margin, 2),
                           "detail": f"Won {w.pf:.2f}–{l.pf:.2f} (+{margin:.2f})"})
            margin, w, l = min(games, key=lambda g: g[0])
            awards.append({"key": "closest", "title": "Closest Game", "roster_id": w.roster_id,
                           "opponent": l.roster_id, "value": round(margin, 2),
                           "detail": f"{w.pf:.2f}–{l.pf:.2f} (margin {margin:.2f})"})
        losers = [t for t in teams if t.result == "L"]
        if losers:
            u = max(losers, key=lambda t: t.pf)
            awards.append({"key": "unluckiest", "title": "Unluckiest Team", "roster_id": u.roster_id,
                           "opponent": u.opponent, "value": round(u.pf, 2),
                           "detail": f"Lost with {u.pf:.2f} points"})
        best = max(teams, key=lambda t: (t.efficiency, t.pf))
        awards.append({"key": "best_manager", "title": "Best Manager", "roster_id": best.roster_id,
                       "value": round(best.efficiency, 4),
                       "detail": f"{100 * best.efficiency:.1f}% of max ({best.pf:.2f} / {best.max_pf:.2f})"})
        mistakes = [t for t in teams if t.mistake]
        if mistakes:
            t = max(mistakes, key=lambda t: t.mistake["gain"])
            m = describe_mistake(t.mistake, players)
            awards.append({"key": "worst_mistake", "title": "Worst Lineup Mistake", "roster_id": t.roster_id,
                           "value": m["gain"],
                           "detail": f"Benched {m['benched_name']} ({m['benched_points']:.2f}) for "
                                     f"{m['started_name']} ({m['started_points']:.2f}): −{m['gain']:.2f}"})
        out[week] = awards
    return out


# -- transactions ------------------------------------------------------------

def _points_since(pid: str, rid: int, from_week: int, weeks: dict[int, dict[int, TeamWeek]]) -> tuple[float, float, int]:
    """(points while rostered, points while started, weeks rostered) for rid."""
    total = started = 0.0
    n = 0
    for w in sorted(weeks):
        if w < from_week or rid not in weeks[w]:
            continue
        t = weeks[w][rid]
        if pid in t.players:
            n += 1
            pts = float(t.players_points.get(pid) or 0.0)
            total += pts
            if pid in t.starters:
                started += pts
    return round(total, 2), round(started, 2), n


PICKUP_WEIGHTS = {"points": 0.7, "started": 0.3}


def pickup_value(points: float, started_points: float, weights: dict | None = None) -> float:
    """Pickup score: a blend of all points while rostered and points in the starting lineup."""
    w = {**PICKUP_WEIGHTS, **(weights or {})}
    return round(float(w["points"]) * points + float(w["started"]) * started_points, 2)


def transaction_notes(season: Season, weeks: dict[int, dict[int, TeamWeek]], players: dict,
                      pickup_weights: dict | None = None) -> dict:
    pickups, trades = [], []
    for leg, txs in sorted(season.transactions.items()):
        for t in txs:
            kind = t.get("type")
            adds = t.get("adds") or {}
            if kind in ("waiver", "free_agent"):
                for pid, rid in adds.items():
                    total, started, n = _points_since(pid, int(rid), leg, weeks)
                    pickups.append({
                        "week": leg, "roster_id": int(rid), "player_id": pid,
                        "player": player_name(pid, players), "type": kind,
                        "faab": (t.get("settings") or {}).get("waiver_bid"),
                        "points": total, "started_points": started, "weeks": n,
                        "value": pickup_value(total, started, pickup_weights),
                    })
            elif kind == "trade":
                sides = []
                for rid in t.get("roster_ids") or []:
                    got = [pid for pid, r in adds.items() if int(r) == int(rid)]
                    pts = [(_points_since(pid, int(rid), leg, weeks), pid) for pid in got]
                    picks = [p for p in (t.get("draft_picks") or []) if int(p.get("owner_id", -1)) == int(rid)]
                    sides.append({
                        "roster_id": int(rid),
                        "received": [{"player_id": pid, "player": player_name(pid, players),
                                      "points": p[0], "started_points": p[1]} for p, pid in pts],
                        "picks": [f"{p.get('season')} round {p.get('round')}" for p in picks],
                        "started_points": round(sum(p[1] for p, _ in pts), 2),
                    })
                if len(sides) >= 2:
                    best = max(sides, key=lambda s: s["started_points"])
                    trades.append({"week": leg, "transaction_id": t.get("transaction_id"),
                                   "sides": sides, "leader": best["roster_id"],
                                   "margin": round(best["started_points"] - min(s["started_points"] for s in sides), 2)})
    pickups.sort(key=lambda p: (-p["value"], -p["started_points"]))
    return {"best_pickups": pickups[:15], "trades": trades}


def team_moves(season: Season, rid: int, week: int, players: dict) -> list[str]:
    """Plain-language list of a team's moves during a given week (for storylines)."""
    moves = []
    for t in season.transactions.get(week, []):
        if rid not in [int(r) for r in (t.get("roster_ids") or [])]:
            continue
        adds = [pid for pid, r in (t.get("adds") or {}).items() if int(r) == rid]
        drops = [pid for pid, r in (t.get("drops") or {}).items() if int(r) == rid]
        if t.get("type") == "trade":
            others = [season.teams[int(r)].team_name for r in t["roster_ids"] if int(r) != rid and int(r) in season.teams]
            moves.append(f"traded away {', '.join(player_name(p, players) for p in drops) or 'picks'} for "
                         f"{', '.join(player_name(p, players) for p in adds) or 'picks'} with {', '.join(others)}")
        else:
            for p in adds:
                moves.append(f"picked up {player_name(p, players)}")
            if not adds:
                for p in drops:
                    moves.append(f"dropped {player_name(p, players)}")
    return moves


# -- history -----------------------------------------------------------------

def _owner_key(season: Season, rid: int) -> str:
    team = season.teams.get(rid)
    return team.owner_id if team and team.owner_id else f"{season.season}:{rid}"


def league_history(seasons: list[tuple[Season, dict[int, dict[int, TeamWeek]]]]) -> dict:
    """All-time records and head-to-head by owner across seasons (current first)."""
    alltime: dict[str, dict] = {}
    h2h: dict[str, dict[str, dict]] = {}
    season_rows = []
    for season, weeks in seasons:
        champion = None
        for m in season.bracket:
            if m.get("p") == 1 and m.get("w"):
                champion = _owner_key(season, int(m["w"]))
        season_rows.append({"season": season.season, "league_id": season.league_id,
                            "name": season.league.get("name"), "champion_owner": champion,
                            "complete": season.league.get("status") == "complete"})
        for rid, team in season.teams.items():
            key = _owner_key(season, rid)
            row = alltime.setdefault(key, {"owner_id": key, "name": team.owner_name, "team_name": team.team_name,
                                           "seasons": [], "wins": 0, "losses": 0, "ties": 0, "pf": 0.0,
                                           "pa": 0.0, "playoff_wins": 0, "playoff_losses": 0, "championships": 0})
            row["seasons"].append(season.season)
            if champion == key:
                row["championships"] += 1
        for week, tw in weeks.items():
            for rid, t in tw.items():
                key = _owner_key(season, rid)
                row = alltime[key]
                row["pf"] += t.pf
                if t.pa is not None:
                    row["pa"] += t.pa
                if t.playoff:
                    # Only winners-bracket games count as playoff results.
                    if t.result and any(rid in (m.get("t1"), m.get("t2")) and m.get("r") for m in season.bracket):
                        row["playoff_wins" if t.result == "W" else "playoff_losses"] += t.result in "WL"
                else:
                    for r in (t.result, t.median_result):
                        row["wins"] += r == "W"
                        row["losses"] += r == "L"
                        row["ties"] += r == "T"
                if t.opponent is not None and t.result:
                    opp = _owner_key(season, t.opponent)
                    rec = h2h.setdefault(key, {}).setdefault(opp, {"w": 0, "l": 0, "t": 0, "pf": 0.0, "pa": 0.0, "games": []})
                    rec[{"W": "w", "L": "l", "T": "t"}[t.result]] += 1
                    rec["pf"] = round(rec["pf"] + t.pf, 2)
                    rec["pa"] = round(rec["pa"] + (t.pa or 0.0), 2)
                    rec["games"].append({"season": season.season, "week": week, "pf": round(t.pf, 2),
                                         "pa": round(t.pa or 0.0, 2), "result": t.result, "playoff": t.playoff})
    for row in alltime.values():
        g = row["wins"] + row["losses"] + row["ties"]
        row["win_pct"] = round((row["wins"] + 0.5 * row["ties"]) / g, 4) if g else 0.0
        row["pf"], row["pa"] = round(row["pf"], 2), round(row["pa"], 2)
    return {
        "seasons": season_rows,
        "alltime": sorted(alltime.values(), key=lambda r: (-r["championships"], -r["win_pct"], -r["pf"])),
        "h2h": h2h,
    }
