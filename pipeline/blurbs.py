"""Movement write-ups: Claude when ANTHROPIC_API_KEY is set, templates otherwise.

Blurbs are cached in SQLite by a hash of the facts, so a rerun with the same
data never calls the API again and past weeks keep their write-ups.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

from .sleeper import Cache

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """You write the weekly movement blurbs for a fantasy football league's power rankings site.

For each team you get a JSON "facts" object. Write 1-2 sentences (max ~45 words) explaining why the team moved up, moved down, or held steady this week, in a fun, light trash-talk tone - the kind of ribbing league-mates enjoy, never mean-spirited, no profanity, nothing about real people beyond their fantasy team.

Accuracy rules:
- Use only the facts provided. Do not invent players, scores, opponents, injuries, or events.
- Any number you mention must appear in the facts (you may round to one decimal).
- "luck_wins" is actual wins minus expected wins from all-play; positive means lucky.
- "biggest_ranking_factor" is the metric that moved their power score most.
- If movement is "new", it is the first ranking of the season: describe where they start.
- Refer to teams by their team name. Vary sentence openings across teams."""

SCHEMA = {
    "type": "object",
    "properties": {
        "blurbs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "roster_id": {"type": "integer"},
                    "blurb": {"type": "string"},
                },
                "required": ["roster_id", "blurb"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["blurbs"],
    "additionalProperties": False,
}


def facts_hash(facts: dict, model: str) -> str:
    raw = json.dumps(facts, sort_keys=True) + model + PROMPT_VERSION
    return hashlib.sha256(raw.encode()).hexdigest()


# -- templates ---------------------------------------------------------------

def _pick(options: list[str], seed: str) -> str:
    return options[int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(options)]


def template_blurb(f: dict) -> str:
    name = f["team"]
    seed = f"{name}{f['week']}"
    tw = f.get("this_week") or {}
    driver = f.get("biggest_ranking_factor")
    spots = abs(f["rank_change"])
    s_spots = "spot" if spots == 1 else "spots"

    if f["movement"] == "new":
        first = _pick([
            f"{name} opens the season at No. {f['rank']} with a {f['power_score']:.0f} power score.",
            f"{name} kicks things off at No. {f['rank']}, and the rest of the league has been warned.",
        ], seed)
    elif f["movement"] == "up":
        why = f" on the back of better {driver['metric']}" if driver and driver["effect"] == "helped" else ""
        first = _pick([
            f"{name} climbs {spots} {s_spots} to No. {f['rank']}{why}.",
            f"Up {spots} {s_spots} to No. {f['rank']}: {name} is heating up{why}.",
        ], seed)
    elif f["movement"] == "down":
        why = f", with {driver['metric']} doing the damage" if driver and driver["effect"] == "hurt" else ""
        first = _pick([
            f"{name} slides {spots} {s_spots} to No. {f['rank']}{why}.",
            f"Down {spots} {s_spots} to No. {f['rank']} for {name}{why}.",
        ], seed)
    else:
        first = _pick([
            f"{name} holds at No. {f['rank']} ({f['record']}).",
            f"No movement for {name}, parked at No. {f['rank']} with a {f['record']} record.",
        ], seed)

    # Candidate angles for sentence two, strongest first; pick one of the top two.
    angles: list[list[str]] = []
    teams = tw.get("teams", 12)
    if tw.get("league_high_score"):
        angles.append([f"Their {tw['points']} points led the whole league this week.",
                       f"Week-high {tw['points']} points - everyone else was playing for second."])
    if tw.get("league_low_score"):
        angles.append([f"Their {tw['points']} points were the lowest in the league. Yikes.",
                       f"Dead last in scoring with {tw['points']}; the group chat noticed."])
    if tw.get("result") == "loss" and tw.get("weekly_score_rank", 99) <= max(2, teams // 3):
        angles.append([f"Scoring {tw['points']} and still losing to {tw['opponent']} is just cruel.",
                       f"{tw['points']} points would've beaten {tw.get('would_have_beaten', 'most teams')}, but not {tw['opponent']}."])
    if tw.get("result") == "win" and tw.get("weekly_score_rank", 0) > (teams * 2) // 3:
        angles.append([f"A win with just {tw['points']} points over {tw['opponent']}? Buy a lottery ticket.",
                       f"Escaped {tw['opponent']} with only {tw['points']} points. Lucky, lucky."])
    if tw.get("lineup_mistake"):
        angles.append([f"Bonus points lost: they {tw['lineup_mistake']}.",
                       f"Lineup tip: they {tw['lineup_mistake']}. Ouch.",
                       f"Someone check the start/sit logic - they {tw['lineup_mistake']}."])
    if f.get("transactions_this_week"):
        angles.append([f"Front office was busy: {f['transactions_this_week'][0]}."])
    if tw.get("result") in ("win", "loss"):
        verb = "beat" if tw["result"] == "win" else "fell to"
        angles.append([f"They {verb} {tw['opponent']} {tw['points']}–{tw['opponent_points']}."])
    if abs(f.get("luck_wins", 0)) >= 1:
        angles.append(["The schedule gods have been generous so far." if f["luck_wins"] > 0
                       else "The schedule has been brutal so far."])
    second = _pick(_pick(angles[:2], seed + "a"), seed + "b") if angles else ""
    return f"{first} {second}".strip()


# -- Claude ------------------------------------------------------------------

def _llm_blurbs(batch: dict[int, dict], model: str, effort: str) -> dict[int, str]:
    import anthropic

    client = anthropic.Anthropic()
    payload = [{"roster_id": rid, "facts": f} for rid, f in batch.items()]
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{
            "role": "user",
            "content": "Write one blurb per team from these facts:\n" + json.dumps(payload, indent=1),
        }],
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"blurb request stopped: {response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    out = {}
    for item in json.loads(text)["blurbs"]:
        blurb = " ".join(str(item.get("blurb", "")).split())
        if 0 < len(blurb) <= 400 and int(item["roster_id"]) in batch:
            out[int(item["roster_id"])] = blurb
    return out


def write_blurbs(
    facts_by_week: dict[int, dict[int, dict]],
    cache: Cache,
    model: str,
    effort: str,
    llm_weeks: set[int],
    use_llm: bool = True,
) -> tuple[dict[int, dict[int, tuple[str, str]]], str]:
    """Returns ({week: {roster_id: (blurb, source)}}, overall_source)."""
    have_key = use_llm and bool(os.environ.get("ANTHROPIC_API_KEY"))
    out: dict[int, dict[int, tuple[str, str]]] = {}
    used_llm = False
    for week, teams in sorted(facts_by_week.items()):
        out[week] = {}
        pending: dict[int, dict] = {}
        for rid, f in teams.items():
            cached = cache.get_blurb(facts_hash(f, model))
            if cached and (cached[1] == "anthropic" or not (have_key and week in llm_weeks)):
                out[week][rid] = cached
            elif have_key and week in llm_weeks:
                pending[rid] = f
            else:
                out[week][rid] = (template_blurb(f), "template")
        if pending:
            try:
                written = _llm_blurbs(pending, model, effort)
            except Exception as err:  # never fail the build over blurbs
                print(f"  ! Claude blurbs failed for week {week}: {err}; using templates", file=sys.stderr)
                written = {}
            for rid, f in pending.items():
                if rid in written:
                    out[week][rid] = (written[rid], "anthropic")
                    cache.put_blurb(facts_hash(f, model), written[rid], "anthropic")
                    used_llm = True
                else:
                    out[week][rid] = (template_blurb(f), "template")
    if used_llm or any(src == "anthropic" for wk in out.values() for _, src in wk.values()):
        return out, "anthropic"
    return out, "template"
