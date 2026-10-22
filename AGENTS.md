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
```

On Windows, set `PYTHONIOENCODING=utf-8` when printing DuckDB tables (player names include non-cp1252 characters).

## Layout

- `src/pitchside/etl/parse.py` — one pass per match JSON. **All ball-order state is derived here** (score/wickets before the ball, phase, batting position, batter's running score, balls remaining, runs required). Don't reconstruct it later with window functions.
- `src/pitchside/etl/load.py` — inserts via pyarrow; skips matches already loaded at the same `meta.revision`, replaces revised ones; bumps `meta.data_version` and writes `ingest_log`.
- `src/pitchside/model/schema.sql` — core tables. `derive.py` — precomputed tables (`batting_innings`, `bowling_innings`, `season_batting`, `season_bowling`), rebuilt in full after every load.
- `src/pitchside/stats/` — analyses. Return plain dicts/lists so the same functions back the CLI and the MCP tools.
- `tournaments.toml` — tracked competitions; one DuckDB file per key (`db/<key>.duckdb`).

## Conventions

- Player identity is the Cricsheet registry id (`player_id`), never the name. Names come from `players`.
- Super overs are `innings_no >= 3` with `is_super_over`; derived/stat tables exclude them.
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
- `@pytest.mark.integration` tests reconcile IPL data with published records (season leaders, 2024 final); keep them passing after any parse/derive change.

## Data

Never commit `data/`, CSVs, zips or `*.duckdb` (all gitignored). Databases are rebuildable with `pitchside load`. Batting hand / bowling style live in `data/player_attributes.csv` (hand-editable, local).
