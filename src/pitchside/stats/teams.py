"""Team (F17) and venue (F18) analyses.

Two kinds of data feed these:
- match results (v_results / v_innings): only match-level filters apply
  (season, competition, team, opposition, venue, city, stage, date, match, innings);
- ball-by-ball team facts (v_team) and player facts (v_bat / v_bowl): every filter applies.
Ball-level filters (phase, bowl_type...) passed to a profile are applied where
they make sense and noted on the results tables they can't apply to.
"""

from __future__ import annotations

from pitchside.engine import Engine
from pitchside.stats import cache
from pitchside.stats.filters import compile_filters
from pitchside.stats.splits import aggregate
from pitchside.stats.table import Table

MATCH_FILTERS = {"season", "competition", "team", "opposition", "venue", "city", "stage", "date", "match", "innings"}

RESULT_DIMENSIONS = {
    "season": "season_year",
    "opposition": "opposition",
    "venue": "venue",
    "setting_chasing": "CASE innings_no WHEN 1 THEN 'setting' ELSE 'chasing' END",
    "stage": "stage",
    "team": "team",
}


def _split_filters(filters: dict | None) -> tuple[dict, list[str]]:
    match_level = {k: v for k, v in (filters or {}).items() if k in MATCH_FILTERS}
    ignored = sorted(k for k in (filters or {}) if k not in MATCH_FILTERS)
    return match_level, ignored


def _rows(engine: Engine, sql: str, params: list) -> tuple[list[str], list[list]]:
    cur = engine.con.execute(sql, params)
    return [c[0] for c in cur.description], [list(r) for r in cur.fetchall()]


def _add_share(table: Table, column: str = "balls", name: str = "share_pct") -> Table:
    """Append each row's percentage share of `column`."""
    if column not in table.columns or not table.rows:
        return table
    i = table.columns.index(column)
    total = sum(r[i] or 0 for r in table.rows)
    table.columns.append(name)
    for r in table.rows:
        r.append(round(100.0 * (r[i] or 0) / total, 1) if total else None)
    return table


def results(engine: Engine, filters: dict | None = None, by: list[str] | None = None, title: str = "") -> Table:
    """Win/loss record from v_results, grouped by result dimensions."""
    match_level, ignored = _split_filters(filters)
    where, params = compile_filters(match_level, resolve_team=engine.resolve_team)
    by = list(by or [])
    dims = [f"{RESULT_DIMENSIONS[d]} AS {d}" for d in by]
    sql = f"""
        SELECT {''.join(d + ', ' for d in dims)}
               count(*) AS matches,
               count(*) FILTER (outcome = 'won') AS won,
               count(*) FILTER (outcome = 'lost') AS lost,
               count(*) FILTER (tied) AS tied,
               count(*) FILTER (outcome = 'no result') AS no_result,
               round(100.0 * count(*) FILTER (outcome = 'won')
                     / nullif(count(*) FILTER (outcome IN ('won', 'lost')), 0), 1) AS win_pct,
               count(*) FILTER (outcome = 'won' AND innings_no = 1) AS won_setting,
               count(*) FILTER (outcome = 'won' AND innings_no = 2) AS won_chasing,
               count(*) FILTER (won_toss AND outcome = 'won') AS won_toss_and_match
        FROM v_results WHERE {where}"""
    if by:
        sql += " GROUP BY ALL ORDER BY " + (", ".join(by) if by != ["opposition"] else "matches DESC")
    columns, rows = _rows(engine, sql, params)
    rows = [r for r in rows if r[len(by)]]  # an ungrouped count over nothing is still one row
    notes = ["tied = tied in normal play; the super over decides won/lost"]
    if ignored:
        notes.append(f"ball-level filters not applied to results: {', '.join(ignored)}")
    return Table(columns, rows, title=title, notes=notes if rows else ["no matches for these filters"])


def team_profile(engine: Engine, team: str, filters: dict | None = None) -> list[Table]:
    name = engine.resolve_team(team)
    f = {**(filters or {}), "team": name}

    def compute() -> list[Table]:
        return [
            results(engine, f, by=["season"], title=f"{name}: results by season"),
            results(engine, f, by=["opposition"], title=f"{name}: results by opposition"),
            aggregate(engine, "team_batting", f, by=["phase"], title=f"{name}: batting by phase"),
            aggregate(engine, "team_bowling", f, by=["phase"], title=f"{name}: bowling by phase (runs/wickets conceded)"),
            _add_share(aggregate(engine, "bowling", f, by=["bowl_type"],
                                 metrics=["balls", "runs", "wickets", "economy", "average", "strike_rate", "dot_pct"],
                                 title=f"{name}: bowling resources by type")),
            _add_share(aggregate(engine, "batting", f, by=["order_group"],
                                 metrics=["runs", "balls", "outs", "average", "strike_rate", "boundary_pct"],
                                 title=f"{name}: batting depth (runs off the bat)"), column="runs", name="runs_share_pct"),
        ]

    return cache.cached("team_profile", {"team": name, "filters": filters or {}}, engine.version_key, compute)


def head_to_head(engine: Engine, team_a: str, team_b: str, filters: dict | None = None, top: int = 5) -> list[Table]:
    a, b = engine.resolve_team(team_a), engine.resolve_team(team_b)
    both = {**(filters or {}), "team": [a, b], "opposition": [a, b]}

    def compute() -> list[Table]:
        return [
            results(engine, {**(filters or {}), "team": a, "opposition": b}, by=["season"],
                    title=f"{a} v {b}: results by season ({a}'s record)"),
            aggregate(engine, "team_batting", both, by=["team", "phase"],
                      metrics=["innings", "runs", "balls", "wickets", "run_rate", "boundary_pct", "dot_pct"],
                      title=f"{a} v {b}: batting by phase"),
            aggregate(engine, "batting", both, by=["player"], metrics=["innings", "runs", "balls", "average", "strike_rate"],
                      order_by="runs", limit=top, title=f"{a} v {b}: top run-scorers"),
            aggregate(engine, "bowling", both, by=["player"], metrics=["innings", "balls", "wickets", "economy", "average"],
                      order_by="wickets", limit=top, title=f"{a} v {b}: top wicket-takers"),
        ]

    return cache.cached("head_to_head", {"a": a, "b": b, "filters": filters or {}, "top": top},
                        engine.version_key, compute)


def venue_summary(engine: Engine, filters: dict | None = None, by: list[str] | None = None, title: str = "") -> Table:
    """Par scores, chasing record and toss effect, one row per match (from the side batting first)."""
    match_level, ignored = _split_filters(filters)
    for key in ("team", "opposition", "innings"):  # rows are per match, not per side
        match_level.pop(key, None)
    where, params = compile_filters(match_level, resolve_team=engine.resolve_team)
    by = list(by or [])
    dims = [f"{RESULT_DIMENSIONS[d]} AS {d}" for d in by]
    # Par uses full-length first innings only (20 overs available, no DLS).
    full = "i1.allocated_balls = 120 AND r.method IS NULL"
    sql = f"""
        SELECT {''.join(d + ', ' for d in dims)}
               count(*) AS matches,
               round(avg(i1.runs) FILTER ({full}), 1) AS avg_1st_innings,
               median(i1.runs) FILTER ({full}) AS median_1st_innings,
               max(i1.runs) AS highest_1st_innings,
               round(avg(i2.runs) FILTER ({full}), 1) AS avg_2nd_innings,
               round(100.0 * count(*) FILTER (r.outcome = 'lost')
                     / nullif(count(*) FILTER (r.outcome IN ('won', 'lost')), 0), 1) AS chasing_win_pct,
               round(100.0 * count(*) FILTER ((r.won_toss AND r.outcome = 'won') OR (NOT r.won_toss AND r.outcome = 'lost'))
                     / nullif(count(*) FILTER (r.outcome IN ('won', 'lost')), 0), 1) AS toss_winner_win_pct,
               round(100.0 * count(*) FILTER (r.toss_decision = 'field') / count(*), 1) AS toss_chose_field_pct
        FROM (SELECT * FROM v_results WHERE innings_no = 1 AND {where}) r
        JOIN v_innings i1 ON i1.match_id = r.match_id AND i1.innings_no = 1
        LEFT JOIN v_innings i2 ON i2.match_id = r.match_id AND i2.innings_no = 2"""
    if by:
        sql += " GROUP BY ALL ORDER BY " + ", ".join(by)
    columns, rows = _rows(engine, sql, params)
    rows = [r for r in rows if r[len(by)]]
    notes = ["averages use full-length first innings (20 overs, no DLS); chasing_win_pct excludes no results"]
    if ignored:
        notes.append(f"ball-level filters not applied to match summary: {', '.join(ignored)}")
    return Table(columns, rows, title=title, notes=notes if rows else ["no matches for these filters"])


def venue_profile(engine: Engine, venue: str, filters: dict | None = None, top: int = 5) -> list[Table]:
    name = engine.resolve_venue(venue)
    f = {**(filters or {}), "venue": name}

    def compute() -> list[Table]:
        return [
            venue_summary(engine, f, title=f"{name}: overview"),
            venue_summary(engine, f, by=["season"], title=f"{name}: by season"),
            aggregate(engine, "team_batting", f, by=["setting_chasing", "phase"],
                      metrics=["innings", "runs", "balls", "wickets", "run_rate", "average", "boundary_pct", "dot_pct"],
                      title=f"{name}: scoring by innings and phase"),
            _add_share(aggregate(engine, "bowling", f, by=["bowl_type"],
                                 metrics=["balls", "runs", "wickets", "economy", "average", "strike_rate", "dot_pct"],
                                 title=f"{name}: pace v spin")),
            aggregate(engine, "batting", f, by=["player"], metrics=["innings", "runs", "balls", "average", "strike_rate"],
                      order_by="runs", limit=top, title=f"{name}: top run-scorers"),
            aggregate(engine, "bowling", f, by=["player"], metrics=["innings", "balls", "wickets", "economy", "average"],
                      order_by="wickets", limit=top, title=f"{name}: top wicket-takers"),
        ]

    return cache.cached("venue_profile", {"venue": name, "filters": filters or {}, "top": top},
                        engine.version_key, compute)


def team_ranking(engine: Engine, metric: str = "run_rate", side: str = "batting", filters: dict | None = None,
                 per: str | None = None, top: int | None = None) -> Table:
    """Teams ranked by a team-level metric (e.g. death-overs run rate), optionally top N per season."""
    role = f"team_{side}"
    title = f"Teams by {side} {metric}" + (f" per {per}" if per else "")
    return cache.cached(
        "team_ranking", {"metric": metric, "side": side, "filters": filters or {}, "per": per, "top": top},
        engine.version_key,
        lambda: aggregate(engine, role, filters, by=["team"], order_by=metric, per=per,
                          limit=top or (None if not per else 1), title=title),
    )
