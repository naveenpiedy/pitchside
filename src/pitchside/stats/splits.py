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
    "team": "team",
    "opposition": "opposition",
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
}
DEFAULT_METRICS = {
    "batting": ["innings", "runs", "balls", "outs", "average", "strike_rate", "dot_pct", "boundary_pct"],
    "bowling": ["innings", "balls", "runs", "wickets", "economy", "average", "strike_rate", "dot_pct", "boundary_pct"],
}
RATE_METRICS = {"average", "strike_rate", "dot_pct", "boundary_pct", "balls_per_boundary", "economy"}

# Sample-size flags, by balls.
LOW_SAMPLE, SMALL_SAMPLE = 30, 120


def role_metrics(role: str) -> dict[str, tuple[str, bool]]:
    if role == "batting":
        return BATTING_METRICS
    if role == "bowling":
        return BOWLING_METRICS
    raise FilterError(f"role must be 'batting' or 'bowling', got {role!r}")


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

    src = "v_bat" if role == "batting" else "v_bowl"
    balls_sql = catalogue["balls"][0]
    where, params = compile_filters(filters, engine.resolve_player)
    if players:
        where += f" AND player_id IN ({', '.join('?' * len(players))})"
        params += list(players)

    select = [f"{DIMENSIONS[d]} AS {d}" for d in dims]
    if "player" in dims:
        select.append("arg_max(team, match_date) AS team")
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
