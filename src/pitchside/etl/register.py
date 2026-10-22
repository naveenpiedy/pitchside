"""The shared registry database: Cricsheet people plus player attributes.

`people` / `people_names` come from the Cricsheet register. `player_attributes`
comes from data/player_attributes.csv, which is hand-editable and is the source
of truth for batting hand and bowling style (Cricsheet has neither).
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from pitchside.config import ATTRIBUTES_CSV, RAW_DIR, REGISTRY_DB
from pitchside.etl.download import download, register_url

REGISTER_DIR = RAW_DIR / "register"

ATTRIBUTES_DDL = """
CREATE OR REPLACE TABLE player_attributes (
    player_id     VARCHAR PRIMARY KEY,
    name          VARCHAR,
    batting_hand  VARCHAR,   -- 'right' / 'left'
    bowling_arm   VARCHAR,   -- 'right' / 'left'
    bowling_style VARCHAR,   -- as published, e.g. 'Right arm offbreak'
    bowl_type     VARCHAR,   -- 'pace' / 'spin'
    bowl_subtype  VARCHAR,   -- e.g. 'RF', 'RFM', 'LFM', 'OB', 'LB', 'SLA', 'LWS'
    source        VARCHAR
)
"""


def connect_registry(path: Path = REGISTRY_DB, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


def refresh_register(fetch: bool = True) -> dict[str, int]:
    """Download (optionally) and load people.csv / names.csv, then reload attributes."""
    people_csv, names_csv = REGISTER_DIR / "people.csv", REGISTER_DIR / "names.csv"
    if fetch or not people_csv.exists():
        download(register_url("people.csv"), people_csv)
        download(register_url("names.csv"), names_csv)

    con = connect_registry()
    try:
        con.execute("CREATE OR REPLACE TABLE people AS SELECT * FROM read_csv(?, header = true, all_varchar = true)",
                    [str(people_csv)])
        if names_csv.exists():
            con.execute("CREATE OR REPLACE TABLE people_names AS SELECT * FROM read_csv(?, header = true, all_varchar = true)",
                        [str(names_csv)])
        load_attributes(con)
        return {
            "people": con.execute("SELECT count(*) FROM people").fetchone()[0],
            "attributes": con.execute("SELECT count(*) FROM player_attributes").fetchone()[0],
        }
    finally:
        con.close()


def load_attributes(con: duckdb.DuckDBPyConnection, csv_path: Path = ATTRIBUTES_CSV) -> None:
    con.execute(ATTRIBUTES_DDL)
    if csv_path.exists():
        con.execute(
            "INSERT INTO player_attributes BY NAME SELECT * FROM read_csv(?, header = true, all_varchar = true)",
            [str(csv_path)],
        )
