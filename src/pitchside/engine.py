"""Query engine: one in-memory DuckDB connection over the competition databases.

Each loaded competition is ATTACHed read-only next to the registry, and temp
views stitch them together, so every analysis is written once against:

- v_balls : every non-super-over delivery, with match context, attributes
            of the batter (hand) and bowler (type, subtype, arm), and the
            per-ball expectations behind true stats (model/baselines.py)
- v_bat   : batting facts, one row per ball faced plus one row per non-striker
            dismissal, keyed by `player_id` = the batter
- v_bowl  : bowling facts, one row per delivery, keyed by `player_id` = the bowler
- v_team  : team facts, every delivery twice (side = 'batting' / 'bowling')
- v_results : one row per match per team, with the outcome for that team

Shared column names across v_bat / v_bowl / v_team (team, opposition,
batting_hand, bowl_type, position...) are what lets one filter grammar serve
every role. Team and venue names are canonicalised through aliases.toml:
`team` / `opposition` are franchises under their current name ("Delhi
Daredevils" seasons count as Delhi Capitals), `team_name` / `opposition_name`
are the names used that season, and `venue` is one name per ground.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import duckdb

from pitchside import config
from pitchside.config import REGISTRY_DB, Tournament, load_aliases, load_tournaments

V_MATCHES = """
CREATE OR REPLACE TEMP VIEW v_matches AS
SELECT m.* REPLACE (coalesce(va.canonical, m.venue) AS venue),
       m.venue AS venue_raw,
       coalesce(t1.canonical, m.team1) AS franchise1,
       coalesce(t2.canonical, m.team2) AS franchise2,
       coalesce(tw.canonical, m.winner) AS winner_franchise,
       CASE WHEN m.stage IS NULL THEN 'league' ELSE 'playoffs' END AS stage_group
FROM v_matches_raw m
LEFT JOIN venue_alias va ON va.name = m.venue
LEFT JOIN team_alias t1 ON t1.name = m.team1
LEFT JOIN team_alias t2 ON t2.name = m.team2
LEFT JOIN team_alias tw ON tw.name = m.winner
"""

V_INNINGS = """
CREATE OR REPLACE TEMP VIEW v_innings AS
SELECT i.*, coalesce(ba.canonical, i.batting_team) AS batting_franchise,
       coalesce(bo.canonical, i.bowling_team) AS bowling_franchise
FROM v_innings_raw i
LEFT JOIN team_alias ba ON ba.name = i.batting_team
LEFT JOIN team_alias bo ON bo.name = i.bowling_team
"""

V_BALLS = """
CREATE OR REPLACE TEMP VIEW v_balls AS
SELECT d.*, m.competition, m.season_year, m.match_date, m.venue, m.city, m.stage_group AS stage,
       i.batting_team, i.bowling_team, i.batting_franchise, i.bowling_franchise,
       bat.batting_hand AS batter_hand, ns.batting_hand AS non_striker_hand,
       coalesce(bwl.bowl_type, 'unknown') AS bowl_type, bwl.bowl_subtype, bwl.bowling_arm,
       e.exp_bat_runs, e.exp_bat_out, e.exp_bat_dot, e.exp_bat_bnd,
       e.exp_bowl_runs, e.exp_bowl_wkt, e.exp_bowl_dot, e.exp_bowl_bnd,
       bo1.entry_legal_balls AS batter_entry_balls, bo1.entry_wickets AS batter_entry_wickets,
       bo2.entry_legal_balls AS non_striker_entry_balls, bo2.entry_wickets AS non_striker_entry_wickets
FROM v_deliveries_raw d
JOIN v_matches m USING (match_id)
JOIN v_innings i USING (match_id, innings_no)
LEFT JOIN v_expected_raw e USING (match_id, innings_no, seq)
LEFT JOIN v_batting_order_raw bo1
       ON bo1.match_id = d.match_id AND bo1.innings_no = d.innings_no AND bo1.player_id = d.batter_id
LEFT JOIN v_batting_order_raw bo2
       ON bo2.match_id = d.match_id AND bo2.innings_no = d.innings_no AND bo2.player_id = d.non_striker_id
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
       batting_franchise AS team, bowling_franchise AS opposition,
       batting_team AS team_name, bowling_team AS opposition_name,
       batting_position AS position, batter_hand AS batting_hand,
       batter_balls_before + 1 AS nth_ball,
       batter_entry_balls AS entry_balls, batter_entry_balls // 6 + 1 AS entry_over,
       batter_entry_wickets AS entry_wickets,
       runs_batter AS runs, is_ball_faced::INT AS faced,
       (is_ball_faced AND runs_batter = 0)::INT AS dot,
       is_four::INT AS fours, is_six::INT AS sixes,
       coalesce(out_player_id = batter_id, false)::INT AS out,
       exp_bat_runs AS exp_runs, exp_bat_out AS exp_out, exp_bat_dot AS exp_dot, exp_bat_bnd AS exp_bnd
FROM v_balls
UNION ALL
SELECT non_striker_id, {CONTEXT_COLS},
       batting_franchise, bowling_franchise, batting_team, bowling_team,
       non_striker_position, non_striker_hand,
       NULL, non_striker_entry_balls, non_striker_entry_balls // 6 + 1, non_striker_entry_wickets,
       0, 0, 0, 0, 0, 1, 0, 0, 0, 0
FROM v_balls WHERE out_player_id = non_striker_id
"""

V_BOWL = f"""
CREATE OR REPLACE TEMP VIEW v_bowl AS
SELECT bowler_id AS player_id, {CONTEXT_COLS},
       bowling_franchise AS team, batting_franchise AS opposition,
       bowling_team AS team_name, batting_team AS opposition_name,
       batting_position AS position, batter_hand AS batting_hand,
       batter_balls_before + 1 AS nth_ball,
       -- entry state of the batter being bowled to
       batter_entry_balls AS entry_balls, batter_entry_balls // 6 + 1 AS entry_over,
       batter_entry_wickets AS entry_wickets,
       is_legal::INT AS balls, bowler_runs AS runs,
       is_bowler_wicket::INT AS wickets,
       (is_legal AND bowler_runs = 0)::INT AS dot,
       is_four::INT AS fours, is_six::INT AS sixes,
       (wides > 0)::INT AS wides, (noballs > 0)::INT AS noballs,
       exp_bowl_runs AS exp_runs, exp_bowl_wkt AS exp_wkts, exp_bowl_dot AS exp_dot, exp_bowl_bnd AS exp_bnd
FROM v_balls
"""

TEAM_SIDE = """
SELECT '{side}' AS side, {cols},
       {team}_franchise AS team, {opp}_franchise AS opposition,
       {team}_team AS team_name, {opp}_team AS opposition_name,
       batting_position AS position, batter_hand AS batting_hand,
       batter_balls_before + 1 AS nth_ball,
       runs_total AS runs, runs_extras AS extras, is_legal::INT AS balls,
       is_wicket::INT AS wickets, (is_legal AND runs_total = 0)::INT AS dot,
       is_four::INT AS fours, is_six::INT AS sixes
FROM v_balls
"""
V_TEAM = ("CREATE OR REPLACE TEMP VIEW v_team AS "
          + TEAM_SIDE.format(side="batting", team="batting", opp="bowling", cols=CONTEXT_COLS)
          + " UNION ALL "
          + TEAM_SIDE.format(side="bowling", team="bowling", opp="batting", cols=CONTEXT_COLS))

# Outcome per team per match. innings_no = 1 if the team batted first, so the
# `innings` (setting / chasing) filter works on results too.
V_RESULTS = """
CREATE OR REPLACE TEMP VIEW v_results AS
WITH sides AS (
    SELECT m.*, s.k,
           CASE s.k WHEN 1 THEN m.team1 ELSE m.team2 END AS team_name,
           CASE s.k WHEN 1 THEN m.team2 ELSE m.team1 END AS opposition_name,
           CASE s.k WHEN 1 THEN m.franchise1 ELSE m.franchise2 END AS team,
           CASE s.k WHEN 1 THEN m.franchise2 ELSE m.franchise1 END AS opposition
    FROM v_matches m CROSS JOIN (VALUES (1), (2)) s(k)
)
SELECT s.match_id, s.competition, s.season_year, s.match_date, s.venue, s.city,
       s.stage_group AS stage, s.team, s.opposition, s.team_name, s.opposition_name,
       CASE WHEN s.result = 'no result' THEN 'no result'
            WHEN s.winner = s.team_name THEN 'won'
            WHEN s.winner IS NULL THEN 'tied'
            ELSE 'lost' END AS outcome,
       s.result = 'tie' AS tied,
       CASE WHEN i1.batting_team = s.team_name THEN 1 WHEN i1.batting_team IS NOT NULL THEN 2 END AS innings_no,
       s.toss_winner = s.team_name AS won_toss, s.toss_decision, s.method
FROM sides s
LEFT JOIN v_innings i1 ON i1.match_id = s.match_id AND i1.innings_no = 1
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


class NameNotFound(LookupError):
    """A team or venue name that matches nothing, or several things."""

    def __init__(self, kind: str, query: str, candidates: list[str]):
        self.kind, self.query, self.candidates = kind, query, candidates
        hint = "; ".join(candidates[:8])
        super().__init__(f"No unique {kind} for {query!r}" + (f". Candidates: {hint}" if hint else ""))


def _resolve_name(kind: str, query: str, names: dict[str, str]) -> str:
    """Resolve against {name_or_alias: canonical}: exact (case-insensitive), then contains."""
    q = query.strip().lower()
    exact = {c for n, c in names.items() if n.lower() == q}
    if len(exact) == 1:
        return exact.pop()
    found = sorted({c for n, c in names.items() if q in n.lower()})
    if len(found) == 1:
        return found[0]
    raise NameNotFound(kind, query, found or sorted(set(names.values())))


@dataclass
class Engine:
    con: duckdb.DuckDBPyConnection
    competitions: list[Tournament]
    data_versions: dict[str, int]
    names: dict[str, str] = field(default_factory=dict)
    team_names: dict[str, str] = field(default_factory=dict)   # name / old name / code -> franchise
    venue_names: dict[str, str] = field(default_factory=dict)  # name / old name -> canonical ground

    priors: dict[tuple[str, str], float] = field(default_factory=dict)  # (role, component) -> k

    def shrink_k(self, role: str, component: str) -> float:
        """Shrinkage prior strength (balls) for a per-ball rate (model/shrinkage.py)."""
        if (role, component) not in self.priors:
            raise LookupError(f"No shrinkage prior for {role} {component}; run `pitchside rebuild <competition>`")
        return self.priors[(role, component)]

    def resolve_team(self, query: str) -> str:
        """Canonical franchise for a team name, former name or code ('RCB', 'Kings XI')."""
        return _resolve_name("team", query, self.team_names)

    def resolve_venue(self, query: str) -> str:
        """Canonical ground for a venue name or part of one ('Chepauk', 'Wankhede')."""
        return _resolve_name("venue", query, self.venue_names)

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


LOCK_DOWN = """
SET autoinstall_known_extensions = false;
SET autoload_known_extensions = false;
SET enable_external_access = false;
SET lock_configuration = true;
"""


SCRATCH_CATALOG = """
CREATE TABLE IF NOT EXISTS scratch._catalog (
    name VARCHAR PRIMARY KEY, created_at TIMESTAMP, sql VARCHAR, versions VARCHAR, rows BIGINT, columns VARCHAR
)
"""


def open_engine(competitions: list[str] | None = None, locked: bool = False,
                scratch: str | None = None) -> Engine:
    """Open the engine over the given competition keys (default: all loaded).

    `locked` is for running SQL from outside (the MCP `query` tool): once the
    views exist, the connection can no longer touch the filesystem (no
    read_csv / COPY / ATTACH / extension installs) or change its settings.
    The databases already attached, read-only, stay readable.

    `scratch` attaches the AI scratch database (config.SCRATCH_DB) as `scratch`:
    'ro' read-only (if it exists), 'rw' writable (created if missing).
    """
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

    aliases = load_aliases()
    for kind in ("team", "venue"):
        con.execute(f"CREATE TEMP TABLE {kind}_alias (name VARCHAR PRIMARY KEY, canonical VARCHAR)")
        if aliases[f"{kind}s"]:
            con.executemany(f"INSERT INTO {kind}_alias VALUES (?, ?)", list(aliases[f"{kind}s"].items()))

    def union(table: str) -> str:
        return " UNION ALL BY NAME ".join(f"SELECT * FROM {t.key}.{table}" for t in selected)

    for t in selected:
        has = con.execute("SELECT count(*) FROM duckdb_tables() WHERE database_name = ? AND table_name = 'expected'",
                          [t.key]).fetchone()[0]
        if not has:
            raise FileNotFoundError(f"Competition {t.key!r} has no true-stat baselines; run `pitchside rebuild {t.key}`")
    for table in ("matches", "innings", "deliveries", "expected", "batting_order"):
        con.execute(f"CREATE OR REPLACE TEMP VIEW v_{table}_raw AS {union(table)}")
    con.execute("CREATE OR REPLACE TEMP VIEW v_players AS SELECT player_id, any_value(name) AS name "
                f"FROM ({union('players')}) GROUP BY player_id")
    for view in (V_MATCHES, V_INNINGS, V_BALLS, V_BAT, V_BOWL, V_TEAM, V_RESULTS):
        con.execute(view)

    names = dict(con.execute("SELECT player_id, name FROM v_players").fetchall())
    # Different players can share a name (two 'Harmeet Singh's): keep display names unique.
    counts = Counter(names.values())
    names = {pid: f"{n} ({pid})" if counts[n] > 1 else n for pid, n in names.items()}

    # Every name a team or ground appears under, plus configured aliases and codes.
    team_names = {n: c for n, c in con.execute(
        "SELECT team_name, any_value(team) FROM v_results GROUP BY 1").fetchall()}
    team_names |= {n: c for n, c in aliases["teams"].items() if c in team_names.values()}
    venue_names = {n: c for n, c in con.execute(
        "SELECT venue_raw, any_value(venue) FROM v_matches GROUP BY 1").fetchall()}
    venue_names |= {c: c for c in venue_names.values()}
    # Shrinkage priors: per competition, combined by number of player-seasons when several are open.
    prior_parts = [f"SELECT * FROM {t.key}.shrink_priors" for t in selected
                   if con.execute("SELECT count(*) FROM duckdb_tables() WHERE database_name = ? "
                                  "AND table_name = 'shrink_priors'", [t.key]).fetchone()[0]]
    priors = {}
    if prior_parts:
        priors = {(r, c): k for r, c, k in con.execute(
            # weight >= 1 so a competition with no qualifying player-seasons still counts
            "SELECT role, component, sum(k * greatest(n_units, 1)) / sum(greatest(n_units, 1)) FROM (" +
            " UNION ALL ".join(prior_parts) +
            ") GROUP BY ALL").fetchall()}

    if scratch == "rw" or (scratch == "ro" and config.SCRATCH_DB.exists()):
        config.SCRATCH_DB.parent.mkdir(parents=True, exist_ok=True)
        mode = " (READ_ONLY)" if scratch == "ro" else ""
        con.execute(f"ATTACH '{config.SCRATCH_DB.as_posix()}' AS scratch{mode}")
        if scratch == "rw":
            con.execute(SCRATCH_CATALOG)

    if locked:
        con.execute(LOCK_DOWN)
    return Engine(con=con, competitions=selected, data_versions=versions, names=names,
                  team_names=team_names, venue_names=venue_names, priors=priors)
