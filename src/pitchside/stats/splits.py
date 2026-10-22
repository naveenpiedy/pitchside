"""The splits engine: aggregate batting or bowling facts by any dimensions, under any filters.

Everything player-facing (splits, profiles, leaderboards, comparisons) is a call
to `aggregate` with different dimensions, filters and ordering.
"""

from __future__ import annotations

from pitchside.engine import Engine
from pitchside.stats.filters import ENTRY_PHASE_SQL, FilterError, compile_filters
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
    # Entry point (A4): when the batter walked in. Openers arrive at ball 0.
    "entry_phase": ENTRY_PHASE_SQL,
    "entry_bucket": ("CASE WHEN entry_balls = 0 THEN '00 opener' WHEN entry_over <= 6 THEN '01-06' "
                     "WHEN entry_over <= 10 THEN '07-10' WHEN entry_over <= 15 THEN '11-15' ELSE '16-20' END"),
    "entry_over": "entry_over",
    "entry_wickets": "entry_wickets",
    "player": "player_id",
    "bowler": "bowler_id",
    "batter": "batter_id",
}
ID_DIMENSIONS = {"player", "bowler", "batter"}
# Dimensions with a natural order; the rest sort by volume (balls), biggest first.
ORDERED = {
    "season": "season", "over": "over", "position": "position", "ball_bucket": "ball_bucket",
    "order_group": "order_group",
    "entry_phase": "CASE entry_phase WHEN 'opener' THEN 0 WHEN 'powerplay' THEN 1 WHEN 'middle' THEN 2 ELSE 3 END",
    "entry_bucket": "entry_bucket", "entry_over": "entry_over", "entry_wickets": "entry_wickets",
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
    # Win probability added (A1), in wins: the change in the batting side's win chance on balls faced.
    "wpa": ("round(sum(wpa), 2)", True),
    "wpa_per_match": ("round(sum(wpa) / nullif(count(DISTINCT match_id), 0), 3)", True),
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
    # Win probability added (A1), in wins, from the bowling side's point of view.
    "wpa": ("round(sum(wpa), 2)", True),
    "wpa_per_match": ("round(sum(wpa) / nullif(count(DISTINCT match_id), 0), 3)", True),
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
    "wpa": "round(sum(wpa), 2)",
}
_BATTING_SIDE_GOOD = {"innings", "runs", "balls", "run_rate", "average", "balls_per_wicket",
                      "boundary_pct", "extras", "fours", "sixes", "wpa"}
TEAM_BATTING_METRICS = {m: (sql, m in _BATTING_SIDE_GOOD) for m, sql in _TEAM_SQL.items()}
# wpa is already signed from each side's point of view, so higher is better for both.
TEAM_BOWLING_METRICS = {m: (sql, m not in _BATTING_SIDE_GOOD or m in ("innings", "balls", "wpa"))
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


# --- Shrinkage (add-on A3) ------------------------------------------------------
# Per-ball components: (numerator, denominator) sums. A shrunk rate is
#     (numerator + k × population rate) / (denominator + k)
# with k from the competition's `shrink_priors` and the population rate taken
# under the same filters (without the player restriction). exp_* components are
# the player's own context and are not shrunk.
SHRINK_COMPONENTS: dict[str, dict[str, tuple[str, str]]] = {
    "batting": {
        "rpb": ("sum(runs)", "sum(faced)"),
        "dpb": ("sum(out)", "sum(faced)"),
        "dot": ("sum(dot)", "sum(faced)"),
        "bnd": ("sum(fours) + sum(sixes)", "sum(faced)"),
        "true_rpb": ("sum(runs) - sum(exp_runs)", "sum(faced)"),
        "true_dpb": ("sum(out) - sum(exp_out)", "sum(faced)"),
        "exp_rpb": ("sum(exp_runs)", "sum(faced)"),
        "exp_dpb": ("sum(exp_out)", "sum(faced)"),
    },
    "bowling": {
        "rpb": ("sum(runs)", "sum(balls)"),
        "wpb": ("sum(wickets)", "sum(balls)"),
        "dot": ("sum(dot)", "sum(balls)"),
        "true_rpb": ("sum(exp_runs) - sum(runs)", "sum(balls)"),
        "true_wpb": ("sum(wickets) - sum(exp_wkts)", "sum(balls)"),
    },
}
UNSHRUNK_COMPONENTS = {"exp_rpb", "exp_dpb"}


def _div(a, b):
    return a / b if a is not None and b else None


# name: (components, value from shrunk rates `r` and balls `n`, higher_is_better)
SHRUNK_METRICS: dict[str, dict[str, tuple[list[str], object, bool]]] = {
    "batting": {
        "strike_rate_shrunk": (["rpb"], lambda r, n: 100 * r["rpb"], True),
        "average_shrunk": (["rpb", "dpb"], lambda r, n: _div(r["rpb"], r["dpb"]), True),
        "dot_pct_shrunk": (["dot"], lambda r, n: 100 * r["dot"], False),
        "boundary_pct_shrunk": (["bnd"], lambda r, n: 100 * r["bnd"], True),
        "true_sr_shrunk": (["true_rpb"], lambda r, n: 100 * r["true_rpb"], True),
        "true_avg_shrunk": (["true_rpb", "true_dpb", "exp_rpb", "exp_dpb"],
                            lambda r, n: _div(r["exp_rpb"] + r["true_rpb"], r["exp_dpb"] + r["true_dpb"])
                            - _div(r["exp_rpb"], r["exp_dpb"]), True),
    },
    "bowling": {
        "economy_shrunk": (["rpb"], lambda r, n: 6 * r["rpb"], False),
        "average_shrunk": (["rpb", "wpb"], lambda r, n: _div(r["rpb"], r["wpb"]), False),
        "strike_rate_shrunk": (["wpb"], lambda r, n: _div(1, r["wpb"]), False),
        "dot_pct_shrunk": (["dot"], lambda r, n: 100 * r["dot"], True),
        "true_economy_shrunk": (["true_rpb"], lambda r, n: 6 * r["true_rpb"], True),
        "true_wickets_shrunk": (["true_wpb"], lambda r, n: r["true_wpb"] * n, True),
    },
}
RATE_METRICS |= {m for role in SHRUNK_METRICS.values() for m in role if m != "true_wickets_shrunk"}


def _shrunk_rate(num, den, k: float, mu):
    if num is None or den is None or mu is None:
        return None
    return (num + k * mu) / (den + k) if den + k else None


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
    """Aggregate v_bat / v_bowl / v_team.

    by        dimensions to group on (see DIMENSIONS)
    players   restrict to these player ids
    metrics   SQL metrics from the role's catalogue and/or *_shrunk metrics
    order_by  a metric to rank on (direction is the metric's "better" direction)
    per       with order_by + limit: top `limit` within each value of this dimension
    """
    catalogue = role_metrics(role)
    shrunk_catalogue = SHRUNK_METRICS.get(role, {})
    metrics = list(metrics or DEFAULT_METRICS[role])
    for m in metrics + ([order_by] if order_by else []):
        if m not in catalogue and m not in shrunk_catalogue:
            raise FilterError(f"Unknown {role} metric {m!r}; choose from {', '.join([*catalogue, *shrunk_catalogue])}")
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

    # Shrinkage: which components, their k, and the population rate under the same filters.
    shrunk = [m for m in metrics if m in shrunk_catalogue]
    rank_in_python = bool(order_by and order_by in shrunk_catalogue)
    components = sorted({c for m in shrunk + ([order_by] if rank_in_python else []) for c in shrunk_catalogue[m][0]})
    priors, population = {}, {}
    if components:
        try:
            priors = {c: 0.0 if c in UNSHRUNK_COMPONENTS else engine.shrink_k(role, c) for c in components}
        except LookupError as err:
            raise FilterError(str(err)) from err
        sums = ", ".join(f"{SHRINK_COMPONENTS[role][c][0]} AS n_{c}, {SHRINK_COMPONENTS[role][c][1]} AS d_{c}"
                         for c in components)
        pop = engine.con.execute(f"SELECT {sums} FROM {src} WHERE {where}", params).fetchone()
        population = {c: _div(pop[2 * i], pop[2 * i + 1]) for i, c in enumerate(components)}

    if players:
        where += f" AND player_id IN ({', '.join('?' * len(players))})"
        params = params + list(players)

    sql_metrics = [m for m in metrics if m in catalogue]
    select = [f"{DIMENSIONS[d]} AS {d}" for d in dims]
    if "player" in dims:
        select.append("arg_max(team_name, match_date) AS team")
    select += [f"{catalogue[m][0]} AS {m}" for m in sql_metrics]
    select.append(f"{balls_sql} AS _balls")
    select += [f"{n} AS _n_{c}, {d} AS _d_{c}" for c, (n, d) in SHRINK_COMPONENTS.get(role, {}).items()
               if c in components]
    sql = f"SELECT {', '.join(select)} FROM {src} WHERE {where}"
    if dims:
        sql += " GROUP BY " + ", ".join(DIMENSIONS[d] for d in dims)
    if min_balls:
        sql += f" HAVING {balls_sql} >= ?"
        params.append(min_balls)

    if order_by and not rank_in_python:
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
    elif dims and not rank_in_python:
        keys = [ORDERED[d] for d in dims if d in ORDERED]
        sql += " ORDER BY " + ", ".join(keys + [f"{balls_sql} DESC"])
        if limit:
            sql += " LIMIT ?"
            params.append(limit)

    cur = engine.con.execute(sql, params)
    fetched = [c[0] for c in cur.description]
    records = [dict(zip(fetched, r)) for r in cur.fetchall()]
    if not dims:  # an ungrouped aggregate over nothing still yields one all-NULL row
        records = [r for r in records if r["_balls"]]

    # Shrunk metrics, computed from the hidden component sums.
    for r in records:
        rates = {c: _shrunk_rate(r[f"_n_{c}"], r[f"_d_{c}"], priors[c], population[c]) for c in components}
        for m in set(shrunk) | ({order_by} if rank_in_python else set()):
            comps, fn, _ = shrunk_catalogue[m]
            try:
                value = fn({c: rates[c] for c in comps}, r["_balls"])
            except TypeError:  # a missing rate
                value = None
            r[m] = round(value, 2) if value is not None else None

    if rank_in_python:
        better_high = shrunk_catalogue[order_by][2]
        records.sort(key=lambda r: (r[order_by] is None,
                                    -(r[order_by] or 0) if better_high else (r[order_by] or 0), -(r["_balls"] or 0)))
        if per and limit:
            seen: dict = {}
            kept = []
            for r in records:
                seen[r[per]] = seen.get(r[per], 0) + 1
                if seen[r[per]] <= limit:
                    kept.append(r)
            records = sorted(kept, key=lambda r: (r[per] is None, r[per]))
        elif limit:
            records = records[:limit]

    columns = dims + (["team"] if "player" in dims else []) + metrics
    rows = [[r.get(c) for c in columns] for r in records]
    for d in dims:
        if d in ID_DIMENSIONS:
            i = columns.index(d)
            for row in rows:
                row[i] = engine.name(row[i])

    notes = [] if rows else ["no data for these filters"]
    balls = [r["_balls"] for r in records]
    if any(b is not None and b < SMALL_SAMPLE for b in balls):
        columns.append("sample")
        for row, b in zip(rows, balls):
            b = b or 0
            row.append("low" if b < LOW_SAMPLE else "small" if b < SMALL_SAMPLE else "")
        notes.append(f"sample: low < {LOW_SAMPLE} balls, small < {SMALL_SAMPLE} balls")
    if shrunk:
        notes.append("*_shrunk: pulled towards the average under the same filters by k balls of prior "
                     f"({', '.join(f'{c} k={priors[c]:.0f}' for c in components if c not in UNSHRUNK_COMPONENTS)})")
    if any(d in ("bowl_type", "bowl_subtype", "bowling_arm", "batting_hand") for d in dims) or \
            any(k in (filters or {}) for k in ("bowl_type", "bowl_subtype", "bowling_arm", "batting_hand")):
        notes.append("bowling style / batting hand from player_attributes; 'unknown' = not classified")
    return Table(columns=columns, rows=rows, title=title, notes=notes)
