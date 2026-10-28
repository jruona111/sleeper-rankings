"""Assemble one season of Sleeper data into a Season object."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .sleeper import SleeperClient

AVATAR_URL = "https://sleepercdn.com/avatars/thumbs/{}"
MAX_WEEK = 18


@dataclass
class Team:
    roster_id: int
    owner_id: str | None
    team_name: str
    owner_name: str
    avatar: str | None
    division: int | None = None

    def to_json(self) -> dict:
        return {
            "roster_id": self.roster_id,
            "owner_id": self.owner_id,
            "team_name": self.team_name,
            "owner_name": self.owner_name,
            "avatar": self.avatar,
            "division": self.division,
        }


@dataclass
class Season:
    league: dict
    teams: dict[int, Team]
    completed_weeks: list[int]
    matchups: dict[int, list[dict]]
    future: dict[int, list[tuple[int, int]]]
    transactions: dict[int, list[dict]] = field(default_factory=dict)
    bracket: list[dict] = field(default_factory=list)

    @property
    def league_id(self) -> str:
        return str(self.league["league_id"])

    @property
    def season(self) -> str:
        return str(self.league.get("season"))

    @property
    def settings(self) -> dict:
        return self.league.get("settings") or {}

    @property
    def roster_positions(self) -> list[str]:
        return self.league.get("roster_positions") or []

    @property
    def playoff_week_start(self) -> int:
        return playoff_week_start(self.league)

    @property
    def last_regular_week(self) -> int:
        return self.playoff_week_start - 1

    @property
    def playoff_teams(self) -> int:
        return int(self.settings.get("playoff_teams") or 0)

    @property
    def median(self) -> bool:
        return bool(self.settings.get("league_average_match"))

    @property
    def num_teams(self) -> int:
        return len(self.teams)

    @property
    def last_completed(self) -> int:
        return self.completed_weeks[-1] if self.completed_weeks else 0

    def is_playoff(self, week: int) -> bool:
        return week >= self.playoff_week_start

    def regular_weeks(self, through: int | None = None) -> list[int]:
        through = self.last_completed if through is None else through
        return [w for w in self.completed_weeks if w <= through and not self.is_playoff(w)]


def playoff_week_start(league: dict) -> int:
    start = int((league.get("settings") or {}).get("playoff_week_start") or 0)
    return start if start > 0 else MAX_WEEK + 1


def final_week(league: dict) -> int:
    settings = league.get("settings") or {}
    start = playoff_week_start(league)
    if start > MAX_WEEK:
        return MAX_WEEK
    teams = int(settings.get("playoff_teams") or 0)
    rounds = max(1, math.ceil(math.log2(teams))) if teams > 1 else 1
    round_type = int(settings.get("playoff_round_type") or 0)
    weeks = rounds * 2 if round_type == 2 else rounds + (1 if round_type == 1 else 0)
    return min(MAX_WEEK, start + weeks - 1)


def last_completed_week(league: dict, state: dict) -> int:
    """Last fully completed fantasy week. Only completed weeks get ranked."""
    end = final_week(league)
    if league.get("status") == "complete" or str(league.get("season")) < str(state.get("season")):
        return end
    if league.get("status") in ("pre_draft", "drafting"):
        return 0
    season_type = state.get("season_type")
    if season_type == "pre":
        return 0
    if season_type in ("post", "off"):
        return end
    return max(0, min(int(state.get("week") or 1) - 1, end))


def build_teams(users: list[dict], rosters: list[dict]) -> dict[int, Team]:
    by_user = {u["user_id"]: u for u in users}
    teams = {}
    for r in rosters:
        rid = int(r["roster_id"])
        user = by_user.get(r.get("owner_id")) or {}
        meta = user.get("metadata") or {}
        owner_name = user.get("display_name") or f"Team {rid}"
        team_name = (meta.get("team_name") or "").strip() or owner_name
        avatar = meta.get("avatar") if str(meta.get("avatar") or "").startswith("http") else None
        if not avatar and user.get("avatar"):
            avatar = AVATAR_URL.format(user["avatar"])
        teams[rid] = Team(
            roster_id=rid,
            owner_id=r.get("owner_id"),
            team_name=team_name,
            owner_name=owner_name,
            avatar=avatar,
            division=(r.get("settings") or {}).get("division"),
        )
    return teams


def pairings(matchups: list[dict]) -> list[tuple[int, int]]:
    groups: dict[int, list[int]] = {}
    for m in matchups:
        if m.get("matchup_id") is not None:
            groups.setdefault(m["matchup_id"], []).append(int(m["roster_id"]))
    return [(g[0], g[1]) for g in groups.values() if len(g) == 2]


def round_robin(roster_ids: list[int], weeks: list[int], offset: int) -> dict[int, list[tuple[int, int]]]:
    """Fallback schedule when Sleeper hasn't published future matchups."""
    ids = sorted(roster_ids)
    if len(ids) % 2:
        ids.append(-1)
    n = len(ids)
    out = {}
    for week in weeks:
        r = (week + offset) % (n - 1)
        rot = [ids[0]] + (ids[1:][-r:] + ids[1:][:-r] if r else ids[1:])
        pairs = [(rot[i], rot[n - 1 - i]) for i in range(n // 2)]
        out[week] = [p for p in pairs if -1 not in p]
    return out


def load_season(client: SleeperClient, league: dict, state: dict, current: bool) -> Season:
    league_id = str(league["league_id"])
    users = client.users(league)
    rosters = client.rosters(league)
    teams = build_teams(users, rosters)
    start = int((league.get("settings") or {}).get("start_week") or 1)
    last = last_completed_week(league, state)
    finished = league.get("status") == "complete"

    matchups: dict[int, list[dict]] = {}
    for week in range(start, last + 1):
        # The most recent week stays on a short TTL so stat corrections are picked up.
        rows = client.matchups(league_id, week, completed=finished or week < last)
        if any((m.get("points") or 0) for m in rows):
            matchups[week] = rows
    completed = sorted(matchups)

    future: dict[int, list[tuple[int, int]]] = {}
    pws = playoff_week_start(league)
    remaining = [w for w in range(max(completed, default=start - 1) + 1, pws) if w <= MAX_WEEK]
    if current and remaining:
        for week in remaining:
            pairs = pairings(client.matchups(league_id, week, completed=False))
            if pairs:
                future[week] = pairs
        missing = [w for w in remaining if w not in future]
        if missing:
            future.update(round_robin(list(teams), missing, offset=0))

    transactions: dict[int, list[dict]] = {}
    if current:
        for week in range(start, min(last + 1, MAX_WEEK) + 1):
            transactions[week] = [
                t for t in client.transactions(league_id, week, completed=finished or week < last)
                if t.get("status") == "complete"
            ]

    bracket = client.winners_bracket(league) if completed and completed[-1] >= pws else []
    return Season(
        league=league,
        teams=teams,
        completed_weeks=completed,
        matchups=matchups,
        future=future,
        transactions=transactions,
        bracket=bracket,
    )


def load_history(client: SleeperClient, league: dict, state: dict, max_seasons: int) -> list[Season]:
    """Previous seasons, newest first, following previous_league_id."""
    seasons = []
    prev = league.get("previous_league_id")
    while prev and prev != "0" and len(seasons) < max_seasons:
        old = client.league(str(prev))
        if not old:
            break
        seasons.append(load_season(client, old, state, current=False))
        prev = old.get("previous_league_id")
    return seasons
