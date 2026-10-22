"""Command line entry point: `pitchside --help`."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import typer

from pitchside.config import get_tournament, load_tournaments
from pitchside.engine import NameNotFound, PlayerNotFound, open_engine
from pitchside.etl import attributes as attrs
from pitchside.etl.download import download
from pitchside.etl.quality import summarize
from pitchside.etl.load import connect, iter_zip, load_matches, rebuild as rebuild_db
from pitchside.etl.register import connect_registry, load_attributes, refresh_register
from pitchside.stats import cache
from pitchside.stats import players as pl
from pitchside.stats import scorecard as sc
from pitchside.stats import teams as tm
from pitchside.stats.filters import FilterError, describe, parse_cli_filters
from pitchside.stats.splits import BATTING_METRICS, BOWLING_METRICS, DIMENSIONS
from pitchside.stats.table import Table

app = typer.Typer(no_args_is_help=True, add_completion=False)
attributes_app = typer.Typer(no_args_is_help=True, help="Batting hand / bowling style per player.")
cache_app = typer.Typer(no_args_is_help=True, help="The analysis result cache.")
schedule_app = typer.Typer(no_args_is_help=True, help="Daily sync via Windows Task Scheduler.")
app.add_typer(attributes_app, name="attributes")
app.add_typer(cache_app, name="cache")
app.add_typer(schedule_app, name="schedule")

COMP_OPT = typer.Option(None, "--comp", "-c", help="Competition key(s); default all loaded")
FILTER_OPT = typer.Option(None, "--filter", "-f", help="key=value, repeatable; see `pitchside filters`")
ROLE_OPT = typer.Option("batting", "--role", "-r", help="batting or bowling")


@contextmanager
def _engine(comps: list[str] | None):
    """Open the engine and turn user errors into clean messages."""
    try:
        with open_engine(comps) as e:
            yield e
    except (FilterError, PlayerNotFound, NameNotFound, KeyError, FileNotFoundError) as err:
        typer.echo(f"error: {err.args[0] if isinstance(err, KeyError) else err}", err=True)
        raise typer.Exit(2)


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


def _show(tables: Table | list[Table]) -> None:
    for t in tables if isinstance(tables, list) else [tables]:
        typer.echo(t.render() + "\n")


@app.command()
def load(
    tournament: str = typer.Argument(..., help="Key from tournaments.toml, e.g. ipl"),
    download_zip: bool = typer.Option(False, "--download", help="Re-download the competition zip first"),
) -> None:
    """Load (or top up) a competition database from its full Cricsheet zip."""
    t = get_tournament(tournament)
    if download_zip or not t.zip_path.exists():
        typer.echo(f"Downloading {t.zip_url}")
        download(t.zip_url, t.zip_path)
    con = connect(t.db_path)
    try:
        r = load_matches(con, t, iter_zip(t.zip_path), source=t.zip_path.name)
    finally:
        con.close()
    typer.echo(f"{t.key}: {r.added} added, {r.updated} updated, {r.skipped} skipped (data_version {r.data_version})")


@app.command()
def register(fetch: bool = typer.Option(True, help="Download the latest register files first")) -> None:
    """Refresh the shared player registry (Cricsheet people + player_attributes.csv)."""
    counts = refresh_register(fetch=fetch)
    typer.echo(f"registry: {counts['people']} people, {counts['attributes']} with attributes")


@app.command()
def info() -> None:
    """What is loaded, per competition."""
    for t in load_tournaments().values():
        if not t.db_path.exists():
            typer.echo(f"{t.key}: not loaded")
            continue
        con = connect(t.db_path, read_only=True)
        try:
            matches, first, last, seasons = con.execute(
                "SELECT count(*), min(match_date), max(match_date), count(DISTINCT season_year) FROM matches"
            ).fetchone()
            balls = con.execute("SELECT count(*) FROM deliveries").fetchone()[0]
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        finally:
            con.close()
        typer.echo(
            f"{t.key} ({t.status}): {matches} matches, {seasons} seasons, {balls} deliveries, "
            f"{first} to {last}, data_version {meta.get('data_version')}, last load {meta.get('last_load')}"
        )


@app.command()
def scorecard(
    tournament: str,
    match_id: str,
    partnerships: bool = typer.Option(False, "--partnerships", "-p"),
    overs: bool = typer.Option(False, "--overs", "-o", help="Over-by-over progression"),
) -> None:
    """Print a match scorecard."""
    con = connect(get_tournament(tournament).db_path, read_only=True)
    try:
        summary = sc.match_summary(con, match_id)
        typer.echo(sc.render_scorecard(summary))
        for inn in summary["innings"]:
            n = inn["innings_no"]
            if partnerships:
                typer.echo(f"\nPartnerships, innings {n}:")
                for p in sc.partnerships(con, match_id, n):
                    typer.echo(f"  {p['wicket']}: {p['runs']} ({p['balls']})  " +
                               ", ".join(f"{k} {v}" for k, v in p["contributions"].items()))
            if overs:
                typer.echo(f"\nOvers, innings {n}:")
                typer.echo("  " + " ".join(f"{o['over']}:{o['runs']}" + ("w" * o["wickets"]) for o in sc.over_progression(con, match_id, n)))
    finally:
        con.close()


@app.command()
def find(name: str, comp: list[str] = COMP_OPT) -> None:
    """Look up players by name (exact, contains, fuzzy)."""
    with _engine(comp) as e:
        rows = e.find_player(name)
        _show(Table(columns=list(rows[0]) if rows else ["player_id"], rows=[list(r.values()) for r in rows]))


@app.command()
def filters() -> None:
    """The filter grammar, split dimensions and metrics."""
    typer.echo("Filters (-f key=value; lists 'a,b', ranges '2022-2025', '2020+'):\n" + describe())
    typer.echo("\nDimensions (--by): " + ", ".join(DIMENSIONS))
    typer.echo("Batting metrics: " + ", ".join(BATTING_METRICS))
    typer.echo("Bowling metrics: " + ", ".join(BOWLING_METRICS))


@app.command()
def player(name: str, comp: list[str] = COMP_OPT, filter: list[str] = FILTER_OPT) -> None:
    """Player profile: overall, by season, phase, bowling type, position, setting/chasing."""
    with _engine(comp) as e:
        _show(pl.profile(e, name, parse_cli_filters(filter)))


@app.command()
def splits(
    name: str,
    by: str = typer.Option("season", "--by", "-b", help="Comma-separated dimensions"),
    role: str = ROLE_OPT,
    metrics: str = typer.Option(None, "--metrics", "-m", help="Comma-separated metrics"),
    comp: list[str] = COMP_OPT,
    filter: list[str] = FILTER_OPT,
) -> None:
    """One player's numbers split by any dimensions under any filters."""
    with _engine(comp) as e:
        _show(pl.splits(e, name, role, _csv(by), parse_cli_filters(filter), _csv(metrics) or None))


@app.command()
def leaders(
    metric: str,
    role: str = typer.Option(None, "--role", "-r", help="batting / bowling (inferred for wickets, economy)"),
    top: int = typer.Option(15, "--top", "-n"),
    min_balls: int = typer.Option(None, "--min-balls", help="Default 60 for rate metrics"),
    per: str = typer.Option(None, "--per", help="Top N within each value of a dimension, e.g. season"),
    comp: list[str] = COMP_OPT,
    filter: list[str] = FILTER_OPT,
) -> None:
    """Leaderboard for a metric, e.g. `leaders economy -f phase=death -f season=2024`."""
    with _engine(comp) as e:
        _show(pl.leaderboard(e, metric, role, parse_cli_filters(filter), min_balls, top, per))


@app.command()
def compare(
    names: list[str],
    role: str = ROLE_OPT,
    by: str = typer.Option(None, "--by", "-b", help="Optional comma-separated dimensions"),
    metrics: str = typer.Option(None, "--metrics", "-m"),
    comp: list[str] = COMP_OPT,
    filter: list[str] = FILTER_OPT,
) -> None:
    """Compare players side by side under the same filters."""
    with _engine(comp) as e:
        _show(pl.compare(e, names, role, parse_cli_filters(filter), _csv(by), _csv(metrics) or None))


@app.command("true")
def true_cmd(
    name: str,
    role: str = ROLE_OPT,
    by: str = typer.Option("season", "--by", "-b", help="Comma-separated dimensions"),
    comp: list[str] = COMP_OPT,
    filter: list[str] = FILTER_OPT,
) -> None:
    """True stats for a player: actual, expected (average player, same balls) and the difference."""
    with _engine(comp) as e:
        _show(pl.true_stats(e, name, role, _csv(by), parse_cli_filters(filter)))


@app.command()
def rebuild(tournament: str) -> None:
    """Recompute derived tables and true-stat baselines (e.g. after changing rule_eras)."""
    t = get_tournament(tournament)
    con = connect(t.db_path)
    try:
        version = rebuild_db(con, t)
    finally:
        con.close()
    typer.echo(f"{t.key}: rebuilt (data_version {version})")


@app.command()
def baselines(
    tournament: str,
    target: str = typer.Option("bat_runs", "--target", "-t",
                               help="bat_runs bat_out bat_dot bat_bnd bowl_runs bowl_wkt bowl_dot bowl_bnd"),
    phase: str = typer.Option(None, "--phase", "-p"),
) -> None:
    """Inspect true-stat situation factors (wickets down x setting/chasing, per rules era and phase)."""
    con = connect(get_tournament(tournament).db_path, read_only=True)
    try:
        sql = ("SELECT era, phase, wickets_down, innings, round(factor, 3) AS factor, weight "
               "FROM baseline_factors WHERE target = ?" + (" AND phase = ?" if phase else "") +
               " ORDER BY era, CASE phase WHEN 'powerplay' THEN 1 WHEN 'middle' THEN 2 ELSE 3 END, wickets_down, innings")
        cur = con.execute(sql, [target, phase] if phase else [target])
        _show(Table([c[0] for c in cur.description], [list(r) for r in cur.fetchall()],
                    title=f"{tournament} {target} factors (1 = the over's season average)"))
    finally:
        con.close()


@app.command()
def team(name: str, comp: list[str] = COMP_OPT, filter: list[str] = FILTER_OPT) -> None:
    """Team profile: results, phase batting/bowling, bowling resources, batting depth."""
    with _engine(comp) as e:
        _show(tm.team_profile(e, name, parse_cli_filters(filter)))


@app.command()
def h2h(team_a: str, team_b: str, top: int = typer.Option(5, "--top", "-n"),
        comp: list[str] = COMP_OPT, filter: list[str] = FILTER_OPT) -> None:
    """Head-to-head between two teams."""
    with _engine(comp) as e:
        _show(tm.head_to_head(e, team_a, team_b, parse_cli_filters(filter), top))


@app.command()
def venue(name: str, top: int = typer.Option(5, "--top", "-n"),
          comp: list[str] = COMP_OPT, filter: list[str] = FILTER_OPT) -> None:
    """Venue profile: par scores, chasing and toss record, phase scoring, pace v spin, top players."""
    with _engine(comp) as e:
        _show(tm.venue_profile(e, name, parse_cli_filters(filter), top))


@app.command("team-rank")
def team_rank(
    metric: str = typer.Argument("run_rate"),
    side: str = typer.Option("batting", "--side", "-s", help="batting or bowling"),
    per: str = typer.Option(None, "--per", help="Rank within each value of a dimension, e.g. season"),
    top: int = typer.Option(None, "--top", "-n"),
    comp: list[str] = COMP_OPT,
    filter: list[str] = FILTER_OPT,
) -> None:
    """Teams ranked by a team metric, e.g. `team-rank run_rate -f phase=death -f season=2026`."""
    with _engine(comp) as e:
        _show(tm.team_ranking(e, metric, side, parse_cli_filters(filter), per, top))


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio (same as `pitchside-mcp`)."""
    from pitchside.mcp_server import main

    main()


@attributes_app.command("fetch")
def attributes_fetch(refresh: bool = typer.Option(False, help="Re-fetch rows previously taken from Wikipedia")) -> None:
    """Fetch batting hand / bowling style from Wikipedia for every loaded player."""
    stats = attrs.fetch_attributes(refresh=refresh, log=typer.echo)
    con = connect_registry()
    try:
        load_attributes(con)
    finally:
        con.close()
    typer.echo(f"{stats['fetched']} fetched, {stats['classified']} with a bowling type, "
               f"{stats['not_found']} without. Edit {attrs.ATTRIBUTES_CSV} (set source=manual) to fill gaps, "
               "then run `pitchside attributes reload`.")


@attributes_app.command("reload")
def attributes_reload() -> None:
    """Reload player_attributes.csv into the registry after hand edits."""
    con = connect_registry()
    try:
        load_attributes(con)
        n = con.execute("SELECT count(*) FROM player_attributes").fetchone()[0]
    finally:
        con.close()
    typer.echo(f"{n} attribute rows loaded")


@attributes_app.command("missing")
def attributes_missing(top: int = typer.Option(20, "--top", "-n"), comp: list[str] = COMP_OPT) -> None:
    """Players without a bowling type or batting hand, by how much they matter (balls)."""
    with _engine(comp) as e:
        for title, sql in [
            ("Bowlers without a bowling type", "SELECT bowler_id, count(*) FILTER (is_legal) FROM v_balls "
                                               "WHERE bowl_type = 'unknown' GROUP BY 1 ORDER BY 2 DESC LIMIT ?"),
            ("Batters without a batting hand", "SELECT batter_id, count(*) FILTER (is_ball_faced) FROM v_balls "
                                               "WHERE batter_hand IS NULL GROUP BY 1 ORDER BY 2 DESC LIMIT ?"),
        ]:
            rows = [[e.name(p), p, n] for p, n in e.con.execute(sql, [top]).fetchall()]
            _show(Table(["name", "player_id", "balls"], rows, title=title))


@app.command("sync")
def sync_cmd(
    comp: list[str] = typer.Option(None, "--comp", "-c", help="Competition key(s); default every 'ongoing' one"),
    full: bool = typer.Option(False, "--full", help="Use each competition's full zip instead of the recent one"),
    attributes: bool = typer.Option(True, help="Fetch batting hand / bowling style for new players"),
) -> None:
    """Pull new matches from Cricsheet, refresh everything derived, run quality checks."""
    from pitchside.etl import sync as sync_mod

    def log(msg: str) -> None:
        typer.echo(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}")

    try:
        results = sync_mod.sync(comp or None, full=full, fetch_attributes=attributes, log=log)
    except KeyError as err:
        typer.echo(f"error: {err.args[0]}", err=True)
        raise typer.Exit(2)
    for r in results:
        if r.checks:
            log(f"{r.competition}: {summarize(r.checks)}")
            for c in r.checks:
                if c.status != "ok":
                    log(f"  {c.status} {c.name}: {c.detail}")
    raise typer.Exit(1 if any(r.failed for r in results) else 0)


@app.command()
def check(tournaments: list[str] = typer.Argument(None, help="Competition key(s); default all loaded")) -> None:
    """Data-quality checks: totals reconcile, targets, limits, ids, true-stat baselines, names, attributes."""
    from pitchside.etl.sync import check_competition

    keys = tournaments or [k for k, t in load_tournaments().items() if t.db_path.exists()]
    failed = False
    for k in keys:
        checks = check_competition(get_tournament(k))
        typer.echo(f"{k}: {summarize(checks)}")
        for c in checks:
            typer.echo(f"  {c.status:<4} {c.name:<20} {c.detail}")
        failed |= any(c.status == "fail" for c in checks)
    raise typer.Exit(1 if failed else 0)


@schedule_app.command("install")
def schedule_install(at: str = typer.Option("09:00", "--at", help="Daily time, HH:MM 24h")) -> None:
    """Register a daily Windows Task Scheduler job running `pitchside sync`."""
    from pitchside.etl import schedule

    typer.echo(_schedule_call(schedule.install, at))


@schedule_app.command("status")
def schedule_status() -> None:
    """Show the scheduled sync job: next run, last run and result."""
    from pitchside.etl import schedule

    typer.echo(_schedule_call(schedule.status))


@schedule_app.command("remove")
def schedule_remove() -> None:
    """Delete the scheduled sync job."""
    from pitchside.etl import schedule

    typer.echo(_schedule_call(schedule.remove))


def _schedule_call(fn, *args) -> str:
    from pitchside.etl.schedule import ScheduleError

    try:
        return fn(*args)
    except ScheduleError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(2)


@cache_app.command("clear")
def cache_clear() -> None:
    """Delete all cached analysis results."""
    typer.echo(f"{cache.clear()} cached results removed")


if __name__ == "__main__":
    app()
