"""Load config.yaml with defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
SITE_DATA = ROOT / "site" / "data"
CACHE_PATH = Path(__file__).resolve().parent / "cache.db"

DEFAULT_WEIGHTS = {
    "pf": 0.30,
    "max_pf": 0.25,
    "win_pct": 0.25,
    "efficiency": 0.15,
    "recent_form": 0.05,
}


@dataclass
class Config:
    league_id: str
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    decay: float = 0.85
    simulations: int = 10000
    random_seed: int | None = 42
    history_seasons: int = 10
    blurb_model: str = "claude-opus-5-5"
    blurb_effort: str = "medium"
    blurb_weeks: str = "all"


def load_config(path: Path = CONFIG_PATH) -> Config:
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    league_id = str(os.environ.get("LEAGUE_ID") or raw.get("league_id") or "").strip()
    weights = dict(DEFAULT_WEIGHTS)
    weights.update({k: float(v) for k, v in (raw.get("weights") or {}).items()})
    blurbs = raw.get("blurbs") or {}
    return Config(
        league_id=league_id,
        weights=weights,
        decay=float(raw.get("decay", 0.85)),
        simulations=int(raw.get("simulations", 10000)),
        random_seed=raw.get("random_seed", 42),
        history_seasons=int(raw.get("history_seasons", 10)),
        blurb_model=str(blurbs.get("model", "claude-opus-5-5")),
        blurb_effort=str(blurbs.get("effort", "medium")),
        blurb_weeks=str(blurbs.get("weeks", "all")),
    )


def league_id_is_set(league_id: str) -> bool:
    return bool(league_id) and league_id.isdigit()
