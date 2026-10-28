# CLAUDE.md

Sleeper fantasy football power rankings: a Python pipeline writes JSON; a static, no-build front end reads it; GitHub Actions runs it weekly and deploys to GitHub Pages.

## Layout

```
config.yaml                 league_id, ranking weights, decay, simulations, blurb model, dynasty model, trade valuation
requirements.txt
pipeline/
  run.py                    entry point (python -m pipeline); orchestrates everything, writes site/data/*
  config.py                 loads config.yaml (LEAGUE_ID env var overrides league_id)
  sleeper.py                Sleeper API v1 client + SQLite cache (pipeline/cache.db, gitignored)
  league.py                 Season/Team model; completed-week logic; schedule; previous_league_id chain
  lineup.py                 optimal lineup (Hungarian algorithm) -> Max PF; biggest lineup mistake
  metrics.py                TeamWeek per team-week; season-to-date metrics; remaining SOS
  rankings.py               z-scores -> weighted composite -> 0-100 score; rank change + "driver" metric
  playoffs.py               Monte Carlo playoff/bye/#1 seed/title odds
  extras.py                 weekly awards, transaction notes, all-time records + head-to-head
  storylines.py             per-team "facts" object for each week's blurb
  dynasty.py                Future Rankings: per-player 3-year projections, projected lineups, pick value
  trades.py                 full trade history; draft-pick ownership chains and outcomes
  trade_value.py            preseason labels, weekly replacement level, PAR, direct + trade-tree value per trade
  blurbs.py                 Claude blurbs (structured JSON output) with template fallback; cached by facts hash
  backtest.py               week-N ranks predicting week-N+1 winners; python -m pipeline.backtest
  demo.py                   synthetic two-season league in Sleeper API shapes (--demo)
  tests/                    pytest: lineup optimizer, metrics, dynasty model, pick chains, trade value/trees, end-to-end demo build
site/
  index.html, css/style.css, js/app.js   vanilla JS SPA with hash routes; Chart.js 4.4.1 from cdnjs
  manifest.webmanifest, icons/           Add to Home Screen
  data/                     generated JSON (committed by the workflow)
.github/workflows/update.yml   Tue 15:00 UTC cron + manual + push; test, run, commit data, deploy Pages
.claude/launch.json         local static server for previews (port 8765)
```

## Commands

```bash
pip install -r requirements.txt
python -m pytest -q pipeline/tests          # unit + end-to-end tests (no network)
python -m pipeline                          # real league (config.yaml / LEAGUE_ID), writes site/data
python -m pipeline --demo                   # synthetic league, no network, in-memory cache
python -m pipeline --no-llm                 # force template blurbs
python -m pipeline --out /tmp/data          # write JSON elsewhere
python -m pipeline backfill --from 1 --to 7 # full build; marks weeks 1-7 as backfilled (no write-ups); safe to rerun
python -m pipeline.backtest [--demo]        # print weight-set accuracy table
python -m http.server 8765 --directory site # view the site
```

`ANTHROPIC_API_KEY` enables Claude blurbs. Without it (or on any API error) blurbs fall back to templates; the build never fails because of blurbs.

## Data rules and conventions

- **Completed weeks**: current season -> `state.week - 1` during the regular season (Sleeper bumps `week` by Tuesday); finished seasons -> through the final playoff week. Weeks with no points are dropped; `settings.start_week` is respected.
- **Caching** (`sleeper.py`): completed weeks' matchups/transactions are cached forever, except the most recent completed week (1-hour TTL so stat corrections land next run). Finished-season league/users/rosters cached forever. `/players/nfl` refreshed at most daily. `/state/nfl` 10 min.
- **Max PF**: exact max-weight assignment of rostered players (`matchup.players`) to starting slots from `roster_positions` (BN/IR/TAXI/RES ignored). Eligibility: FLEX=RB/WR/TE, WRRB_FLEX=WR/RB, REC_FLEX=WR/TE, SUPER_FLEX=QB/RB/WR/TE, IDP_FLEX=DL/LB/DB. Player positions = `fantasy_positions` ∪ `position`; alphabetic IDs are team defenses. A slot may stay empty rather than start a negative scorer. Max PF is floored at actual PF.
- **Records/win %/all-play/luck**: regular season only. Median leagues (`league_average_match`) add a W/L vs. the weekly median. Ties = half win. Byes (null `matchup_id`) give no H2H result.
- **Luck** = (wins + ties/2) − expected wins, where an H2H game's expectation is the week's all-play win fraction and a median game's expectation is its actual result.
- **PF-based metrics** (PF, Max PF, efficiency, form, consistency) include playoff weeks.
- **Recent form** = Σ PF·decay^weeks_ago / Σ decay^weeks_ago.
- **Power score**: per-metric z-score (population SD) across teams, weighted sum / Σ|weights| = composite; score = 100·Φ(composite / SD(composite)). Ties broken by PF. All weeks are recomputed each run (changing weights re-scores history) and saved to `rankings_history` in cache.db.
- **Driver**: metric whose weighted contribution changed most vs. last week, in the direction the team moved.
- **Playoff sim**: Normal(mean, sd) per team, both shrunk toward league values with 2 pseudo-games. Remaining schedule from Sleeper's future-week matchups (round-robin fallback). Seeding: division winners first (if divisions), then wins, then PF. Bracket: byes to top seeds, fixed seeding unless `playoff_seed_type == 1`. Once in the playoffs, the real `winners_bracket` is simulated forward.
- **Blurbs**: one Claude call per week (all uncached teams) with JSON-schema output, `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`). Cache key = sha256(facts + model + PROMPT_VERSION); bump `PROMPT_VERSION` in blurbs.py to regenerate.
- **Owner identity across seasons** = Sleeper `owner_id`; orphan rosters fall back to `"<season>:<roster_id>"`.
- **Backfill**: every build ranks every completed week from data through that week only (week N never sees week N+1). `backfill --from A --to B` runs the normal build and marks weeks A..B `backfilled`: ranked and charted, but no blurbs (`blurb`/`blurb_source` null) and no Claude call. The mark lives in the committed `rankings/week-N.json` (`"backfilled": true`) and every later build carries it over, so CI keeps it. To un-backfill a week, delete its JSON and rebuild.
- **Weekly player stats** (`/stats/nfl/regular/{season}/{week}`, unofficial): previous NFL season weeks 1–18 + current completed weeks; cached forever once final. A failed week is skipped (rostered players' matchup points still count). Points = Σ `scoring_settings[k]·stat[k]`, falling back to `pts_ppr`; a rostered player's actual matchup points win when present.
- **Trades**: transaction rounds 0–18 of every season (current season: 0 through `state.week`; offseason trades are filed in round 1), type `trade`, status `complete`, deduped by `transaction_id`. A pick is keyed `(season, round, original roster_id)`; its chain = every trade's `draft_picks` entry with that key, ordered by `status_updated`. Draft outcome: `/league/{id}/drafts` → `/draft/{id}` (`slot_to_roster_id`) + `/draft/{id}/picks`; a pick's original roster = `slot_to_roster_id[draft_slot]` (traded picks keep the original slot), falling back to the draft's `traded_picks` when unambiguous. Outcome `made` (player + who used it), `unused` (that season's draft is complete without it) or `pending` (owner = chain end, overridden by `/draft/{id}/traded_picks` and `/league/{id}/traded_picks`). Team names are the current team for the roster_id.

## Trade value (trade_value.py; parameters in config.yaml `trades:`)

- **Preseason**: week 1 start = midnight US Eastern on Sleeper's `state.season_start_date` (current NFL season), else `trades.week1_dates[season]`, else the Thursday after Labor Day. A trade more than `preseason_days` (7) days before that is `"Preseason {season}"`; within those days it is `"Week 1"`; after it, its `leg`.
- **Weekly replacement level** (every completed week, all seasons, playoffs included) per position group (IDP positions grouped DL/LB/DB): pool = teams × (dedicated starting slots + each flex slot's share), share = that flex slot's league-wide starter positions over the season (`replacement.flex_usage: season`, or `week`; even split across eligible positions without usage), × `pool_scale`. Replacement = the score of the player ranked `round(pool) + 1` that week among every NFL player (weekly stats under league scoring, rostered players' matchup points taking priority); 0 if the pool runs out.
- **Weekly PAR** = max(0, points − replacement), only in weeks the player was in that team's starting lineup (`matchup.starters`). Start pts = his points in those weeks.
- **Holding** (direct value): a received player counts from the trade's week (`leg`, at least the season's start week) through the week of the first later transaction that drops him from that roster (drop, waiver swap or trade; inclusive, but only weeks he is in that roster's matchup `players`). A received pick counts 0 if flipped before the draft (first later trade with `previous_owner_id` = the team); if the team used it, the drafted player is held from the draft (`start_time`; that season's start week) the same way. All transactions of every season (rounds 0–18) are read for exits.
- **Verdict** (per mode): the side with the most PAR wins by (its PAR − next best); "even" if that rounds to 0.0; `early` ("Too early to tell") if all sides combined have fewer than `min_started_weeks` (4) started player-weeks. Net (per side) = own − mean of the other sides (sums to 0 across a trade).
- **Trade tree**: start from the side's received assets (root node, share 1). When a held asset (player, pick, or the player drafted with it) leaves in a later trade, that trade becomes one child node (all tree assets sent in the same trade share one node); the team's received assets there are held with the node's share and followed the same way, in time order. Share = Σ(tree fraction × weight) / Σ weight over everything the team sent in that trade, weight = the asset's PAR over the `recent_par_weeks` (4) weeks before the trade, started or not (picks 0); all weights 0 → even split by count. Node value = share × its assets' value; tree total = Σ nodes. Drops end a branch. Each holding belongs to one node, so no production is counted twice.

## Future Rankings model (dynasty.py; parameters in config.yaml `dynasty:`)

Years = the next three seasons (current season + 1, or the current season if the league is `pre_draft`/`drafting`). Per player:
- **Games**: weeks with `gp`/`off_snp`/`gms_active` > 0. Snap share = `off_snp / tm_off_snp`; opportunities = `rec_tgt` + `rb_rush_weight·rush_att` (RB only).
- **Recency weights**: game i of n (chronological) gets `recency_decay^(n-1-i)`; W = Σ weights. PPG, snap level and opportunities/game are weighted means.
- **Replacement level** (per position) = PPG of the player ranked `round(1.5 · teams · max(1, dedicated slots))` among players with ≥ 4 games.
- **Shrink**(x) = (W·x + k·repl) / (W + k), k = `prior_games`. A player with no games projects at replacement level.
- **Points component** = shrink(PPG).
- **Young** (QB/RB/WR/TE) = age ≤ `young_age` or years_exp ≤ `young_years_exp`. Young RB/WR/TE: base = 0.6·points + 0.2·snaps + 0.2·opportunity (`blend`); young QB: 0.8·points + 0.2·snaps (`qb_blend`); everyone else: base = points. Missing components drop out and the weights renormalize.
  - snaps = shrink(a + b·clamp(snap + `snap_trend_weight`·trend, 0, 1)); opportunity = shrink(a + b·opp/game), where (a, b) is a least-squares fit of PPG on that measure over the position's regulars (≥ 4 games, snap share ≥ 20%).
  - trend = mean snap share of the last 3 games − the 3 before (latest season if it has ≥ 4 games with snaps).
- **Opportunity adjustment** `adj` (sum, clamped to ±`max_total`): for depth_chart_order 2 behind the order-1 player at the same NFL team/depth position: + `behind_injured_starter` if the starter is Out/IR/PUP/Doubtful/Sus; + `behind_aging_starter` if the starter's age ≥ his position's `cliff_age`; otherwise + `behind_young_starter` (negative) if the starter is ≤ 26 with snap share ≥ 65%. Plus clamp(`snap_trend`·trend, ±`snap_trend_cap`) with ≥ 4 snap games. "Rising" = young and adj ≥ `rising_threshold`.
- **Age curve**: age on Sept 1 (birth_date, else `age` + years). Each year from age a to a+1: × (1+growth) if a+1 ≤ peak_age; × 1 if a+1 ≤ cliff_age; else × (1−decline); compounded. K/DEF: no curve. "Past cliff" = age > cliff_age.
- **Projection** for year k (0-based) = base · age_mult(k+1 years) · (1 + adj·`year_ramp[k]`) · (`no_team_factor` if no NFL team).

Per team and year: score = optimal lineup (lineup.optimal_lineup over the roster's `players`, slots from `roster_positions`) on projected PPG + Σ `pick_values[round]` for owned picks with season ≤ year. Owned picks = each roster's own picks for the projected seasons × `draft_rounds`, reassigned by `/league/{id}/traded_picks`. Ranks per year; Dynasty = Σ `year_weights`·score / Σ weights. Top 3 = best projected starters (Dynasty: by weighted projection). Aging core = that year's top 5 starters past their cliff (Dynasty: year 1).

## JSON schemas (site/data)

All numbers are floats rounded to 2 dp (percent-type fields to 4 dp, 0–1 scale) unless noted. Roster IDs are ints in values, strings as object keys.

### meta.json
```jsonc
{
  "generated_at": "2026-10-27T15:02:11+00:00",
  "demo": false,
  "league": { "league_id": "…", "name": "…", "season": "2026", "avatar": "url|null", "status": "in_season",
              "num_teams": 12, "playoff_teams": 6, "playoff_week_start": 15, "median_scoring": false,
              "roster_positions": ["QB", "RB", "…"] },
  "nfl_state": { "season": "2026", "week": 9, "season_type": "regular" },
  "weeks": [1, 2, 3],               // ranked (completed) weeks
  "latest_week": 3,                 // 0 before week 1 is complete
  "backfilled_weeks": [1, 2],       // ranked weeks without write-ups
  "teams": { "<roster_id>": Team },
  "weights": { "pf": 0.3, "max_pf": 0.25, "win_pct": 0.25, "efficiency": 0.15, "recent_form": 0.05 },
  "decay": 0.85,
  "metric_labels": { "pf": "points per game", "…": "…" },
  "blurb_source": "anthropic" | "template"
}
// Team = { roster_id, owner_id, team_name, owner_name, avatar: url|null, division: int|null }
```

### rankings/week-N.json
```jsonc
{ "week": 8, "backfilled": false, "rankings": [ {
    "rank": 1, "prev_rank": 2 | null, "change": 1,      // positive = moved up
    "roster_id": 4, "score": 92.9, "composite": 1.21,
    "z": { "pf": 1.4, "…": 0 },
    "driver": { "metric": "win_pct", "label": "win percentage", "delta": 0.08, "z_before": 0.5, "z_after": 0.9 } | null,
    "metrics": SeasonMetrics,   // season-to-date through week N
    "week": TeamWeek | null,    // this week's game
    "blurb": "…" | null, "blurb_source": "anthropic" | "template" | null,   // null on backfilled weeks
    "facts": { … }              // storyline facts given to the blurb writer
} ] }
```
`SeasonMetrics`: `games, wins, losses, ties, win_pct, pf (per game), pf_total, pa (per game), pa_total, max_pf (per game), max_pf_total, efficiency, recent_form, consistency (SD), allplay_w, allplay_l, allplay_t, allplay_pct, expected_wins, luck, sos (opponents' PPG), bench_total, bench_pg, high, low, streak ("W3"), last3 [pts], worst_mistake: Mistake|null`.

`TeamWeek`: `week, pf, max_pf, bench_pts, efficiency, opponent (roster_id|null), pa|null, result ("W"|"L"|"T"|null), median_result|null, allplay [w,l,t], playoff (bool), mistake: Mistake|null`.

`Mistake`: `slot, benched, benched_points, started (player_id|null), started_points, gain` (+ `benched_name, started_name, week` where shown).

### teams.json
`{ "<roster_id>": Team & { season: SeasonMetrics|null, remaining_sos: float|null, weekly: [TeamWeek & {rank, score}], schedule: [{week, opponent, pf?, pa?, result?, playoff?, future}], h2h: [{opponent, w, l, t, pf, pa, games: [{season, week, pf, pa, result, playoff}]}], alltime: AllTime|null } }`

### playoffs.json
```jsonc
{ "status": "regular_season" | "playoffs" | "complete" | "preseason", "as_of_week": 8, "simulations": 10000,
  "remaining_weeks": [9, 10], "playoff_teams": 6, "byes": 2, "median_games": false,
  "teams": [ { "roster_id": 1, "playoff_pct": 94.1, "bye_pct": 60.2, "first_seed_pct": 35.3, "champ_pct": 26.9,
               "proj_wins": 8.8, "avg_seed": 2.7, "seed_dist": [35.3, …],   // percents, 0-100
               "mean": 147.0, "sd": 16.2, "record": [5, 3, 0] } ] }
```

### awards.json
`{ "weeks": { "<week>": [ { key, title, roster_id, opponent?, value, detail } ] } }` — keys: `high_score, low_score, blowout, closest, unluckiest, best_manager, worst_mistake`.

### transactions.json
`{ "best_pickups": [{week, roster_id, player_id, player, type, faab, points, started_points, weeks}], "trades": [{week, transaction_id, leader, margin, sides: [{roster_id, received: [{player_id, player, points, started_points}], picks: ["2027 round 3"], started_points}]}] }` — points count only weeks the player was on that roster from the transaction week on.

### future.json
```jsonc
{ "years": [2027, 2028, 2029], "year_weights": [0.5, 0.3, 0.2], "pick_values": {"1": 4.0, "…": 0}, "age_curves": {},
  "replacement": {"RB": 9.7}, "young_age": 25, "young_years_exp": 3, "stats_weeks": 25, "as_of": {"season": "2026", "week": 7},
  "teams": [ {                                   // sorted by dynasty rank
    "roster_id": 4, "power_rank": 1 | null,
    "views": { "2027": { "score", "rank", "lineup_pts", "picks_pts", "top": [PlayerRef], "aging": [PlayerRef] },
               "dynasty": { "score", "rank", "top": [PlayerRef], "aging": [PlayerRef] } },
    "rising": [{ "player_id", "name", "pos", "age", "reasons": ["backup to aging X (30)"] }],
    "picks": [{ "season": "2027", "round": 1, "original": 4 }],
    "players": [{ "player_id", "name", "pos", "nfl_team", "age", "young", "games", "ppg", "base",
                  "components": {"points", "snaps?", "opportunity?"}, "adj", "reasons",
                  "proj": [y1, y2, y3], "starter": [bool, bool, bool] }] } ] }
// PlayerRef = { player_id, name, pos, age (in that year), points }
```

### trades.json
```jsonc
{ "seasons": ["2026", "2025"],
  "trades": [ {                                  // newest first
    "transaction_id": "…", "season": "2026", "week": 1, "time": 1787180305682,   // ms epoch; offseason trades are week 1
    "roster_ids": [2, 4],
    "sides": [ { "roster_id": 2, "faab": 0,
      "players": [{ "player_id", "name", "pos" }],
      "picks": [{ "season": "2026", "round": 1, "original": 4,
                  "later": [{ "transaction_id", "time", "from", "to" }],   // later trades of this pick
                  "outcome": { "status": "made", "player_id", "player", "pos", "roster_id", "pick_no" }
                           | { "status": "pending", "owner": 3 } | { "status": "unused" } }] } ] } ] }
```

Trade value fields (added to each trade card above):
```jsonc
{ "label": "Preseason 2026" | "Week 5", "preseason": true, "week": 5 | null,
  "verdict": { "direct": Verdict, "tree": Verdict },
  // Verdict = { "status": "win", "roster_id", "margin", "started_weeks" } | { "status": "even", … } | { "status": "early", "started_weeks" }
  "sides": [ { …,
    "value": { "direct": Value, "tree": Value },   // Value = { start_pts, par, started_weeks, net_par, net_start_pts }
    "players"/"picks": [ { …, "value": { start_pts, par, started_weeks, weeks, end: End, drafted?: {player_id, player, pos} } } ],
    "tree": [ Node ] } ] }
// End = { how: "held" | "dropped" | "traded" | "pending" | "lost", season?, week?, transaction_id?, to?: [roster_id] }
// Node = { transaction_id, time, season, with: [roster_id], share (0-1), gave: [label], gave_other: [label],
//          assets: [{ label, kind: "player"|"pick", pos, drafted, start_pts, par, started_weeks, end }],
//          start_pts, par (share-weighted), children: [Node] }
```
Top level also has `min_started_weeks`, `recent_par_weeks`, `replacement` and `week1` (`{season: "YYYY-MM-DD"}`).

### history.json
`{ "seasons": [{season, league_id, name, champion_owner, complete}], "alltime": [AllTime] }` where `AllTime = {owner_id, name, team_name, seasons: [..], wins, losses, ties, win_pct, pf, pa, playoff_wins, playoff_losses, championships}`. (Head-to-head lives per team in teams.json.)

### backtest.json
`{ "seasons": ["2026", "2025"], "results": [{ name, weights, correct, games, accuracy (0-1|null), by_season: [{season, correct, games, accuracy}] }] }` — first entry is always "Current weights".

## Front end notes

- Hash routes: `#/rankings[/week]`, `#/odds`, `#/awards[/week]`, `#/stats[/week]`, `#/team/<roster_id>`, `#/future[/<year>|/dynasty]`, `#/future/team/<roster_id>`, `#/trades[/<transaction_id>]` (scrolls to and highlights that card, clearing filters that would hide it), `#/trades/leaderboard`.
- Trades tab state (team / season / week incl. Preseason / sort: newest, biggest PAR margin, most total PAR / mode: Direct value vs Trade tree) lives in `store.tradeFilter`. Team summary card and Trade Leaderboard are computed client-side from trades.json in the selected mode; W-L = PAR verdicts excluding "too early"; best/worst = highest/lowest net PAR among trades with a verdict.
- Backfilled weeks (`blurb: null`) render without a blurb paragraph.
- Look: "poster" style. Archivo (Google Fonts, variable width; condensed 75% for big numerals and headings), square corners, heavy ink rules instead of cards/shadows, boxed tabs, solid movement tags (`.move`). Rankings rows (`.prow`) show a 2-digit rank, rounded-square team picture, score, and a rank-by-week sparkline (`spark()`).
- Colors are CSS tokens on `:root`; dark mode via `prefers-color-scheme` plus a `data-theme` override saved in localStorage. Charts read tokens at render time and re-render on theme change.
- Designed at 380px first. Check new UI at phone width with no horizontal page scroll (only `.table-wrap` scrolls sideways).
- All user-provided strings (team/player names) go through `esc()`.
