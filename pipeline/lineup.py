"""Optimal-lineup (Max PF) and lineup-mistake calculations.

The optimal lineup is a maximum-weight bipartite matching between starting
slots and rostered players, solved exactly with the Hungarian algorithm. A
greedy fill is not always optimal once players are eligible at several
positions (e.g. a QB/TE) or slots overlap (FLEX, SUPER_FLEX, ...).
"""
from __future__ import annotations

from typing import Iterable

DL = {"DL", "DE", "DT"}
LB = {"LB", "ILB", "OLB"}
DB = {"DB", "CB", "S", "SS", "FS"}

SLOT_ELIGIBILITY: dict[str, set[str]] = {
    "QB": {"QB"},
    "RB": {"RB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "K": {"K"},
    "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"},
    "WRRB_FLEX": {"WR", "RB"},
    "REC_FLEX": {"WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "DL": DL,
    "LB": LB,
    "DB": DB,
    "IDP_FLEX": DL | LB | DB,
}

NON_STARTING = {"BN", "IR", "TAXI", "RES"}


def starting_slots(roster_positions: Iterable[str]) -> list[str]:
    return [p for p in roster_positions if p not in NON_STARTING]


def player_positions(pid: str, players: dict) -> set[str]:
    info = players.get(pid) or {}
    positions = set(info.get("fantasy_positions") or [])
    if info.get("position"):
        positions.add(info["position"])
    if not positions and pid.isalpha():
        positions.add("DEF")  # team defenses use the team abbreviation as id
    return positions


def eligible(slot: str, positions: set[str]) -> bool:
    return bool(SLOT_ELIGIBILITY.get(slot, {slot}) & positions)


def _hungarian(cost: list[list[float]]) -> list[int]:
    """Min-cost assignment for an n x m matrix (n <= m). Returns column per row."""
    n, m = len(cost), len(cost[0])
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)  # p[j] = row assigned to column j (1-indexed)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j], way[j] = cur, j0
                if minv[j] < delta:
                    delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    result = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            result[p[j] - 1] = j - 1
    return result


def optimal_lineup(
    roster_positions: Iterable[str],
    players_points: dict[str, float],
    players: dict,
    pool: Iterable[str] | None = None,
) -> tuple[float, list[tuple[str, str | None]]]:
    """Best possible starting lineup.

    roster_positions: the league's roster_positions (BN/IR/TAXI are ignored).
    players_points: {player_id: points} for the week.
    players: Sleeper /players/nfl dict (only positions are used).
    pool: player ids available (defaults to every key of players_points).

    Returns (max_points, [(slot, player_id or None), ...]). A slot is left
    empty (None, 0 points) when no eligible player would score above zero.
    """
    slots = starting_slots(roster_positions)
    if not slots:
        return 0.0, []
    pool = [pid for pid in (pool if pool is not None else players_points) if pid and pid != "0"]
    pos = {pid: player_positions(pid, players) for pid in pool}
    big = 1e9
    # Columns: real players, then one "empty" dummy per slot (0 points, any slot).
    cost = []
    for slot in slots:
        row = [
            -float(players_points.get(pid) or 0.0) if eligible(slot, pos[pid]) else big
            for pid in pool
        ]
        row += [0.0] * len(slots)
        cost.append(row)
    assignment = _hungarian(cost)
    lineup: list[tuple[str, str | None]] = []
    total = 0.0
    for i, (slot, col) in enumerate(zip(slots, assignment)):
        if col < len(pool) and cost[i][col] < big:
            pid = pool[col]
            lineup.append((slot, pid))
            total += float(players_points.get(pid) or 0.0)
        else:
            lineup.append((slot, None))
    return round(total, 2), lineup


def biggest_mistake(
    roster_positions: Iterable[str],
    starters: list[str],
    players_points: dict[str, float],
    players: dict,
    pool: Iterable[str],
) -> dict | None:
    """The single bench-for-starter swap that would have gained the most points."""
    slots = starting_slots(roster_positions)
    started = set(starters)
    bench = [pid for pid in pool if pid and pid != "0" and pid not in started]
    best = None
    for slot, starter in zip(slots, starters):
        starter_pts = float(players_points.get(starter) or 0.0) if starter and starter != "0" else 0.0
        for pid in bench:
            if not eligible(slot, player_positions(pid, players)):
                continue
            gain = float(players_points.get(pid) or 0.0) - starter_pts
            if gain > 0 and (best is None or gain > best["gain"]):
                best = {
                    "slot": slot,
                    "benched": pid,
                    "benched_points": round(float(players_points.get(pid) or 0.0), 2),
                    "started": starter if starter and starter != "0" else None,
                    "started_points": round(starter_pts, 2),
                    "gain": round(gain, 2),
                }
    return best
