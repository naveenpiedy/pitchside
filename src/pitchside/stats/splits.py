"""The splits engine: aggregate batting or bowling facts by any dimensions, under any filters.

Everything player-facing (splits, profiles, leaderboards, comparisons) is a call
to `aggregate` with different dimensions, filters and ordering.
"""

from __future__ import annotations

from pitchside.engine import Engine
from pitchside.stats.filters import FilterError, compile_filters
from pitchside.stats.table import Table

DIMENSIONS: dict[str, str] = {
    "season": "season_year",
    "competition": "competition",
    "phase": "phase",
    "over": "over_no + 1",
    "setting_chasing": "CASE innings_no WHEN 1 THEN 'setting' ELSE 'chasing' END",
    "bowl_type": "bowl_type",
    "bowl_subtype": "coalesce(bowl_subtype, 'unknown')",
    "bowling_arm": "coalesce(bowling_arm, 'unknown')",
    "batting_hand": "coalesce(batting_hand, 'unknown')",
    "position": "position",
    "order_group": ("CASE WHEN position <= 3 THEN '1-3 top' WHEN position <= 7 THEN '4-7 middle' "
                    "ELSE '8-11 lower' END"),
    "team": "team",
    "opposition": "opposition",
    "team_name": "team_name",
    "opposition_name": "opposition_name",
    "venue": "venue",
    "city": "city",
    "stage": "stage",
    "ball_bucket": ("CASE WHEN nth_ball <= 5 THEN '01-05' WHEN nth_ball <= 10 THEN '06-10' "
                    "WHEN nth_ball <= 20 THEN '11-20' WHEN nth_ball <= 30 THEN '21-30' ELSE '31+' END"),
    "wickets_down": "wickets_before",
    "player": "player_id",
    "bowler": "bowler_id",
    "batter": "batter_id",
}
ID_DIMENSIONS = {"player", "bowler", "batter"}
# Dimensions with a natural order; the rest sort by volume (balls), biggest first.
ORDERED = {
    "season": "season", "over": "over", "position": "position", "ball_bucket": "ball_bucket",
    "order_group": "order_group",
    "phase": "CASE phase WHEN 'powerplay' THEN 1 WHEN 'middle' THEN 2 ELSE 3 END",
    "setting_chasing": "min(innings_no)", "wickets_down": "wickets_down",
}

BATTING_METRICS: dict[str, tuple[str, bool]] = {  # name: (sql, higher_is_better)
    "innings": ("count(DISTINCT match_id || ':' || innings_no)", True),
    "runs": ("sum(runs)", True),
    "balls": ("sum(faced)", True),
    "outs": ("sum(out)", False),
    "average": ("round(sum(runs) / nullif(sum(out), 0), 2)", True),
    "strike_rate": ("round(100.0 * sum(runs) / nullif(sum(faced), 0), 2)", True),
    "dot_pct": ("round(100.0 * sum(dot) / nullif(sum(faced), 0), 1)", False),
    "boundary_pct": ("round(100.0 * (sum(fours) + sum(sixes)) / nullif(sum(faced), 0), 1)", True),
    "balls_per_boundary": ("round(sum(faced) / nullif(sum(fours) + sum(sixes), 0), 1)", False),
    "fours": ("sum(fours)", True),
    "sixes": ("sum(sixes)", True),
    # True stats: actual vs what an average batter would have produced on the same balls
    # (model/baselines.py). Positive true_* is always better than average.
    "exp_runs": ("round(sum(exp_runs), 1)", True),
    "true_runs": ("round(sum(runs) - sum(exp_runs), 1)", True),
    "exp_sr": ("round(100.0 * sum(exp_runs) / nullif(sum(faced), 0), 2)", True),
    "true_sr": ("round(100.0 * (sum(runs) - sum(exp_runs)) / nullif(sum(faced), 0), 2)", True),
    "exp_avg": ("round(sum(exp_runs) / nullif(sum(exp_out), 0), 2)", True),
    "true_avg": ("round(sum(runs) / nullif(sum(out), 0) - sum(exp_runs) / nullif(sum(exp_out), 0), 2)", True),
    "exp_outs": ("round(sum(exp_out), 1)", False),
    "outs_saved": ("round(sum(exp_out) - sum(out), 1)", True),
    "true_dot_pct": ("round(100.0 * (sum(exp_dot) - sum(dot)) / nullif(sum(faced), 0), 1)", True),
    "true_boundary_pct": ("round(100.0 * (sum(fours) + sum(sixes) - sum(exp_bnd)) / nullif(sum(faced), 0), 1)", True),
}
BOWLING_METRICS: dict[str, tuple[str, bool]] = {
    "innings": ("count(DISTINCT match_id || ':' || innings_no)", True),
    "balls": ("sum(balls)", True),
    "runs": ("sum(runs)", False),
    "wickets": ("sum(wickets)", True),
    "economy": ("round(6.0 * sum(runs) / nullif(sum(balls), 0), 2)", False),
    "average": ("round(sum(runs) / nullif(sum(wickets), 0), 2)", False),
    "strike_rate": ("round(sum(balls) / nullif(sum(wickets), 0), 1)", False),
    "dot_pct": ("round(100.0 * sum(dot) / nullif(sum(balls), 0), 1)", True),
    "boundary_pct": ("round(100.0 * (sum(fours) + sum(sixes)) / nullif(sum(balls), 0), 1)", False),
    "fours": ("sum(fours)", False),
    "sixes": ("sum(sixes)", False),
    "wides": ("sum(wides)", False),
    "noballs": ("sum(noballs)", False),
    # True stats from the bowler's side: positive = better than an average bowler on the same balls.
    "exp_runs": ("round(sum(exp_runs), 1)", False),
    "runs_saved": ("round(sum(exp_runs) - sum(runs), 1)", True),
    "exp_economy": ("round(6.0 * sum(exp_runs) / nullif(sum(balls), 0), 2)", False),
    "true_economy": ("round(6.0 * (sum(exp_runs) - sum(runs)) / nullif(sum(balls), 0), 2)", True),
    "exp_wickets": ("round(sum(exp_wkts), 1)", True),
    "true_wickets": ("round(sum(wickets) - sum(exp_wkts), 1)", True),
    "exp_avg": ("round(sum(exp_runs) / nullif(sum(exp_wkts), 0), 2)", False),
    "true_avg": ("round(sum(exp_runs) / nullif(sum(exp_wkts), 0) - sum(runs) / nullif(sum(wickets), 0), 2)", True),
    "exp_sr": ("round(sum(balls) / nullif(sum(exp_wkts), 0), 1)", False),
    "true_sr": ("round(sum(balls) / nullif(sum(exp_wkts), 0) - sum(balls) / nullif(sum(wickets), 0), 1)", True),
    "true_dot_pct": ("round(100.0 * (sum(dot) - sum(exp_dot)) / nullif(sum(balls), 0), 1)", True),
    "true_boundary_pct": ("round(100.0 * (sum(exp_bnd) - sum(fours) - sum(sixes)) / nullif(sum(balls), 0), 1)", True),
}
# Team-level facts (v_team): runs include extras, wickets are all dismissals.
# The same numbers are good for the batting side and bad for the bowling side.
_TEAM_SQL = {
    "innings": "count(DISTINCT match_id || ':' || innings_no)",
    "runs": "sum(runs)",
    "balls": "sum(balls)",
    "wickets": "sum(wickets)",
    "run_rate": "round(6.0 * sum(runs) / nullif(sum(balls), 0), 2)",
    "average": "round(sum(runs) / nullif(sum(wickets), 0), 2)",
    "balls_per_wicket": "round(sum(balls) / nullif(sum(wickets), 0), 1)",
    "dot_pct": "round(100.0 * sum(dot) / nullif(sum(balls), 0), 1)",
    "boundary_pct": "round(100.0 * (sum(fours) + sum(sixes)) / nullif(sum(balls), 0), 1)",
    "extras": "sum(extras)",
    "fours": "sum(fours)",
    "sixes": "sum(sixes)",
}
_BATTING_SIDE_GOOD = {"innings", "runs", "balls", "run_rate", "average", "balls_per_wicket",
                      "boundary_pct", "extras", "fours", "sixes"}
TEAM_BATTING_METRICS = {m: (sql, m in _BATTING_SIDE_GOOD) for m, sql in _TEAM_SQL.items()}
TEAM_BOWLING_METRICS = {m: (sql, m not in _BATTING_SIDE_GOOD or m in ("innings", "balls"))
                        for m, sql in _TEAM_SQL.items()}

DEFAULT_METRICS = {
    "batting": ["innings", "runs", "balls", "outs", "average", "strike_rate", "dot_pct", "boundary_pct"],
    "bowling": ["innings", "balls", "runs", "wickets", "economy", "average", "strike_rate", "dot_pct", "boundary_pct"],
    "team_batting": ["innings", "runs", "balls", "wickets", "run_rate", "average", "dot_pct", "boundary_pct"],
    "team_bowling": ["innings", "runs", "balls", "wickets", "run_rate", "average", "dot_pct", "boundary_pct"],
}
RATE_METRICS = {"average", "strike_rate", "dot_pct", "boundary_pct", "balls_per_boundary", "economy",
                "run_rate", "balls_per_wicket", "exp_sr", "true_sr", "exp_avg", "true_avg", "exp_economy",
                "true_economy", "true_dot_pct", "true_boundary_pct"}
# Shown for true stats: actual, expected, difference.
TRUE_METRICS = {
    "batting": ["innings", "balls", "runs", "exp_runs", "true_runs", "strike_rate", "exp_sr", "true_sr",
                "average", "exp_avg", "true_avg"],
    "bowling": ["innings", "balls", "wickets", "exp_wickets", "true_wickets", "economy", "exp_economy",
                "true_economy", "runs_saved", "average", "exp_avg", "true_avg"],
}

# role: (source view, extra predicate, metric catalogue)
ROLES = {
    "batting": ("v_bat", "TRUE", BATTING_METRICS),
    "bowling": ("v_bowl", "TRUE", BOWLING_METRICS),
    "team_batting": ("v_team", "side = 'batting'", TEAM_BATTING_METRICS),
    "team_bowling": ("v_team", "side = 'bowling'", TEAM_BOWLING_METRICS),
}
TEAM_ROLES = {"team_batting", "team_bowling"}

# Sample-size flags, by balls.
LOW_SAMPLE, SMALL_SAMPLE = 30, 120


def role_metrics(role: str) -> dict[str, tuple[str, bool]]:
    if role not in ROLES:
        raise FilterError(f"role must be one of {', '.join(ROLES)}, got {role!r}")
    return ROLES[role][2]


def aggregate(
    engine: Engine,
    role: str,
    filters: dict | None = None,
    by: list[str] | tuple[str, ...] = (),
    players: list[str] | None = None,
    metrics: list[str] | None = None,
    min_balls: int = 0,
    order_by: str | None = None,
    limit: int | None = None,
    per: str | None = None,
    title: str = "",
) -> Table:
    """Aggregate v_bat / v_bowl.

    by        dimensions to group on (see DIMENSIONS)
    players   restrict to these player ids
    order_by  a metric to rank on (direction is the metric's "better" direction)
    per       with order_by + limit: top `limit` within each value of this dimension
    """
    catalogue = role_metrics(role)
    metrics = list(metrics or DEFAULT_METRICS[role])
    for m in metrics + ([order_by] if order_by else []):
        if m not in catalogue:
            raise FilterError(f"Unknown {role} metric {m!r}; choose from {', '.join(catalogue)}")
    dims = list(by) + ([per] if per and per not in by else [])
    for d in dims:
        if d not in DIMENSIONS:
            raise FilterError(f"Unknown dimension {d!r}; choose from {', '.join(DIMENSIONS)}")
    if role in TEAM_ROLES and ("player" in dims or players):
        raise FilterError(f"{role} is team-level: no 'player' dimension or players")

    src, side, _ = ROLES[role]
    balls_sql = catalogue["balls"][0]
    where, params = compile_filters(filters, engine.resolve_player, engine.resolve_team)
    where = f"{side} AND {where}"
    if players:
        where += f" AND player_id IN ({', '.join('?' * len(players))})"
        params += list(players)

    select = [f"{DIMENSIONS[d]} AS {d}" for d in dims]
    if "player" in dims:
        select.append("arg_max(team_name, match_date) AS team")
    select += [f"{catalogue[m][0]} AS {m}" for m in metrics]
    if "balls" not in metrics:
        select.append(f"{balls_sql} AS _balls")
    sql = f"SELECT {', '.join(select)} FROM {src} WHERE {where}"
    if dims:
        sql += " GROUP BY " + ", ".join(DIMENSIONS[d] for d in dims)
    if min_balls:
        sql += f" HAVING {balls_sql} >= ?"
        params.append(min_balls)

    if order_by:
        direction = "DESC" if catalogue[order_by][1] else "ASC"
        rank = f"{catalogue[order_by][0]} {direction} NULLS LAST, {balls_sql} DESC"
        if per and limit:
            sql += f" QUALIFY row_number() OVER (PARTITION BY {DIMENSIONS[per]} ORDER BY {rank}) <= ?"
            params.append(limit)
            sql += f" ORDER BY {ORDERED.get(per, per)}, {rank}"
        else:
            sql += f" ORDER BY {rank}"
            if limit:
                sql += " LIMIT ?"
                params.append(limit)
    elif dims:
        keys = [ORDERED[d] for d in dims if d in ORDERED]
        sql += " ORDER BY " + ", ".join(keys + [f"{balls_sql} DESC"])
        if limit:
            sql += " LIMIT ?"
            params.append(limit)

    cur = engine.con.execute(sql, params)
    columns = [c[0] for c in cur.description]
    rows = [list(r) for r in cur.fetchall()]
    if not dims:  # an ungrouped aggregate over nothing still yields one all-NULL row
        rows = [r for r in rows if any(v not in (None, 0) for v in r)]

    for d in dims:
        if d in ID_DIMENSIONS:
            i = columns.index(d)
            for r in rows:
                r[i] = engine.name(r[i])

    balls_col = columns.index("balls") if "balls" in columns else columns.index("_balls")
    notes = [] if rows else ["no data for these filters"]
    if any(r[balls_col] is not None and r[balls_col] < SMALL_SAMPLE for r in rows):
        columns.append("sample")
        for r in rows:
            b = r[balls_col] or 0
            r.append("low" if b < LOW_SAMPLE else "small" if b < SMALL_SAMPLE else "")
        notes.append(f"sample: low < {LOW_SAMPLE} balls, small < {SMALL_SAMPLE} balls")
    if "_balls" in columns:
        i = columns.index("_balls")
        columns.pop(i)
        for r in rows:
            r.pop(i)
    if any(d in ("bowl_type", "bowl_subtype", "bowling_arm", "batting_hand") for d in dims) or \
            any(k in (filters or {}) for k in ("bowl_type", "bowl_subtype", "bowling_arm", "batting_hand")):
        notes.append("bowling style / batting hand from player_attributes; 'unknown' = not classified")
    return Table(columns=columns, rows=rows, title=title, notes=notes)
