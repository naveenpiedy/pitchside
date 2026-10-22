"""The AI-facing tool layer: every function takes plain arguments and returns
compact text. `mcp_server.py` registers these as MCP tools; tests call them
directly.

Design rules (token efficiency):
- Analysis tools compute locally and return small finished tables.
- Results are pipe-separated text with notes, not JSON: columns once, no padding.
- `query` caps rows (default 50) and says when there are more; `export` writes
  bulk results to a file and returns only its path and shape.
- Each call opens the engine and closes it again (about 40 ms), so a running
  server never holds the database files and `pitchside load` can always write.
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated

import duckdb
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
from pydantic import Field

from pitchside import config
from pitchside.engine import NameNotFound, PlayerNotFound, open_engine
from pitchside.stats import players as pl
from pitchside.stats import scorecard as sc
from pitchside.stats import teams as tm
from pitchside.stats.filters import FilterError, compile_filters, describe
from pitchside.stats.splits import (BATTING_METRICS, BOWLING_METRICS, DIMENSIONS, SHRUNK_METRICS,
                                    TEAM_BATTING_METRICS)
from pitchside.stats.table import Table

QUERY_LIMIT, QUERY_MAX_LIMIT = 50, 1000

# Shared parameter types: the descriptions end up in the MCP tool schemas.
Filters = Annotated[dict | None, Field(description='e.g. {"season": "2023+", "phase": "death", "bowl_type": "spin", '
                                                   '"opposition": "MI"}; all keys in `grammar`')]
By = Annotated[list[str] | None, Field(description="split dimensions, e.g. ['season'], ['phase', 'bowl_type']")]
Role = Annotated[str, Field(description="batting | bowling")]
Player = Annotated[str, Field(description="name or player_id (from find_player)")]
Competitions = Annotated[list[str] | None, Field(description="competition keys, e.g. ['ipl']; default: all loaded")]
Metrics = Annotated[list[str] | None, Field(description="metric names (see grammar); default: a standard set")]
QUERY_TIMEOUT_S, EXPORT_TIMEOUT_S = 20, 120


class ToolError(ValueError):
    """A user-facing error: bad arguments, unknown names, rejected SQL."""


@contextmanager
def _engine(competitions: list[str] | None = None, locked: bool = False, scratch: str | None = None):
    try:
        with open_engine(competitions or None, locked=locked, scratch=scratch) as e:
            yield e
    except (FilterError, PlayerNotFound, NameNotFound) as err:
        raise ToolError(str(err)) from err
    except (KeyError, FileNotFoundError) as err:
        raise ToolError(err.args[0] if err.args else str(err)) from err


def _render(tables: Table | list[Table]) -> str:
    return "\n\n".join(t.render_compact() for t in (tables if isinstance(tables, list) else [tables]))


# --- discovery ----------------------------------------------------------------

def overview() -> str:
    """What is loaded: competitions, seasons, matches, freshness."""
    rows = []
    for t in config.load_tournaments().values():
        if not t.db_path.exists():
            rows.append([t.key, t.name, t.status, "not loaded", None, None, None, None])
            continue
        con = duckdb.connect(str(t.db_path), read_only=True)
        try:
            n, first, last, s0, s1 = con.execute(
                "SELECT count(*), min(match_date), max(match_date), min(season_year), max(season_year) FROM matches"
            ).fetchone()
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        finally:
            con.close()
        rows.append([t.key, t.name, t.status, f"{s0}-{s1}", n, f"{first} to {last}",
                     meta.get("data_version"), meta.get("last_load")])
    table = Table(["competition", "name", "status", "seasons", "matches", "dates", "data_version", "last_load"], rows,
                  title="Loaded competitions",
                  notes=["T20 only. Rules eras for true stats: " + "; ".join(
                      f"{t.key} {list(t.rule_eras) or 'none'}" for t in config.load_tournaments().values())])
    return _render(table)


SCHEMA: dict[str, str] = {
    "v_balls": "every delivery (super overs excluded): match context, batter hand, bowler type, ball state "
               "before the ball, and per-ball expectations exp_bat_* / exp_bowl_*",
    "v_bat": "batting facts keyed by player_id = batter; one row per ball faced plus one per non-striker "
             "dismissal; runs, faced, dot, fours, sixes, out, exp_runs, exp_out, exp_dot, exp_bnd",
    "v_bowl": "bowling facts keyed by player_id = bowler; one row per delivery; balls (legal), runs (charged), "
              "wickets (credited), dot, fours, sixes, wides, noballs, exp_runs, exp_wkts, exp_dot, exp_bnd",
    "v_team": "team facts, every delivery twice: side = 'batting' / 'bowling'; runs incl. extras, wickets all dismissals",
    "v_results": "one row per match per team: outcome won/lost/tied/no result, innings_no 1 = batted first, toss",
    "v_matches": "one row per match: canonical venue, franchise1/2, winner_franchise, stage_group, result, method",
    "v_innings": "one row per innings: totals, target, allocated_balls, batting/bowling franchise",
    "v_players": "player_id -> name",
    "{comp}.batting_innings": "scorecard line per batter per innings (incl. dismissal, entry state)",
    "{comp}.bowling_innings": "scorecard line per bowler per innings",
    "{comp}.season_batting": "per player per season batting totals",
    "{comp}.season_bowling": "per player per season bowling totals",
    "{comp}.baseline_factors": "true-stat situation factors by target, era, phase, wickets_down, innings",
    "{comp}.baseline_levels": "true-stat level by target, season, over",
    "reg.player_attributes": "batting_hand, bowling_arm, bowling_style, bowl_type (pace/spin), bowl_subtype",
}
COLUMN_NOTES: dict[str, str] = {
    "team": "canonical franchise (current name); team_name = name used that season",
    "opposition": "canonical franchise; opposition_name = name used that season",
    "phase": "powerplay (overs 1-6) / middle (7-15) / death (16-20), scaled for shortened innings",
    "over_no": "0-based over", "position": "batting position", "nth_ball": "the batter's nth ball of the innings",
    "season_year": "year the season was played", "stage": "league / playoffs",
    "bowl_type": "pace / spin / unknown", "innings_no": "1 = first innings, 2 = chase",
    "wickets_before": "team wickets down before the ball", "balls_remaining": "legal balls left in the allocation",
    "exp_runs": "expected runs for an average player in the same situation (true stats baseline)",
    "out": "1 if this player was dismissed on this row",
}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def describe_schema(table: str | None = None, column: str | None = None, limit: int = 30) -> str:
    """No args: tables. table: its columns. table + column: the values it holds (most common first)."""
    with _engine() as e:
        comps = [t.key for t in e.competitions]
        if table is None:
            rows = []
            for name, desc in SCHEMA.items():
                rows.append([name.replace("{comp}", "|".join(comps)) if "{comp}" in name else name, desc])
            return _render(Table(["table", "contents"], rows, title="Tables (query with the `query` tool)",
                                 notes=["{comp} tables exist once per competition, e.g. " + f"{comps[0]}.batting_innings",
                                        "prefer v_* views: team/venue names canonical, attributes and expectations joined"]))
        if not _IDENT.match(table):
            raise ToolError(f"Bad table name {table!r}")
        try:
            cols = e.con.execute(f"DESCRIBE {table}").fetchall()
        except duckdb.Error as err:
            raise ToolError(f"Unknown table {table!r}; call describe_schema() for the list") from err
        names = [c[0] for c in cols]
        if column is None:
            return _render(Table(["column", "type", "note"], [[c[0], c[1], COLUMN_NOTES.get(c[0], "")] for c in cols],
                                 title=f"{table} columns"))
        if column not in names:
            raise ToolError(f"Unknown column {column!r} in {table}; columns: {', '.join(names)}")
        distinct = e.con.execute(f'SELECT count(DISTINCT "{column}") FROM {table}').fetchone()[0]
        rows = e.con.execute(f'SELECT "{column}", count(*) AS n FROM {table} GROUP BY 1 ORDER BY n DESC LIMIT ?',
                             [min(limit, 200)]).fetchall()
        return _render(Table([column, "rows"], [list(r) for r in rows], title=f"{table}.{column} values",
                             notes=[f"{distinct} distinct values" + (f"; showing top {len(rows)}" if distinct > len(rows) else "")]))


def grammar() -> str:
    """Filter keys, split dimensions and metric names accepted by the analysis tools."""
    return "\n".join([
        "## filters: an object of key -> value. Values: scalar, list, 'a,b', range '2022-2025' or '2020+', "
        "dates 'YYYY-MM-DD..YYYY-MM-DD'",
        describe(),
        "## dimensions (by / per): " + ", ".join(DIMENSIONS),
        "## batting metrics: " + ", ".join(BATTING_METRICS),
        "## bowling metrics: " + ", ".join(BOWLING_METRICS),
        "## shrunk (small-sample-adjusted) metrics: batting " + ", ".join(SHRUNK_METRICS["batting"])
        + "; bowling " + ", ".join(SHRUNK_METRICS["bowling"])
        + ". Each is pulled towards the average under the same filters by k balls of prior evidence "
        "(k estimated from the data per rate). Rank with these when samples are small or uneven.",
        "## team metrics (team_ranking): " + ", ".join(TEAM_BATTING_METRICS),
        "## true stats: true_* = actual vs an average player on the same balls (situation-, era-adjusted); "
        "positive is always better; exp_* is the expectation.",
    ])


def find_player(name: str, competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        rows = e.find_player(name)
        return _render(Table(["player_id", "name", "match", "balls_involved", "seasons", "competitions"],
                             [list(r.values()) for r in rows], title=f"Players matching {name!r}",
                             notes=["pass player_id to other tools when a name is ambiguous"]))


def find_team(name: str, competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        q = name.strip().lower()
        hits = sorted({c for n, c in e.team_names.items() if q in n.lower()})
        rows = [[c, ", ".join(sorted(n for n, cc in e.team_names.items() if cc == c and n != c))] for c in hits]
        return _render(Table(["team", "also_known_as"], rows, title=f"Teams matching {name!r}",
                             notes=["any of these names or codes work wherever a team is expected"]))


def find_venue(name: str, competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        q = name.strip().lower()
        hits = sorted({c for n, c in e.venue_names.items() if q in n.lower()})
        rows = e.con.execute("SELECT venue, any_value(city), count(*), min(season_year), max(season_year) "
                             "FROM v_matches WHERE venue IN (SELECT unnest(?)) GROUP BY 1 ORDER BY 3 DESC",
                             [hits]).fetchall() if hits else []
        return _render(Table(["venue", "city", "matches", "first", "last"], [list(r) for r in rows],
                             title=f"Venues matching {name!r}"))


def find_matches(team: str | None = None, opposition: str | None = None, season: str | int | None = None,
                 venue: str | None = None, date: str | None = None, limit: int = 20,
                 competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        filters = {"team": team, "opposition": opposition, "season": season, "venue": venue, "date": date}
        where, params = compile_filters(filters, resolve_team=e.resolve_team)
        if not team:
            where += " AND innings_no IS DISTINCT FROM 2"  # one row per match
        rows = e.con.execute(f"""
            SELECT r.match_id, r.match_date, r.team, r.opposition, r.venue, r.stage, r.outcome,
                   coalesce(m.win_by_runs || ' runs', m.win_by_wickets || ' wkts', m.result) AS margin
            FROM (SELECT * FROM v_results WHERE {where}) r JOIN v_matches m USING (match_id)
            ORDER BY r.match_date DESC LIMIT ?""", params + [min(limit, 200)]).fetchall()
        return _render(Table(["match_id", "date", "team", "opposition", "venue", "stage", "outcome_for_team", "margin"],
                             [list(r) for r in rows], title="Matches (newest first)",
                             notes=["pass match_id to scorecard"]))


# --- analysis -----------------------------------------------------------------

def player_profile(player: Player, filters: Filters = None, competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        return _render(pl.profile(e, player, filters))


def player_splits(player: Player, role: Role = "batting", by: By = None, filters: Filters = None,
                  metrics: Metrics = None, competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        return _render(pl.splits(e, player, role, by, filters, metrics))


def true_stats(player: Player, role: Role = "batting", by: By = None, filters: Filters = None,
               competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        return _render(pl.true_stats(e, player, role, by, filters))


def compare_players(players: list[str], role: Role = "batting", by: By = None,
                    filters: Filters = None, metrics: Metrics = None,
                    competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        return _render(pl.compare(e, players, role, filters, by, metrics))


def leaderboard(metric: str, role: str | None = None, filters: Filters = None, min_balls: int | None = None,
                top: int = 15, per: str | None = None, competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        return _render(pl.leaderboard(e, metric, role, filters, min_balls, min(top, 100), per))


def team_profile(team: str, filters: Filters = None, competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        return _render(tm.team_profile(e, team, filters))


def head_to_head(team_a: str, team_b: str, filters: Filters = None, top: int = 5,
                 competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        return _render(tm.head_to_head(e, team_a, team_b, filters, top))


def venue_profile(venue: str, filters: Filters = None, top: int = 5, competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        return _render(tm.venue_profile(e, venue, filters, top))


def team_ranking(metric: str = "run_rate", side: str = "batting", filters: Filters = None,
                 per: str | None = None, top: int | None = None, competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        return _render(tm.team_ranking(e, metric, side, filters, per, top))


def scorecard(match_id: str, partnerships: bool = False, overs: bool = False,
              competitions: Competitions = None) -> str:
    with _engine(competitions) as e:
        comp = e.con.execute("SELECT competition FROM v_matches WHERE match_id = ?", [str(match_id)]).fetchone()
        if not comp:
            raise ToolError(f"No match {match_id!r}; use find_matches")
        con = e.con
        con.execute(f"USE {comp[0]}")
        try:
            summary = sc.match_summary(con, str(match_id))
            parts = [sc.render_scorecard(summary)]
            for inn in summary["innings"]:
                n = inn["innings_no"]
                if partnerships:
                    parts.append(_render(Table(["wicket", "runs", "balls", "batters"],
                                               [[p["wicket"], p["runs"], p["balls"], " & ".join(p["batters"])]
                                                for p in sc.partnerships(con, str(match_id), n)],
                                               title=f"Partnerships, innings {n}")))
                if overs:
                    parts.append(_render(Table(["over", "runs", "wickets", "score"],
                                               [[o["over"], o["runs"], o["wickets"], f"{o['score']}/{o['wickets_down']}"]
                                                for o in sc.over_progression(con, str(match_id), n)],
                                               title=f"Overs, innings {n}")))
            return "\n\n".join(parts)
        finally:
            con.execute("USE memory")


# --- SQL ----------------------------------------------------------------------

def _check_select(con: duckdb.DuckDBPyConnection, sql: str) -> None:
    try:
        statements = con.extract_statements(sql)
    except duckdb.Error as err:
        raise ToolError(f"SQL parse error: {err}") from err
    if len(statements) != 1:
        raise ToolError("Send exactly one statement")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise ToolError("Only SELECT (or WITH ... SELECT) queries are allowed")


def _run_with_timeout(con: duckdb.DuckDBPyConnection, sql: str, timeout_s: float):
    timer = threading.Timer(timeout_s, con.interrupt)
    timer.start()
    try:
        return con.execute(sql)
    except duckdb.InterruptException as err:
        raise ToolError(f"Query exceeded {timeout_s:.0f}s; aggregate more or narrow the filters") from err
    except duckdb.Error as err:
        raise ToolError(f"SQL error: {err}") from err
    finally:
        timer.cancel()


def query(sql: str, limit: int = QUERY_LIMIT, competitions: Competitions = None) -> str:
    """Run one read-only SELECT over the views/tables (and scratch.* tables) and return at most `limit` rows."""
    limit = max(1, min(int(limit), QUERY_MAX_LIMIT))
    with _engine(competitions, locked=True, scratch="ro") as e:
        _check_select(e.con, sql)
        start = time.perf_counter()
        cur = _run_with_timeout(e.con, sql, QUERY_TIMEOUT_S)
        columns = [c[0] for c in cur.description]
        rows = [list(r) for r in cur.fetchmany(limit + 1)]
        more = len(rows) > limit
        notes = [f"{min(len(rows), limit)} rows in {time.perf_counter() - start:.2f}s"]
        if more:
            notes.append(f"more rows exist beyond {limit}: aggregate further, raise limit (max {QUERY_MAX_LIMIT}), "
                         "or use export for the full result")
        return _render(Table(columns, rows[:limit], notes=notes))


def export(sql: str, format: str = "csv", name: str | None = None, competitions: Competitions = None) -> str:
    """Run a SELECT and write the full result to data/exports; returns only the path and shape."""
    if format not in ("csv", "parquet"):
        raise ToolError("format must be 'csv' or 'parquet'")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name or "export").strip("_") or "export"
    path = config.EXPORT_DIR / f"{stem}_{datetime.now():%Y%m%d_%H%M%S}.{format}"
    with _engine(competitions, locked=True) as e:
        _check_select(e.con, sql)
        timer = threading.Timer(EXPORT_TIMEOUT_S, e.con.interrupt)
        timer.start()
        try:
            table = e.con.sql(sql).to_arrow_table()
        except duckdb.InterruptException as err:
            raise ToolError(f"Export exceeded {EXPORT_TIMEOUT_S}s") from err
        except duckdb.Error as err:
            raise ToolError(f"SQL error: {err}") from err
        finally:
            timer.cancel()
    path.parent.mkdir(parents=True, exist_ok=True)
    if format == "csv":
        pacsv.write_csv(table, str(path))
    else:
        pq.write_table(table, str(path))
    return "\n".join([
        "## Exported", f"path: {path}", f"rows: {table.num_rows}", f"bytes: {path.stat().st_size}",
        f"columns: {', '.join(table.column_names)}", "note: the data is on disk only; it was not sent back"])


# --- data ---------------------------------------------------------------------

def sync(full: bool = False, competitions: Competitions = None) -> str:
    """Pull new matches from Cricsheet for ongoing competitions, then run quality checks."""
    from pitchside.etl import sync as sync_mod

    try:
        results = sync_mod.sync(competitions or None, full=full, log=lambda _msg: None)
    except KeyError as err:
        raise ToolError(err.args[0]) from err
    rows, notes = [], []
    for r in results:
        summary = f"{sum(c.status == 'fail' for c in r.checks)} fail, {sum(c.status == 'warn' for c in r.checks)} warn"
        rows.append([r.competition, r.source, r.added, r.updated, r.data_version, r.error or summary])
        notes += [f"{r.competition} {c.status} {c.name}: {c.detail}" for c in r.checks if c.status != "ok"]
    return _render(Table(["competition", "source", "added", "updated", "data_version", "checks"], rows,
                         title="Sync", notes=notes))


# --- scratch space (add-on A5) ------------------------------------------------
# Named tables the AI builds for multi-step analysis, kept in db/scratch.duckdb
# across calls and readable from `query` as scratch.<name>. Each records the
# data versions it was built from, so stale tables are flagged after a sync.

SCRATCH_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
SCRATCH_MAX_ROWS = 2_000_000


def scratch_save(name: str, sql: str, competitions: Competitions = None) -> str:
    """Save the result of one SELECT as scratch.<name> (replacing any table of that name)."""
    if not SCRATCH_NAME.match(name):
        raise ToolError("name: lowercase letters, digits and _, starting with a letter (max 41 chars)")
    with _engine(competitions, locked=True, scratch="rw") as e:
        _check_select(e.con, sql)
        _run_with_timeout(e.con, f"CREATE OR REPLACE TABLE scratch.{name} AS {sql}", EXPORT_TIMEOUT_S)
        rows = e.con.execute(f"SELECT count(*) FROM scratch.{name}").fetchone()[0]
        if rows > SCRATCH_MAX_ROWS:
            e.con.execute(f"DROP TABLE scratch.{name}")
            raise ToolError(f"{rows} rows is over the scratch limit of {SCRATCH_MAX_ROWS}; aggregate first, "
                            "or use export for bulk data")
        cols = [f"{c[0]} {c[1]}" for c in e.con.execute(f"DESCRIBE scratch.{name}").fetchall()]
        e.con.execute("INSERT OR REPLACE INTO scratch._catalog VALUES (?, ?, ?, ?, ?, ?)",
                      [name, datetime.now(), sql, e.version_key, rows, ", ".join(cols)])
        cur = e.con.execute(f"SELECT * FROM scratch.{name} LIMIT 5")
        return _render(Table([c[0] for c in cur.description], [list(r) for r in cur.fetchall()],
                             title=f"scratch.{name} (first 5 of {rows} rows)",
                             notes=[f"saved; read it with query: SELECT ... FROM scratch.{name}"]))


def scratch_list() -> str:
    """Saved scratch tables, with their size, source SQL and whether new data has arrived since."""
    if not config.SCRATCH_DB.exists():
        return "(no scratch tables yet; create one with scratch_save)"
    with _engine(scratch="ro") as e:
        current = e.version_key
        try:
            rows = e.con.execute("SELECT name, rows, created_at, versions, sql FROM scratch._catalog "
                                 "ORDER BY created_at DESC").fetchall()
        except duckdb.Error:
            rows = []
        return _render(Table(["name", "rows", "created_at", "stale", "sql"],
                             [[n, r, c, "yes" if v != current else "", s] for n, r, c, v, s in rows],
                             title="Scratch tables",
                             notes=["stale = built before the latest data load; re-run scratch_save to refresh"]))


def scratch_drop(name: str) -> str:
    """Delete scratch.<name>."""
    if not SCRATCH_NAME.match(name):
        raise ToolError("bad name")
    with _engine(scratch="rw") as e:
        exists = e.con.execute("SELECT count(*) FROM duckdb_tables() WHERE database_name = 'scratch' "
                               "AND table_name = ?", [name]).fetchone()[0]
        if not exists:
            raise ToolError(f"No scratch table {name!r}")
        e.con.execute(f"DROP TABLE scratch.{name}")
        e.con.execute("DELETE FROM scratch._catalog WHERE name = ?", [name])
        return f"dropped scratch.{name}"
