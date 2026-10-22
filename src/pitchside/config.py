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
ALIASES_TOML = ROOT / "aliases.toml"

CRICSHEET_BASE = "https://cricsheet.org"


@dataclass(frozen=True)
class Tournament:
    key: str
    name: str
    cricsheet: str
    event: str
    format: str
    status: str
    rule_eras: tuple[int, ...] = ()

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
    return {key: Tournament(key=key, **{**cfg, "rule_eras": tuple(cfg.get("rule_eras", ()))})
            for key, cfg in raw.items()}


def get_tournament(key: str) -> Tournament:
    tournaments = load_tournaments()
    if key not in tournaments:
        raise KeyError(f"Unknown tournament {key!r}; known: {', '.join(tournaments)}")
    return tournaments[key]


def load_aliases(path: Path = ALIASES_TOML) -> dict[str, dict[str, str]]:
    """{'teams': {name_or_code: canonical}, 'venues': {...}}; every canonical name maps to itself."""
    raw = {}
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    out: dict[str, dict[str, str]] = {}
    for kind in ("teams", "venues"):
        mapping: dict[str, str] = {}
        for canonical, others in raw.get(kind, {}).items():
            for name in [canonical, *others]:
                if mapping.get(name, canonical) != canonical:
                    raise ValueError(f"{kind} alias {name!r} maps to both {mapping[name]!r} and {canonical!r}")
                mapping[name] = canonical
        out[kind] = mapping
    return out
