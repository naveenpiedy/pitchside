"""Result cache for lazily computed analyses (F5).

Keyed by (function, arguments, data versions of the competitions involved), so
any new load bumps a data_version and old entries simply stop matching. Lives
in its own file so competition databases stay read-only for analysis.
The cache is best-effort: if it can't be opened (e.g. locked by another
process), results are computed without it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta

import duckdb

from pitchside.config import CACHE_DB
from pitchside.stats.table import Table

DDL = """
CREATE TABLE IF NOT EXISTS cache (
    key        VARCHAR PRIMARY KEY,
    fn         VARCHAR NOT NULL,
    versions   VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    result     JSON NOT NULL
)
"""

ENABLED = True
MAX_AGE = timedelta(days=60)


def _json_default(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    raise TypeError(type(v))


def _key(fn: str, args: dict, versions: str) -> str:
    raw = json.dumps([fn, args, versions], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def cached(fn: str, args: dict, versions: str, compute: Callable[[], Table | list[Table]]):
    """Return compute()'s result, from the cache when an entry exists for these args/versions."""
    if not ENABLED:
        return compute()
    key = _key(fn, args, versions)
    try:
        CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(CACHE_DB))
    except duckdb.Error:
        return compute()
    try:
        con.execute(DDL)
        row = con.execute("SELECT result FROM cache WHERE key = ?", [key]).fetchone()
        if row:
            data = json.loads(row[0])
            return [Table.from_dict(d) for d in data] if isinstance(data, list) else Table.from_dict(data)
        result = compute()
        payload = [t.to_dict() for t in result] if isinstance(result, list) else result.to_dict()
        con.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?, ?)",
                    [key, fn, versions, datetime.now(), json.dumps(payload, default=_json_default)])
        con.execute("DELETE FROM cache WHERE created_at < ?", [datetime.now() - MAX_AGE])
        return result
    finally:
        con.close()


def clear() -> int:
    if not CACHE_DB.exists():
        return 0
    con = duckdb.connect(str(CACHE_DB))
    try:
        con.execute(DDL)
        n = con.execute("SELECT count(*) FROM cache").fetchone()[0]
        con.execute("DELETE FROM cache")
        return n
    finally:
        con.close()
