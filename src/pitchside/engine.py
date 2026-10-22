"""Query engine: one in-memory DuckDB connection over the competition databases.

Each loaded competition is ATTACHed read-only next to the registry, and temp
views stitch them together, so every analysis is written once against:

- v_balls : every non-super-over delivery, with match context and attributes
            of the batter (hand) and bowler (type, subtype, arm)
- v_bat   : batting facts, one row per ball faced plus one row per non-striker
            dismissal, keyed by `player_id` = the batter
- v_bowl  : bowling facts, one row per delivery, keyed by `player_id` = the bowler

Shared column names across v_bat / v_bowl (team, opposition, batting_hand,
bowl_type, position...) are what lets one filter grammar serve both roles.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import duckdb

from pitchside.config import REGISTRY_DB, Tournament, load_tournaments

V_BALLS_PART = """
SELECT d.*, m.competition, m.season_year, m.match_date, m.venue, m.city,
       CASE WHEN m.stage IS NULL THEN 'league' ELSE 'playoffs' END AS stage,
       i.batting_team, i.bowling_team,
       bat.batting_hand AS batter_hand, ns.batting_hand AS non_striker_hand,
       coalesce(bwl.bowl_type, 'unknown') AS bowl_type, bwl.bowl_subtype, bwl.bowling_arm
FROM {db}.deliveries d
JOIN {db}.matches m USING (match_id)
JOIN {db}.innings i USING (match_id, innings_no)
LEFT JOIN reg.player_attributes bat ON bat.player_id = d.batter_id
LEFT JOIN reg.player_attributes ns  ON ns.player_id  = d.non_striker_id
LEFT JOIN reg.player_attributes bwl ON bwl.player_id = d.bowler_id
WHERE NOT i.is_super_over
"""

CONTEXT_COLS = """match_id, innings_no, seq, competition, season_year, match_date, venue, city, stage,
       phase, over_no, wickets_before, score_before, balls_remaining,
       bowl_type, bowl_subtype, bowling_arm, bowler_id, batter_id"""

V_BAT = f"""
CREATE OR REPLACE TEMP VIEW v_bat AS
SELECT batter_id AS player_id, {CONTEXT_COLS},
       batting_team AS team, bowling_team AS opposition,
       batting_position AS position, batter_hand AS batting_hand,
       batter_balls_before + 1 AS nth_ball,
       runs_batter AS runs, is_ball_faced::INT AS faced,
       (is_ball_faced AND runs_batter = 0)::INT AS dot,
       is_four::INT AS fours, is_six::INT AS sixes,
       coalesce(out_player_id = batter_id, false)::INT AS out
FROM v_balls
UNION ALL
SELECT non_striker_id, {CONTEXT_COLS},
       batting_team, bowling_team,
       non_striker_position, non_striker_hand,
       NULL, 0, 0, 0, 0, 0, 1
FROM v_balls WHERE out_player_id = non_striker_id
"""

V_BOWL = f"""
CREATE OR REPLACE TEMP VIEW v_bowl AS
SELECT bowler_id AS player_id, {CONTEXT_COLS},
       bowling_team AS team, batting_team AS opposition,
       batting_position AS position, batter_hand AS batting_hand,
       batter_balls_before + 1 AS nth_ball,
       is_legal::INT AS balls, bowler_runs AS runs,
       is_bowler_wicket::INT AS wickets,
       (is_legal AND bowler_runs = 0)::INT AS dot,
       is_four::INT AS fours, is_six::INT AS sixes,
       (wides > 0)::INT AS wides, (noballs > 0)::INT AS noballs
FROM v_balls
"""

REGISTRY_TABLES = {
    "player_attributes": "(player_id VARCHAR, batting_hand VARCHAR, bowling_arm VARCHAR, "
                         "bowling_style VARCHAR, bowl_type VARCHAR, bowl_subtype VARCHAR)",
    "people": "(identifier VARCHAR, unique_name VARCHAR)",
    "people_names": "(identifier VARCHAR, name VARCHAR)",
}


class PlayerNotFound(LookupError):
    def __init__(self, query: str, candidates: list[dict]):
        self.query, self.candidates = query, candidates
        hint = "; ".join(f"{c['name']} ({c['player_id']})" for c in candidates[:5])
        super().__init__(f"No unique player for {query!r}" + (f". Candidates: {hint}" if hint else ""))


@dataclass
class Engine:
    con: duckdb.DuckDBPyConnection
    competitions: list[Tournament]
    data_versions: dict[str, int]
    names: dict[str, str] = field(default_factory=dict)

    @property
    def version_key(self) -> str:
        return ",".join(f"{k}:{v}" for k, v in sorted(self.data_versions.items()))

    def name(self, player_id: str | None) -> str | None:
        return self.names.get(player_id, player_id) if player_id else None

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- players ---------------------------------------------------------

    def find_player(self, query: str, limit: int = 8) -> list[dict]:
        """Players matching a name (exact, then contains, then fuzzy), most active first."""
        q = query.strip()
        rows = self.con.execute("""
            WITH names AS (
                SELECT player_id, name AS alias, true AS is_primary FROM v_players
                UNION SELECT identifier, unique_name, false FROM reg.people
                    WHERE identifier IN (SELECT player_id FROM v_players)
                UNION SELECT identifier, name, false FROM reg.people_names
                    WHERE identifier IN (SELECT player_id FROM v_players)
            ),
            scored AS (
                SELECT player_id,
                       max(CASE WHEN lower(alias) = lower($q) AND is_primary THEN 4
                                WHEN lower(alias) = lower($q) THEN 3
                                WHEN alias ILIKE '%' || $q || '%' THEN 2
                                WHEN jaro_winkler_similarity(lower(alias), lower($q)) > 0.88 THEN 1
                                ELSE 0 END) AS score
                FROM names GROUP BY player_id
            ),
            activity AS (
                SELECT player_id, count(*) AS balls, min(season_year) AS first, max(season_year) AS last,
                       string_agg(DISTINCT competition, ',') AS comps
                FROM (SELECT batter_id AS player_id, season_year, competition FROM v_balls
                      UNION ALL SELECT bowler_id, season_year, competition FROM v_balls)
                GROUP BY player_id
            )
            SELECT s.player_id, p.name, s.score, coalesce(a.balls, 0) AS balls_involved,
                   a.first, a.last, a.comps
            FROM scored s JOIN v_players p USING (player_id) LEFT JOIN activity a USING (player_id)
            WHERE s.score > 0
            ORDER BY s.score DESC, balls_involved DESC LIMIT $limit
        """, {"q": q, "limit": limit}).fetchall()
        return [
            {"player_id": r[0], "name": r[1], "match": ["", "fuzzy", "contains", "alias", "exact"][r[2]],
             "balls_involved": r[3], "seasons": f"{r[4]}-{r[5]}" if r[4] else "", "competitions": r[6]}
            for r in rows
        ]

    def resolve_player(self, query: str) -> str:
        """Player id for an id or a name; raises PlayerNotFound if ambiguous."""
        if query in self.names:
            return query
        cands = self.find_player(query)
        exact = [c for c in cands if c["match"] == "exact"]
        if len(exact) == 1:
            return exact[0]["player_id"]
        # A clear front-runner by activity wins ('Rashid' -> Rashid Khan, not Adil Rashid).
        ranked = sorted(cands, key=lambda c: c["balls_involved"], reverse=True)
        if len(ranked) == 1 or (len(ranked) > 1 and ranked[0]["balls_involved"] > 10 * max(ranked[1]["balls_involved"], 1)):
            return ranked[0]["player_id"]
        aliases = [c for c in cands if c["match"] == "alias"]
        if len(aliases) == 1:
            return aliases[0]["player_id"]
        raise PlayerNotFound(query, cands)


def open_engine(competitions: list[str] | None = None) -> Engine:
    """Open the engine over the given competition keys (default: all loaded)."""
    tournaments = load_tournaments()
    keys = competitions or [k for k, t in tournaments.items() if t.db_path.exists()]
    selected = []
    for k in keys:
        if k not in tournaments:
            raise KeyError(f"Unknown competition {k!r}; known: {', '.join(tournaments)}")
        if not tournaments[k].db_path.exists():
            raise FileNotFoundError(f"Competition {k!r} is not loaded; run `pitchside load {k}`")
        selected.append(tournaments[k])
    if not selected:
        raise FileNotFoundError("No competitions loaded; run `pitchside load <key>`")

    # `reg` exposes the registry tables, or empty stand-ins when it hasn't been built.
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS reg")
    tables: set[str] = set()
    if REGISTRY_DB.exists():
        con.execute(f"ATTACH '{REGISTRY_DB.as_posix()}' AS regdb (READ_ONLY)")
        tables = {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'regdb'").fetchall()}
    for table, empty in REGISTRY_TABLES.items():
        con.execute(f"CREATE VIEW reg.{table} AS SELECT * FROM regdb.{table}" if table in tables
                    else f"CREATE TABLE reg.{table} {empty}")

    versions = {}
    for t in selected:
        con.execute(f"ATTACH '{t.db_path.as_posix()}' AS {t.key} (READ_ONLY)")
        row = con.execute(f"SELECT value FROM {t.key}.meta WHERE key = 'data_version'").fetchone()
        versions[t.key] = int(row[0]) if row else 0

    con.execute("CREATE OR REPLACE TEMP VIEW v_balls AS " +
                " UNION ALL ".join(V_BALLS_PART.format(db=t.key) for t in selected))
    con.execute("CREATE OR REPLACE TEMP VIEW v_players AS SELECT player_id, any_value(name) AS name FROM (" +
                " UNION ALL ".join(f"SELECT * FROM {t.key}.players" for t in selected) + ") GROUP BY player_id")
    con.execute("CREATE OR REPLACE TEMP VIEW v_matches AS " +
                " UNION ALL BY NAME ".join(f"SELECT * FROM {t.key}.matches" for t in selected))
    con.execute(V_BAT)
    con.execute(V_BOWL)

    names = dict(con.execute("SELECT player_id, name FROM v_players").fetchall())
    # Different players can share a name (two 'Harmeet Singh's): keep display names unique.
    counts = Counter(names.values())
    names = {pid: f"{n} ({pid})" if counts[n] > 1 else n for pid, n in names.items()}
    return Engine(con=con, competitions=selected, data_versions=versions, names=names)
