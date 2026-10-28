"""Sleeper API v1 client with a SQLite cache.

Caching policy
- Matchups and transactions for completed weeks never change -> cached forever.
- League/users/rosters of a finished season -> cached forever; otherwise 1 hour.
- /players/nfl (~5 MB) -> refreshed at most once per day.
- /state/nfl -> 10 minutes.
- Weekly player stats (/stats/nfl/regular/...) for finished weeks -> forever.
- Drafts, draft picks and draft traded picks of a completed draft -> forever.

The same SQLite file also stores computed history (weekly rankings, blurbs).
"""
from __future__ import annotations

import json
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any, Callable

import requests

BASE_URL = "https://api.sleeper.app/v1"
HOUR = 3600
DAY = 24 * HOUR

Transport = Callable[[str], Any]


def http_transport(path: str) -> Any:
    """GET BASE_URL + path with retries. Returns parsed JSON (may be None)."""
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            resp = requests.get(BASE_URL + path, timeout=30)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} for {path}")
            resp.raise_for_status()
            return resp.json() if resp.content else None
        except (requests.RequestException, ValueError) as err:
            last_err = err
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Sleeper API request failed: {path}: {last_err}")


class Cache:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path))
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS http_cache (
                key TEXT PRIMARY KEY,
                fetched_at REAL NOT NULL,
                permanent INTEGER NOT NULL DEFAULT 0,
                body BLOB
            );
            CREATE TABLE IF NOT EXISTS rankings_history (
                league_id TEXT NOT NULL,
                week INTEGER NOT NULL,
                roster_id INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                score REAL NOT NULL,
                composite REAL NOT NULL,
                PRIMARY KEY (league_id, week, roster_id)
            );
            CREATE TABLE IF NOT EXISTS blurbs (
                facts_hash TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        self.db.commit()

    # -- raw HTTP responses -------------------------------------------------
    def get(self, key: str, max_age: float | None) -> tuple[bool, Any]:
        row = self.db.execute(
            "SELECT fetched_at, permanent, body FROM http_cache WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return False, None
        fetched_at, permanent, body = row
        if not permanent and max_age is not None and time.time() - fetched_at > max_age:
            return False, None
        return True, json.loads(zlib.decompress(body))

    def put(self, key: str, value: Any, permanent: bool) -> None:
        body = zlib.compress(json.dumps(value, separators=(",", ":")).encode())
        self.db.execute(
            "INSERT OR REPLACE INTO http_cache (key, fetched_at, permanent, body) VALUES (?, ?, ?, ?)",
            (key, time.time(), int(permanent), body),
        )
        self.db.commit()

    # -- computed history ---------------------------------------------------
    def save_rankings(self, league_id: str, week: int, rows: list[dict]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO rankings_history VALUES (?, ?, ?, ?, ?, ?)",
            [(league_id, week, r["roster_id"], r["rank"], r["score"], r["composite"]) for r in rows],
        )
        self.db.commit()

    def load_rankings(self, league_id: str) -> dict[int, dict[int, dict]]:
        out: dict[int, dict[int, dict]] = {}
        for week, rid, rank, score, comp in self.db.execute(
            "SELECT week, roster_id, rank, score, composite FROM rankings_history WHERE league_id = ?",
            (league_id,),
        ):
            out.setdefault(week, {})[rid] = {"rank": rank, "score": score, "composite": comp}
        return out

    def get_blurb(self, facts_hash: str) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT text, source FROM blurbs WHERE facts_hash = ?", (facts_hash,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def put_blurb(self, facts_hash: str, text: str, source: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO blurbs VALUES (?, ?, ?, ?)",
            (facts_hash, text, source, time.time()),
        )
        self.db.commit()


class SleeperClient:
    def __init__(self, cache: Cache, transport: Transport = http_transport):
        self.cache = cache
        self.transport = transport
        self.fetches = 0

    def _get(self, path: str, max_age: float | None = None, permanent: bool = False,
             keep_empty: bool = False) -> Any:
        hit, value = self.cache.get(path, max_age)
        if hit:
            return value
        value = self.transport(path)
        self.fetches += 1
        # Never cache an empty response permanently unless the caller knows it is final
        # (e.g. a week of a finished season with no transactions): it may just not exist yet.
        self.cache.put(path, value, permanent=permanent and (bool(value) or (keep_empty and value is not None)))
        return value

    def state(self) -> dict:
        return self._get("/state/nfl", max_age=600)

    def players(self) -> dict:
        return self._get("/players/nfl", max_age=DAY)

    def league(self, league_id: str) -> dict | None:
        path = f"/league/{league_id}"
        league = self._get(path, max_age=HOUR)
        if league and league.get("status") == "complete":
            self.cache.put(path, league, permanent=True)
        return league

    def _season_scoped(self, league: dict, suffix: str) -> Any:
        done = league.get("status") == "complete"
        return self._get(f"/league/{league['league_id']}/{suffix}", max_age=HOUR, permanent=done)

    def users(self, league: dict) -> list[dict]:
        return self._season_scoped(league, "users") or []

    def rosters(self, league: dict) -> list[dict]:
        return self._season_scoped(league, "rosters") or []

    def winners_bracket(self, league: dict) -> list[dict]:
        return self._season_scoped(league, "winners_bracket") or []

    def matchups(self, league_id: str, week: int, completed: bool) -> list[dict]:
        return self._get(f"/league/{league_id}/matchups/{week}", max_age=HOUR, permanent=completed) or []

    def transactions(self, league_id: str, week: int, completed: bool, final: bool = False) -> list[dict]:
        return self._get(f"/league/{league_id}/transactions/{week}", max_age=HOUR, permanent=completed,
                         keep_empty=final) or []

    def traded_picks(self, league: dict) -> list[dict]:
        """Current owners of traded future draft picks."""
        return self._season_scoped(league, "traded_picks") or []

    def drafts(self, league: dict) -> list[dict]:
        return self._season_scoped(league, "drafts") or []

    def _draft_scoped(self, draft: dict, suffix: str) -> Any:
        done = draft.get("status") == "complete"
        return self._get(f"/draft/{draft['draft_id']}{suffix}", max_age=HOUR, permanent=done, keep_empty=done)

    def draft(self, draft: dict) -> dict:
        """Full draft object (adds slot_to_roster_id)."""
        return self._draft_scoped(draft, "") or draft

    def draft_picks(self, draft: dict) -> list[dict]:
        return self._draft_scoped(draft, "/picks") or []

    def draft_traded_picks(self, draft: dict) -> list[dict]:
        return self._draft_scoped(draft, "/traded_picks") or []

    def stats(self, season: str, week: int, completed: bool) -> dict | None:
        """Weekly player stats (unofficial endpoint). None if it fails."""
        try:
            data = self._get(f"/stats/nfl/regular/{season}/{week}", max_age=6 * HOUR, permanent=completed)
        except RuntimeError as err:
            print(f"  ! stats {season} week {week} unavailable: {err}")
            return None
        return data if isinstance(data, dict) else None
