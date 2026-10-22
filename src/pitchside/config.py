"""Paths and the tournament registry (tournaments.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(os.environ.get("PITCHSIDE_HOME", Path(__file__).resolve().parents[2]))
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_DIR = ROOT / "db"
REGISTRY_DB = DB_DIR / "registry.duckdb"
CACHE_DB = DB_DIR / "cache.duckdb"
ATTRIBUTES_CSV = DATA_DIR / "player_attributes.csv"
TOURNAMENTS_TOML = ROOT / "tournaments.toml"

CRICSHEET_BASE = "https://cricsheet.org"


@dataclass(frozen=True)
class Tournament:
    key: str
    name: str
    cricsheet: str
    event: str
    format: str
    status: str

    @property
    def zip_url(self) -> str:
        return f"{CRICSHEET_BASE}/downloads/{self.cricsheet}_json.zip"

    @property
    def zip_path(self) -> Path:
        return RAW_DIR / self.key / f"{self.cricsheet}_json.zip"

    @property
    def db_path(self) -> Path:
        return DB_DIR / f"{self.key}.duckdb"


def load_tournaments(path: Path = TOURNAMENTS_TOML) -> dict[str, Tournament]:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return {key: Tournament(key=key, **cfg) for key, cfg in raw.items()}


def get_tournament(key: str) -> Tournament:
    tournaments = load_tournaments()
    if key not in tournaments:
        raise KeyError(f"Unknown tournament {key!r}; known: {', '.join(tournaments)}")
    return tournaments[key]
