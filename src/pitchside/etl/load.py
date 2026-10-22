"""Load parsed matches into a competition database."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path

import duckdb
import pyarrow as pa

from pitchside.config import Tournament
from pitchside.etl.parse import MatchRows, parse_match
from pitchside.model import derive

CORE_TABLES = (
    "matches", "match_players", "innings", "deliveries",
    "wickets", "batting_order", "replacements",
)

# Cricsheet match_type values accepted for each tournament format.
MATCH_TYPES = {"T20": {"T20", "IT20"}, "ODI": {"ODI", "ODM"}}


@dataclass
class LoadResult:
    added: int
    updated: int
    skipped: int
    data_version: int


def connect(db_path: Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=read_only)
    if not read_only:
        con.execute(resources.files("pitchside.model").joinpath("schema.sql").read_text())
    return con


def iter_zip(zip_path: Path) -> Iterator[tuple[str, dict, str]]:
    """Yield (match_id, match JSON, file name) for every match in a Cricsheet zip."""
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not name.endswith(".json"):
                continue
            yield Path(name).stem, json.loads(z.read(name)), name


def _insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    staged = pa.Table.from_pylist(rows)
    con.register("_staged", staged)
    try:
        con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM _staged")
    finally:
        con.unregister("_staged")


def _bump_version(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
    version = int(row[0]) + 1 if row else 1
    con.execute("INSERT OR REPLACE INTO meta VALUES ('data_version', ?)", [str(version)])
    return version


def load_matches(
    con: duckdb.DuckDBPyConnection,
    tournament: Tournament,
    matches: Iterable[tuple[str, dict, str]],
    source: str,
    filter_event: bool = False,
) -> LoadResult:
    """Insert new matches and replace ones Cricsheet has revised.

    Matches already loaded at the same revision are skipped, so loading the
    same file twice is a no-op. With `filter_event`, matches from other
    competitions (as found in the shared recently-added zips) are ignored.
    """
    existing = dict(con.execute("SELECT match_id, revision FROM matches").fetchall())
    allowed_types = MATCH_TYPES.get(tournament.format, {tournament.format})
    batch = MatchRows()
    replaced: list[str] = []
    added = skipped = 0

    for match_id, data, name in matches:
        info = data["info"]
        if filter_event and info.get("event", {}).get("name") != tournament.event:
            continue
        if info.get("match_type") not in allowed_types:
            skipped += 1
            continue
        revision = int(data.get("meta", {}).get("revision", 1))
        if match_id in existing:
            if revision <= existing[match_id]:
                skipped += 1
                continue
            replaced.append(match_id)
        else:
            added += 1
        batch.extend(parse_match(match_id, data, tournament.key, source_file=name))

    version = int((con.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone() or [0])[0])
    if added or replaced:
        con.execute("BEGIN TRANSACTION")
        try:
            if replaced:
                for table in CORE_TABLES:
                    con.execute(f"DELETE FROM {table} WHERE match_id IN (SELECT unnest(?))", [replaced])
            for table in CORE_TABLES:
                _insert(con, table, getattr(batch, table))
            _upsert_players(con, batch.players)
            derive.refresh(con)
            version = _bump_version(con)
            con.execute("INSERT OR REPLACE INTO meta VALUES ('last_load', ?)", [datetime.now().isoformat(timespec="seconds")])
            con.execute(
                "INSERT INTO ingest_log VALUES (?, ?, ?, ?, ?)",
                [datetime.now(), source, added, len(replaced), version],
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return LoadResult(added=added, updated=len(replaced), skipped=skipped, data_version=version)


def _upsert_players(con: duckdb.DuckDBPyConnection, players: dict[str, str]) -> None:
    if not players:
        return
    staged = pa.Table.from_pylist([{"player_id": k, "name": v} for k, v in players.items()])
    con.register("_staged", staged)
    try:
        con.execute("INSERT OR REPLACE INTO players SELECT player_id, name FROM _staged")
    finally:
        con.unregister("_staged")
