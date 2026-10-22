"""Keep ongoing competitions current from Cricsheet (F22).

    pitchside sync

For each competition marked `status = "ongoing"` in tournaments.toml:
1. Source: Cricsheet's recently-added zip (30 days, ~1.5 MB, shared by all
   competitions) when the last sync is recent; otherwise, or on first load,
   the competition's full zip. The recent zip's window counts back from
   Cricsheet's own last publish, so the widest window is used and loading is
   idempotent: matches already loaded at the same revision are skipped and
   revised ones replaced.
2. Load (filtered to the competition's event name), refreshing derived tables
   and true-stat baselines and bumping data_version (which invalidates caches).
3. If new matches arrived: refresh the Cricsheet register and fetch batting
   hand / bowling style for new players.
4. Run the data-quality checks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from pitchside import config
from pitchside.config import CRICSHEET_BASE, RAW_DIR, REGISTRY_DB, Tournament
from pitchside.etl.download import download
from pitchside.etl.load import connect, iter_zip, load_matches
from pitchside.etl.quality import Check, run_checks

RECENT_NAME = "recently_added_30_json.zip"
RECENT_URL = f"{CRICSHEET_BASE}/downloads/{RECENT_NAME}"
RECENT_ZIP = RAW_DIR / "recent" / RECENT_NAME
RECENT_MAX_AGE = timedelta(days=25)  # beyond this the 30-day zip might miss matches
LOCK_RETRIES, LOCK_WAIT_S = 6, 5


@dataclass
class SyncResult:
    competition: str
    source: str
    added: int = 0
    updated: int = 0
    data_version: int | None = None
    checks: list[Check] = field(default_factory=list)
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None or any(c.status == "fail" for c in self.checks)


def connect_with_retry(path: Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a database, waiting briefly if another process (e.g. an MCP call) holds it."""
    for attempt in range(LOCK_RETRIES):
        try:
            return connect(path, read_only=read_only)
        except duckdb.IOException:
            if attempt == LOCK_RETRIES - 1:
                raise
            time.sleep(LOCK_WAIT_S)
    raise AssertionError("unreachable")


def last_sync(con: duckdb.DuckDBPyConnection) -> datetime | None:
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    value = meta.get("last_sync") or meta.get("last_load")
    return datetime.fromisoformat(value) if value else None


def choose_source(db_exists: bool, last: datetime | None, now: datetime, full: bool = False) -> str:
    """'recent' (shared 30-day zip) or 'full' (the competition's own zip)."""
    if full or not db_exists or last is None or now - last > RECENT_MAX_AGE:
        return "full"
    return "recent"


def _sync_one(t: Tournament, source: str, recent_zip: Path | None, now: datetime, log) -> SyncResult:
    result = SyncResult(t.key, source)
    if source == "full":
        log(f"{t.key}: downloading {t.zip_url}")
        download(t.zip_url, t.zip_path)
        zip_path, filter_event = t.zip_path, False
    else:
        zip_path, filter_event = recent_zip, True
    con = connect_with_retry(t.db_path)
    try:
        r = load_matches(con, t, iter_zip(zip_path), source=zip_path.name, filter_event=filter_event)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync', ?), ('last_sync_source', ?)",
                    [now.isoformat(timespec="seconds"), zip_path.name])
    finally:
        con.close()
    result.added, result.updated, result.data_version = r.added, r.updated, r.data_version
    log(f"{t.key}: {r.added} added, {r.updated} updated from {zip_path.name} (data_version {r.data_version})")
    return result


def check_competition(t: Tournament) -> list[Check]:
    con = connect_with_retry(t.db_path, read_only=True)
    try:
        if REGISTRY_DB.exists():
            con.execute(f"ATTACH '{REGISTRY_DB.as_posix()}' AS reg (READ_ONLY)")
        return run_checks(con, registry_attached=REGISTRY_DB.exists())
    finally:
        con.close()


def sync(competitions: list[str] | None = None, full: bool = False, fetch_attributes: bool = True,
         recent_zip: Path | None = None, log=print, now: datetime | None = None) -> list[SyncResult]:
    """Sync the given competitions (default: every 'ongoing' one). `recent_zip`
    skips the download (tests, offline use)."""
    now = now or datetime.now()
    tournaments = config.load_tournaments()
    keys = competitions or [k for k, t in tournaments.items() if t.status == "ongoing"]
    unknown = [k for k in keys if k not in tournaments]
    if unknown:
        raise KeyError(f"Unknown competition(s) {', '.join(unknown)}; known: {', '.join(tournaments)}")

    plans: dict[str, str] = {}
    for k in keys:
        t = tournaments[k]
        last = None
        if t.db_path.exists():
            con = connect_with_retry(t.db_path, read_only=True)
            try:
                last = last_sync(con)
            finally:
                con.close()
        plans[k] = choose_source(t.db_path.exists(), last, now, full)

    if "recent" in plans.values() and recent_zip is None:
        log(f"downloading {RECENT_URL}")
        recent_zip = download(RECENT_URL, RECENT_ZIP)

    results = []
    for k in keys:
        try:
            results.append(_sync_one(tournaments[k], plans[k], recent_zip, now, log))
        except Exception as err:  # keep going with the other competitions
            log(f"{k}: sync failed: {err}")
            results.append(SyncResult(k, plans[k], error=str(err)))

    if fetch_attributes and any(r.added for r in results):
        _refresh_players(log)

    for r in results:
        if r.error is None:
            r.checks = check_competition(tournaments[r.competition])
    return results


def _refresh_players(log) -> None:
    """New matches can bring new players: refresh the register and their attributes."""
    from pitchside.etl import attributes as attrs
    from pitchside.etl.register import connect_registry, load_attributes, refresh_register

    try:
        counts = refresh_register(fetch=True)
        log(f"register: {counts['people']} people")
        stats = attrs.fetch_attributes(log=log)
        con = connect_registry()
        try:
            load_attributes(con)
        finally:
            con.close()
        log(f"attributes: {stats['fetched']} new players fetched, {stats['not_found']} without a bowling type")
    except Exception as err:  # attributes are best-effort; the match data is already in
        log(f"player refresh failed (data is loaded; retry with `pitchside register` / `attributes fetch`): {err}")
