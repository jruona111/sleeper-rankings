"""Synthetic Sleeper league for local testing (python -m pipeline --demo).

Builds API-shaped responses for two seasons (a finished 2025 season with
median scoring and a 2026 season in progress) and serves them through the
same SleeperClient interface as the real API.
"""
from __future__ import annotations

import random
import re

CURRENT_ID = "900000000000000002"
PREVIOUS_ID = "900000000000000001"
COMPLETED_WEEKS = 8

FIRST = ["Josh", "Jalen", "Lamar", "Patrick", "Joe", "Justin", "Bijan", "Breece", "Jahmyr", "Saquon",
         "Derrick", "Kyren", "Travis", "CeeDee", "Ja'Marr", "Amon", "Puka", "Garrett", "Nico", "Malik",
         "Sam", "George", "Mark", "Trey", "Brock", "Jake", "Dalton", "Tank", "Rashee", "Zay", "Chris", "DK",
         "Tyreek", "Drake", "Rome", "Jordan", "Kenneth", "James", "Chase", "Brian", "De'Von", "Isiah"]
LAST = ["Allenby", "Hurtsworth", "Jackman", "Mahone", "Burrowes", "Herbst", "Robins", "Halley", "Gibbons",
        "Barkman", "Henryson", "Williamsby", "Kelcey", "Lambert", "Chasen", "St. Brown", "Nacua", "Wilsons",
        "Collinsworth", "Nabers", "LaPorte", "Kittle", "Andrewsen", "McBride", "Bowersox", "Ferguson",
        "Kincaid", "Dell", "Rice", "Flowers", "Olave", "Metcalfe", "Hillman", "London", "Odunze", "Addison",
        "Walker", "Cook", "Brown", "Robinson", "Achane", "Pacheco"]
NFL = ["ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET", "GB", "HOU", "IND",
       "JAX", "KC", "LV", "LAC", "LAR", "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SF", "SEA",
       "TB", "TEN", "WAS"]
TEAM_NAMES = ["Taco Corp", "Mahomes Alone", "The Replacements", "Kittle Me Elmo", "Bijan Mustard",
              "Hurts So Good", "Ja'Marr the Merrier", "Saquon Deck", "Lamb Chops", "Herbert's Garden",
              "Nacua Matata", "Gibbs Me More"]
OWNERS = ["gridirongreg", "waiverwendy", "faabfrank", "benchbetty", "tradebaitted", "flexluthor",
          "kickerkate", "puntgod", "draftdaddy", "sleeperagent", "redzonerita", "byeweekbob"]
ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "K", "DEF"] + ["BN"] * 6
SLOT_OK = {"QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"},
           "FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"}}
MEAN = {"QB": 18, "RB": 10, "WR": 10, "TE": 7, "K": 8, "DEF": 7}
SD = {"QB": 6, "RB": 6, "WR": 6.5, "TE": 4.5, "K": 3.5, "DEF": 5}


def _players(rng: random.Random) -> dict:
    players = {}
    pid = 1000
    counts = {"QB": 40, "RB": 80, "WR": 95, "TE": 40, "K": 32}
    for pos, n in counts.items():
        for i in range(n):
            pid += 1
            first, last = rng.choice(FIRST), rng.choice(LAST)
            players[str(pid)] = {
                "player_id": str(pid), "position": pos, "fantasy_positions": [pos],
                "first_name": first, "last_name": last, "full_name": f"{first} {last}",
                "team": rng.choice(NFL),
                "_talent": max(1.0, MEAN[pos] * (1.6 - 1.2 * i / n) + rng.gauss(0, 1.5)),
            }
    # A QB/TE hybrid makes the lineup optimizer earn its keep.
    hybrid = next(p for p in players.values() if p["position"] == "QB" and p["_talent"] < 12)
    hybrid["fantasy_positions"] = ["QB", "TE"]
    for team in NFL:
        players[team] = {"player_id": team, "position": "DEF", "fantasy_positions": ["DEF"],
                         "first_name": team, "last_name": team, "team": team,
                         "_talent": MEAN["DEF"] + rng.gauss(0, 1.5)}
    byes = {team: 5 + (i % 10) for i, team in enumerate(NFL)}
    for p in players.values():
        p["_bye"] = byes[p["team"]]
    return players


def _schedule(n: int, weeks: int, shift: int) -> dict[int, list[tuple[int, int]]]:
    ids = list(range(1, n + 1))
    out = {}
    for week in range(1, weeks + 1):
        r = (week + shift) % (n - 1)
        rot = [ids[0]] + (ids[1:][-r:] + ids[1:][:-r] if r else ids[1:])
        out[week] = [(rot[i], rot[n - 1 - i]) for i in range(n // 2)]
    return out


class DemoLeague:
    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.players = _players(self.rng)
        self.routes: dict[str, object] = {}
        self.routes["/state/nfl"] = {"week": COMPLETED_WEEKS + 1, "leg": COMPLETED_WEEKS + 1, "season": "2026",
                                     "season_type": "regular", "display_week": COMPLETED_WEEKS + 1}
        self.routes["/players/nfl"] = {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                                       for k, v in self.players.items()}
        self._build_season(PREVIOUS_ID, "2025", None, completed=17, median=True, shift=3)
        self._build_season(CURRENT_ID, "2026", PREVIOUS_ID, completed=COMPLETED_WEEKS, median=False, shift=0)

    # -- helpers ------------------------------------------------------------
    def _points(self, pid: str, week: int) -> float:
        p = self.players[pid]
        if p["_bye"] == week:
            return 0.0
        pts = self.rng.gauss(p["_talent"], SD[p["position"]] * (0.6 + p["_talent"] / (2.5 * MEAN[p["position"]])))
        return round(max(-2.0, pts), 2)

    def _set_lineup(self, roster: list[str], week: int, skill: float) -> list[str]:
        # Managers rank players by a noisy projection; weaker managers are noisier.
        proj = {pid: (0 if self.players[pid]["_bye"] == week and self.rng.random() > 0.08
                      else self.players[pid]["_talent"] * self.rng.uniform(1 - skill, 1 + skill))
                for pid in roster}
        starters, used = [], set()
        for slot in [s for s in ROSTER_POSITIONS if s != "BN"]:
            options = [pid for pid in roster if pid not in used and self.players[pid]["position"] in SLOT_OK[slot]]
            pick = max(options, key=proj.get) if options else "0"
            starters.append(pick)
            used.add(pick)
        return starters

    def _draft(self, n: int) -> list[list[str]]:
        pools = {pos: sorted([pid for pid, p in self.players.items() if p["position"] == pos],
                             key=lambda pid: -self.players[pid]["_talent"]) for pos in MEAN}
        need = {"QB": 2, "RB": 5, "WR": 5, "TE": 2, "K": 1, "DEF": 1}
        rosters = [[] for _ in range(n)]
        for pos, k in need.items():
            for round_ in range(k):
                order = range(n) if round_ % 2 == 0 else reversed(range(n))
                for t in order:
                    pool = pools[pos]
                    pick = pool.pop(self.rng.randrange(min(3, len(pool))))
                    rosters[t].append(pick)
        return rosters

    def _build_season(self, league_id, season, prev_id, completed, median, shift):
        rng, n = self.rng, 12
        rosters = self._draft(n)
        skill = [rng.uniform(0.15, 0.6) for _ in range(n)]
        sched = _schedule(n, 14, shift)
        users = [{"user_id": f"{700 + i}", "display_name": OWNERS[i],
                  "metadata": {"team_name": TEAM_NAMES[(i + (0 if season == "2026" else 5)) % n]}, "avatar": None}
                 for i in range(n)]
        rostered = {pid for r in rosters for pid in r}
        free_agents = [pid for pid in self.players if pid not in rostered]
        txid = 1
        for week in range(1, 19):
            txs = []
            if 2 <= week <= completed + 1 and week <= 15:
                # Waivers: a few teams swap their worst bench player for a free agent.
                for t in rng.sample(range(n), 3):
                    fa = max(rng.sample(free_agents, 25), key=lambda pid: self.players[pid]["_talent"])
                    pos = self.players[fa]["position"]
                    same = [pid for pid in rosters[t] if self.players[pid]["position"] == pos]
                    if len(same) < 2:
                        continue
                    drop = min(same, key=lambda pid: self.players[pid]["_talent"])
                    rosters[t].remove(drop)
                    rosters[t].append(fa)
                    free_agents.remove(fa)
                    free_agents.append(drop)
                    txs.append({"transaction_id": f"{league_id[-3:]}{txid}", "type": "waiver", "status": "complete",
                                "roster_ids": [t + 1], "adds": {fa: t + 1}, "drops": {drop: t + 1}, "leg": week,
                                "settings": {"waiver_bid": rng.randint(0, 30)}, "draft_picks": [], "creator": users[t]["user_id"]})
                    txid += 1
                if week == 5:
                    a, b = 2, 9
                    pa = max((p for p in rosters[a] if self.players[p]["position"] == "WR"), key=lambda p: self.players[p]["_talent"])
                    pb = max((p for p in rosters[b] if self.players[p]["position"] == "WR"), key=lambda p: self.players[p]["_talent"])
                    rosters[a].remove(pa); rosters[b].remove(pb)
                    rosters[a].append(pb); rosters[b].append(pa)
                    txs.append({"transaction_id": f"{league_id[-3:]}{txid}", "type": "trade", "status": "complete",
                                "roster_ids": [a + 1, b + 1], "adds": {pb: a + 1, pa: b + 1},
                                "drops": {pa: a + 1, pb: b + 1}, "leg": week, "draft_picks": [
                                    {"season": "2027", "round": 3, "roster_id": b + 1, "owner_id": a + 1, "previous_owner_id": b + 1}],
                                "settings": None})
                    txid += 1
            self.routes[f"/league/{league_id}/transactions/{week}"] = txs

            rows = []
            if week <= 14:
                for mid, (a, b) in enumerate(sched[week], start=1):
                    for rid in (a, b):
                        roster = list(rosters[rid - 1])
                        played = week <= completed
                        starters = self._set_lineup(roster, week, skill[rid - 1])
                        pts = {pid: self._points(pid, week) for pid in roster} if played else {}
                        rows.append({
                            "roster_id": rid, "matchup_id": mid, "players": roster, "starters": starters,
                            "players_points": pts,
                            "starters_points": [pts.get(p, 0.0) for p in starters],
                            "points": round(sum(pts.get(p, 0.0) for p in starters), 2), "custom_points": None,
                        })
            elif week <= completed:
                rows = self._playoff_rows(league_id, rosters, skill, week)
            self.routes[f"/league/{league_id}/matchups/{week}"] = rows

        self.routes[f"/league/{league_id}"] = {
            "league_id": league_id, "name": "Demo League (sample data)", "season": season, "sport": "nfl",
            "status": "complete" if completed >= 17 else "in_season", "previous_league_id": prev_id,
            "avatar": None, "total_rosters": n, "roster_positions": ROSTER_POSITIONS,
            "settings": {"playoff_week_start": 15, "playoff_teams": 6, "num_teams": n, "start_week": 1,
                         "league_average_match": 1 if median else 0, "playoff_round_type": 0,
                         "playoff_seed_type": 0, "divisions": 0},
        }
        self.routes[f"/league/{league_id}/users"] = users
        self.routes[f"/league/{league_id}/rosters"] = [
            {"roster_id": i + 1, "owner_id": users[i]["user_id"], "players": rosters[i], "settings": {}}
            for i in range(n)]
        self.routes.setdefault(f"/league/{league_id}/winners_bracket", [])

    def _playoff_rows(self, league_id, rosters, skill, week):
        """Six-team bracket with byes for seeds 1-2 (weeks 15-17)."""
        if week == 15:
            pf = {}
            wins = {}
            for w in range(1, 15):
                games = self.routes[f"/league/{league_id}/matchups/{w}"]
                scores = sorted(r["points"] for r in games)
                med = (scores[5] + scores[6]) / 2
                for r in games:
                    pf[r["roster_id"]] = pf.get(r["roster_id"], 0) + r["points"]
                    wins[r["roster_id"]] = wins.get(r["roster_id"], 0) + (r["points"] > med)
                for r in games:
                    opp = next(o for o in games if o["matchup_id"] == r["matchup_id"] and o is not r)
                    wins[r["roster_id"]] += r["points"] > opp["points"]
            self._seeds = sorted(pf, key=lambda r: (-wins[r], -pf[r]))
            s = self._seeds
            self._bracket = [
                {"r": 1, "m": 1, "t1": s[2], "t2": s[5]}, {"r": 1, "m": 2, "t1": s[3], "t2": s[4]},
                {"r": 2, "m": 3, "t1": s[0], "t2_from": {"w": 2}}, {"r": 2, "m": 4, "t1": s[1], "t2_from": {"w": 1}},
                {"r": 3, "m": 5, "t1_from": {"w": 3}, "t2_from": {"w": 4}, "p": 1},
                {"r": 3, "m": 6, "t1_from": {"l": 3}, "t2_from": {"l": 4}, "p": 3},
            ]
        games = []
        for m in self._bracket:
            if m["r"] != week - 14:
                continue
            t1 = m.get("t1") or self._resolve(m["t1_from"])
            t2 = m.get("t2") or self._resolve(m["t2_from"])
            games.append((m, t1, t2))
        rows = []
        for mid, (m, t1, t2) in enumerate(games, start=1):
            pts_by = {}
            for rid in (t1, t2):
                roster = rosters[rid - 1]
                starters = self._set_lineup(roster, week, skill[rid - 1])
                pts = {pid: self._points(pid, week) for pid in roster}
                pts_by[rid] = round(sum(pts.get(p, 0.0) for p in starters), 2)
                rows.append({"roster_id": rid, "matchup_id": mid, "players": roster, "starters": starters,
                             "players_points": pts, "points": pts_by[rid], "custom_points": None})
            m["w"] = t1 if pts_by[t1] >= pts_by[t2] else t2
            m["l"] = t2 if m["w"] == t1 else t1
        if week == 17:
            self.routes[f"/league/{league_id}/winners_bracket"] = self._bracket
        return rows

    def _resolve(self, src):
        key, mid = next(iter(src.items()))
        match = next(m for m in self._bracket if m["m"] == mid)
        return match[key]

    def transport(self, path: str):
        if path in self.routes:
            return self.routes[path]
        if re.match(r"/league/\d+/(matchups|transactions)/\d+$", path):
            return []
        return None
