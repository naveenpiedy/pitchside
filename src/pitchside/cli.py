"""Command line entry point: `pitchside --help`."""

from __future__ import annotations

import typer

from pitchside.config import get_tournament, load_tournaments
from pitchside.etl.download import download
from pitchside.etl.load import connect, iter_zip, load_matches
from pitchside.etl.register import refresh_register
from pitchside.stats import scorecard as sc

app = typer.Typer(no_args_is_help=True, add_completion=False)


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


if __name__ == "__main__":
    app()
