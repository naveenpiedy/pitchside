# AGENTS.md

Guidance for AI coding agents working on **pitchside**: a local cricket analytics engine over Cricsheet ball-by-ball data, stored in DuckDB and (later) exposed to AI clients via MCP.

## Commands

```bash
uv sync                                   # install (Python 3.12, managed by uv)
uv run pytest                             # all tests; integration test skips if IPL data isn't loaded
uv run pitchside load ipl                 # download + build db/ipl.duckdb (idempotent)
uv run pitchside register                 # Cricsheet people register -> db/registry.duckdb
uv run pitchside info
uv run pitchside scorecard ipl 1426312 -p
uv run pitchside attributes fetch         # Wikipedia -> data/player_attributes.csv -> registry
uv run pitchside splits Kohli --by bowl_type,phase -f season=2023+
uv run pitchside filters                  # filter keys, dimensions, metrics
```

On Windows, set `PYTHONIOENCODING=utf-8` when printing DuckDB tables (player names include non-cp1252 characters).

## Layout

- `src/pitchside/etl/parse.py` — one pass per match JSON. **All ball-order state is derived here** (score/wickets before the ball, phase, batting position, batter's running score, balls remaining, runs required). Don't reconstruct it later with window functions.
- `src/pitchside/etl/load.py` — inserts via pyarrow; skips matches already loaded at the same `meta.revision`, replaces revised ones; bumps `meta.data_version` and writes `ingest_log`.
- `src/pitchside/model/schema.sql` — core tables. `derive.py` — precomputed tables (`batting_innings`, `bowling_innings`, `season_batting`, `season_bowling`), rebuilt in full after every load.
- `src/pitchside/etl/attributes.py` — batting hand / bowling style: Cricsheet register Cricinfo id -> Wikidata (P2697) -> Wikipedia infobox. Cricinfo itself returns 403 to scripts.
- `src/pitchside/engine.py` — `open_engine()` ATTACHes every loaded competition DB + registry read-only into an in-memory DuckDB and builds temp views `v_balls`, `v_bat` (striker balls + non-striker dismissals), `v_bowl`. v_bat / v_bowl share column names so one filter means the same in both roles.
- `src/pitchside/stats/filters.py` — the filter grammar (CLI `-f k=v` and, later, MCP args). Always parameterised SQL.
- `src/pitchside/stats/splits.py` — `aggregate()`: every split / leaderboard / comparison is this with different dimensions, filters and ordering. Metric catalogues carry a "higher is better" flag used for ranking.
- `aliases.toml` — canonical team (franchise, current name) and venue (one per ground) names, plus team codes. The engine builds `team_alias` / `venue_alias` temp tables from it. `v_team` (team facts, each ball once per side) and `v_results` (one row per match per team; `innings_no` = 1 if that team batted first) sit beside v_bat / v_bowl. Roles `team_batting` / `team_bowling` in `aggregate` read v_team.
- `src/pitchside/stats/teams.py` — team profile, head-to-head, venue profile, team ranking. Results tables only take match-level filters (`MATCH_FILTERS`); other filters are reported in the table notes, never silently dropped.
- `src/pitchside/stats/players.py` — profile, splits, leaderboard, compare; each wrapped in `cache.cached`. Analyses return `Table` (`stats/table.py`) so the same functions back the CLI and the MCP tools.
- `tournaments.toml` — tracked competitions; one DuckDB file per key (`db/<key>.duckdb`).

## Conventions

- Player identity is the Cricsheet registry id (`player_id`), never the name. Display names come from `Engine.names`, which suffixes the id when two players share a name. Resolve user-typed names with `Engine.resolve_player` (exact primary name > clear activity front-runner > unique alias; else `PlayerNotFound` with candidates).
- Teams: `team` / `opposition` columns are canonical franchises; `team_name` / `opposition_name` are the names used that season. Filters resolve names, old names and codes through `Engine.resolve_team`; venues through `Engine.resolve_venue`. Add new renames or grounds to `aliases.toml`, not to code.
- Records and milestones (highest scores, best figures, fastest fifties) are deliberately not a feature: they are one-off SQL over `batting_innings`, `bowling_innings` and `deliveries` (`batter_runs_before` / `batter_balls_before` give balls-to-milestone).
- Dimension names must not collide with metric names (that is why the setting/chasing dimension is `setting_chasing`, not `innings`).
- Every result reports sample size; `aggregate` adds a `sample` column (`low` < 30 balls, `small` < 120).
- Cache keys include each competition's `data_version`, so loads invalidate automatically; `pitchside cache clear` wipes it.
- `data/player_attributes.csv`: rows with `source` other than `wikipedia` are never overwritten by a fetch.
- Super overs are `innings_no >= 3` with `is_super_over`; derived/stat tables exclude them.
- Phases (T20): powerplay overs 1-6, middle 7-15, death 16-20. Shortened innings scale those proportions, and the powerplay length comes from Cricsheet's recorded mandatory powerplay when present. `innings.allocated_balls` is the innings' real allocation: a match shortened before the start (target overs < match overs, no DLS method) reduces both innings.
- Use `season_year` (the year a season was played), not the raw `season` label (`2007/08` = IPL 2008).
- Cricket rules encoded in `parse.py`: wides aren't balls faced; no-balls are faced but not legal; byes, leg-byes and penalties aren't charged to the bowler; retired hurt / retired not out isn't a wicket; `non_boundary` 4s/6s aren't boundaries.
- Integer `sum()` in DuckDB returns HUGEINT; cast to `BIGINT` in derived tables.
- Comments are sparse and explain *why*; match the surrounding style.

## Design principles

- **Compute locally, return conclusions.** Stats and (future) MCP tools aggregate in DuckDB and return small results with sample sizes; raw rows are opt-in.
- True stats (Jarrod Kimber / Good Areas style) are symmetric for batters and bowlers, scored against per-ball expected runs and dismissals.
- v1 is T20 only; anything format-specific (phases, baselines) must be keyed by format.

## Tests

- `tests/conftest.py` holds `MINI_MATCH`, a hand-built match covering the edge cases above. Add a case there when you add a rule.
- `mini_engine` (conftest) builds an Engine over MINI_MATCH with a temp registry and cache; use it for engine/stat tests.
- `@pytest.mark.integration` tests reconcile IPL data with published records (season leaders, 2024 final) and check that `aggregate` totals equal the precomputed season tables for every player-season; keep them passing after any parse/derive/engine change.

## Data

Never commit `data/`, CSVs, zips or `*.duckdb` (all gitignored). Databases are rebuildable with `pitchside load`. Batting hand / bowling style live in `data/player_attributes.csv` (hand-editable, local).
