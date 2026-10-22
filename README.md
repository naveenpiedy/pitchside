# pitchside

Local cricket analytics engine over [Cricsheet](https://cricsheet.org) ball-by-ball data, stored in DuckDB (one database per competition). Feature plan: true stats, splits, MCP access for AI clients, auto-sync.

## Setup

```bash
uv sync
uv run pitchside load ipl        # downloads the IPL zip on first run, builds db/ipl.duckdb
uv run pitchside register        # Cricsheet people register -> db/registry.duckdb
uv run pitchside info
uv run pitchside scorecard ipl 1426312 --partnerships
uv run pytest
```

Competitions are listed in `tournaments.toml`. Batting hand and bowling style (not in Cricsheet) go in `data/player_attributes.csv`.

## Layout

- `src/pitchside/etl/` — download, parse (one pass per match, derives ball state), load (idempotent, revision-aware), register
- `src/pitchside/model/` — `schema.sql` core tables; `derive.py` precomputed scorecard and season tables
- `src/pitchside/stats/` — analyses (`scorecard.py` so far)
