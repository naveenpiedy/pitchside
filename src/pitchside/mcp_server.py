"""MCP server: exposes pitchside to any MCP client (Claude Desktop/Code, Cursor, ...).

Run with `pitchside-mcp` (stdio). The tool logic lives in `pitchside.tools`;
this module only registers it, with short descriptions to keep the tool
manifest cheap, and turns tool errors into MCP tool errors.
"""

from __future__ import annotations

import functools
from collections.abc import Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError
from mcp.types import ToolAnnotations

from pitchside import tools

INSTRUCTIONS = """\
Pitchside: ball-by-ball T20 cricket data (Cricsheet) with computed stats, running locally.
Compute here, then infer: ask the tools for the answer, not for raw rows.

Workflow:
1. overview tells you what is loaded. grammar lists filter keys, split dimensions and metric names.
2. Resolve names first when unsure: find_player / find_team / find_venue (codes like RCB work).
   Pass player_id when a name is ambiguous.
3. Prefer the analysis tools (player_profile, player_splits, net_stats, compare_players, leaderboard,
   team_profile, head_to_head, venue_profile, team_ranking, scorecard). Each takes `filters`, an object
   such as {"season": "2023+", "phase": "death", "bowl_type": "spin"}.
4. For anything else, use query: one read-only SELECT over the v_* views (describe_schema shows them
   and the values a column holds). Aggregate in SQL; rows are capped. For multi-step work, save
   intermediate results with scratch_save and query scratch.<name> later. For bulk data, use export,
   which writes a file and returns only its path.
5. Small samples: prefer the *_shrunk metrics (e.g. net_sr_shrunk) to rank players with uneven balls.
6. Match-winning impact: wpa (win probability added, in wins) is a metric everywhere; mvp and match_impact
   summarise it. net_* measure efficiency vs an average player; wpa measures effect on results.

Net stats (net_*) compare each ball with what an average player in the same competition would produce
in the same situation (over, season, wickets down, setting/chasing, ground, rules era). Positive = better than
average; exp_* is the expectation. Results flag small samples (low < 30 balls, small < 120): treat them
as noise.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITES_FILE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
DELETES = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)
UPDATES_DATA = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

# name: (function, description, annotations)
TOOLS: dict[str, tuple[Callable, str, ToolAnnotations]] = {
    # discovery
    "overview": (tools.overview, "What is loaded: competitions, seasons, match counts, freshness.", READ_ONLY),
    "grammar": (tools.grammar, "Filter keys and value forms, split dimensions, and metric names for the analysis tools.",
                READ_ONLY),
    "describe_schema": (tools.describe_schema,
                        "Tables for `query`. No args: list tables. table: its columns. table + column: the values "
                        "it holds, most common first (check spellings before filtering).", READ_ONLY),
    "find_player": (tools.find_player, "Players matching a name (exact, alias, contains, fuzzy) with ids and activity.",
                    READ_ONLY),
    "find_team": (tools.find_team, "Teams matching a name, old name or code, with their aliases.", READ_ONLY),
    "find_venue": (tools.find_venue, "Grounds matching a name or city, with match counts.", READ_ONLY),
    "find_matches": (tools.find_matches, "Matches by team, opposition, season, venue or date; newest first, with ids.",
                     READ_ONLY),
    # analysis
    "player_profile": (tools.player_profile,
                       "Full profile: overall and by season, phase, pace/spin, bowling subtype, position, "
                       "setting/chasing, plus net stats by season and phase. Batting and bowling.", READ_ONLY),
    "player_splits": (tools.player_splits,
                      "One player's batting or bowling split by any dimensions (by), under filters, with chosen "
                      "metrics.", READ_ONLY),
    "net_stats": (tools.net_stats, "A player's net stats: actual, expected and the difference, split by `by`.",
                   READ_ONLY),
    "compare_players": (tools.compare_players,
                        "Players side by side under identical filters, optionally split by dimensions.", READ_ONLY),
    "leaderboard": (tools.leaderboard,
                    "Players ranked by any metric (incl. net_*), with a ball minimum; `per` gives top N within each "
                    "season/phase/etc. Role inferred for bowling-only metrics.", READ_ONLY),
    "team_profile": (tools.team_profile,
                     "Team results by season and opponent, phase batting/bowling, bowling by type, batting depth.",
                     READ_ONLY),
    "head_to_head": (tools.head_to_head, "Two teams: results by season, phase scoring, top players in the fixture.",
                     READ_ONLY),
    "venue_profile": (tools.venue_profile,
                      "Ground: par first-innings score, chasing and toss record, phase scoring, pace v spin, "
                      "top players.", READ_ONLY),
    "team_ranking": (tools.team_ranking, "Teams ranked by a team metric (e.g. run_rate) for a side and filters.",
                     READ_ONLY),
    "archetypes": (tools.archetypes,
                   "Player archetypes (e.g. Powerplay enforcer, Anchor opener, Middle-overs aggressor, Finisher; "
                   "Death specialist, New-ball seamer, Middle-overs spinner) with the features behind each label. "
                   "summary=true for counts per archetype; archetype='finisher' to filter.", READ_ONLY),
    "prematch_brief": (tools.prematch_brief,
                       "One-call brief before Team A v Team B (optionally at a venue): head to head, recent form, "
                       "venue par and chase record, phase profiles vs league, key players, each side's batters v "
                       "the other's pace/spin mix, and batter-v-bowler head to heads. Recent = last 3 seasons "
                       "unless filters.season is given.", READ_ONLY),
    "mvp": (tools.mvp,
            "Most valuable players by win probability added (WPA, in wins): batting + bowling impact on the "
            "result, under any filters (e.g. a season, a team, playoffs).", READ_ONLY),
    "match_impact": (tools.match_impact,
                     "One match: win probability over by over, the players who swung it most, and the biggest "
                     "single-ball swings.", READ_ONLY),
    "scorecard": (tools.scorecard, "Match scorecard; optionally partnerships and over-by-over.", READ_ONLY),
    # SQL
    "query": (tools.query,
              "One read-only SELECT (DuckDB SQL) over the v_* views and per-competition tables. Returns at most "
              "`limit` rows (default 50, max 1000) and says if more exist. Aggregate here rather than fetching rows.",
              READ_ONLY),
    "export": (tools.export,
               "Run a SELECT and write the full result to a local CSV/Parquet file; returns only path, row count "
               "and columns. Use for bulk data you or the user will process elsewhere.", WRITES_FILE),
    # scratch space
    "scratch_save": (tools.scratch_save,
                     "Save one SELECT's result as scratch.<name> for multi-step analysis; it persists across calls "
                     "and `query` can read and join it. Replaces a table of the same name. Returns a 5-row preview.",
                     WRITES_FILE),
    "scratch_list": (tools.scratch_list, "Saved scratch tables with size, source SQL, and whether they are stale.",
                     READ_ONLY),
    "scratch_drop": (tools.scratch_drop, "Delete a scratch table.", DELETES),
    # data
    "sync": (tools.sync,
             "Download new matches from Cricsheet for ongoing competitions (e.g. last night's game), refresh "
             "derived stats, and report data-quality checks. Only when asked or when data is clearly stale.",
             UPDATES_DATA),
}


def _as_tool_errors(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except tools.ToolError as err:
            raise MCPToolError(str(err)) from err

    return wrapper


def build_server() -> MCPServer:
    server = MCPServer(name="pitchside", instructions=INSTRUCTIONS)
    for name, (fn, description, annotations) in TOOLS.items():
        server.tool(name=name, description=description, annotations=annotations)(_as_tool_errors(fn))
    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
