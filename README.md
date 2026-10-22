# pitchside

Local cricket analytics engine over [Cricsheet](https://cricsheet.org) ball-by-ball data, stored in DuckDB (one database per competition). Feature plan: true stats, splits, MCP access for AI clients, auto-sync.

## Setup

```bash
uv sync
uv run pitchside load ipl        # downloads the IPL zip on first run, builds db/ipl.duckdb
uv run pitchside register        # Cricsheet people register -> db/registry.duckdb
uv run pitchside info
uv run pitchside scorecard ipl 1426312 --partnerships
uv run pitchside attributes fetch  # batting hand / bowling style from Wikipedia -> data/player_attributes.csv
uv run pytest
```

## Analysis

```bash
uv run pitchside player Kohli -f season=2022+                          # profile
uv run pitchside splits Kohli --by bowl_subtype,phase -f season=2023+  # any split, any filters
uv run pitchside compare "SA Yadav" Klaasen Pooran -f bowl_type=spin
uv run pitchside compare Bumrah Rashid -r bowling --by batting_hand
uv run pitchside leaders economy -f phase=death -f season=2025
uv run pitchside leaders runs --per season -n 1
uv run pitchside filters        # the filter grammar, dimensions and metrics
uv run pitchside find Sharma    # name lookup when a name is ambiguous
```

Filters are `-f key=value` (lists `a,b`, ranges `2022-2025`, `2020+`), the same grammar the MCP tools will take.
Results flag small samples (`low` < 30 balls, `small` < 120) and are cached until new data is loaded.

Competitions are listed in `tournaments.toml`. Batting hand and bowling style (not in Cricsheet) live in `data/player_attributes.csv`: fetched from Wikipedia, hand-editable (set `source` to `manual` so a refetch never overwrites it, then `pitchside attributes reload`). `pitchside attributes missing` lists the gaps that matter most.

## Layout

- `src/pitchside/etl/` — download, parse (one pass per match, derives ball state), load (idempotent, revision-aware), register, attributes (Wikipedia batting hand / bowling style)
- `src/pitchside/model/` — `schema.sql` core tables; `derive.py` precomputed scorecard and season tables
- `src/pitchside/engine.py` — in-memory DuckDB over all competition DBs + registry; views `v_balls`, `v_bat`, `v_bowl`; player name resolution
- `src/pitchside/stats/` — `filters.py` (filter grammar), `splits.py` (aggregation engine), `players.py` (profile, splits, leaderboard, compare), `cache.py`, `table.py`, `scorecard.py`
