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
uv run pitchside true Kohli --by season -f season=2023+                # true stats: actual, expected, difference
uv run pitchside leaders true_sr -f season=2026 --min-balls 200        # any true metric ranks too
uv run pitchside leaders true_economy -f phase=death --min-balls 120
uv run pitchside baselines ipl -t bat_runs -p death                    # inspect the situation factors
uv run pitchside team RCB -f season=2024+                              # team profile
uv run pitchside h2h CSK MI                                            # head-to-head
uv run pitchside venue Chinnaswamy -f season=2022+                     # venue profile
uv run pitchside team-rank run_rate --side bowling -f phase=death      # teams ranked
uv run pitchside filters        # the filter grammar, dimensions and metrics
uv run pitchside find Sharma    # name lookup when a name is ambiguous
```

Filters are `-f key=value` (lists `a,b`, ranges `2022-2025`, `2020+`), the same grammar the MCP tools will take.
Results flag small samples (`low` < 30 balls, `small` < 120) and are cached until new data is loaded.

**True stats** compare every ball with what an average player in the same competition would produce in the same situation: the over's level that season, adjusted for wickets down and setting/chasing within each rules era (`rule_eras` in `tournaments.toml`, e.g. IPL 2023 for the Impact Player). Positive is always better than average, and league totals sum to zero. Batting: `true_sr`, `true_avg`, `true_runs`, `outs_saved`, `true_dot_pct`, `true_boundary_pct`. Bowling: `true_economy`, `runs_saved`, `true_wickets`, `true_avg`, `true_sr`, `true_dot_pct`, `true_boundary_pct`. Each comes with its `exp_*` counterpart. After changing `rule_eras`, run `pitchside rebuild <comp>`.

Teams and grounds that appear under several names are unified in `aliases.toml`. Renamed franchises count as one, under their current name (Delhi Daredevils seasons are Delhi Capitals), and short codes such as `RCB` or `KXIP` work anywhere a team is expected.

Competitions are listed in `tournaments.toml`. Batting hand and bowling style (not in Cricsheet) live in `data/player_attributes.csv`: fetched from Wikipedia, hand-editable (set `source` to `manual` so a refetch never overwrites it, then `pitchside attributes reload`). `pitchside attributes missing` lists the gaps that matter most.

## Keeping it current

```bash
uv run pitchside sync                    # new matches for every "ongoing" competition, then quality checks
uv run pitchside check                   # data-quality checks on demand
uv run pitchside schedule install --at 09:00   # daily Windows Task Scheduler job (per user, no admin)
uv run pitchside schedule status
uv run pitchside schedule remove
```

`sync` fetches Cricsheet's 30-day recently-added zip (or a competition's full zip if it hasn't synced for 25+ days), loads only matches from tracked competitions (idempotent; revised matches are replaced), rebuilds derived tables and true-stat baselines, refreshes the player register and fetches attributes for new players, then runs the quality checks. It exits non-zero if a check fails. The scheduled job appends to `data/logs/sync.log`, and runs late if the machine was off at the scheduled time. Competitions marked `status = "ended"` in `tournaments.toml` are never re-synced.

Quality checks: innings totals vs deliveries, scorecard runs + extras vs totals, chase targets, wicket and ball limits, winners, registry ids, true-stat baselines zero-sum (all `fail`); plus `warn` for team/venue names not yet in `aliases.toml` and players missing batting hand or bowling style.

## Using it from an AI (MCP)

`pitchside-mcp` is an MCP server on stdio. Calculation happens locally; the AI gets small finished tables back.

- **Claude Code**: `.mcp.json` in this folder registers it automatically (approve it when prompted).
- **Claude Desktop / other clients**: add to the client's MCP config:

```json
{
  "mcpServers": {
    "pitchside": {
      "command": "uv",
      "args": ["--directory", "C:/Users/Snorlax/PycharmProjects/cricket_analysis", "run", "pitchside-mcp"]
    }
  }
}
```

Tools: `overview`, `grammar`, `describe_schema`, `find_player` / `find_team` / `find_venue` / `find_matches`; analysis: `player_profile`, `player_splits`, `true_stats`, `compare_players`, `leaderboard`, `team_profile`, `head_to_head`, `venue_profile`, `team_ranking`, `scorecard`; SQL: `query` (one read-only SELECT, rows capped) and `export` (full result to `data/exports/`, only the path comes back); data: `sync` (pull new matches on request).

The SQL connection can't touch the filesystem or change settings, and each call opens and closes the databases, so `pitchside load` works while a client is connected.

## Layout

- `src/pitchside/etl/` — download, parse (one pass per match, derives ball state), load (idempotent, revision-aware), register, attributes (Wikipedia batting hand / bowling style)
- `src/pitchside/model/` — `schema.sql` core tables; `derive.py` precomputed scorecard and season tables
- `src/pitchside/tools.py` — the AI-facing tool functions (compact text in, compact text out); `mcp_server.py` registers them as MCP tools
- `src/pitchside/engine.py` — in-memory DuckDB over all competition DBs + registry; views `v_balls`, `v_bat`, `v_bowl`; player name resolution
- `src/pitchside/stats/` — `filters.py` (filter grammar), `splits.py` (aggregation engine), `players.py` (profile, splits, leaderboard, compare), `teams.py` (team, head-to-head, venue, team ranking), `cache.py`, `table.py`, `scorecard.py`
