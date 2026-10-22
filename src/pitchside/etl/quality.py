"""Data-quality checks for a competition database (F25).

Run after every load/sync and on demand (`pitchside check`). Each check
returns ok / warn / fail:
- fail: the data or derived tables contradict themselves (totals don't
  reconcile, true stats aren't zero-sum). Analyses can't be trusted.
- warn: real-world quirks or gaps worth a look (umpire-miscounted overs,
  players without attributes, names not yet in aliases.toml).
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

from pitchside.config import load_aliases


@dataclass
class Check:
    name: str
    status: str  # ok / warn / fail
    detail: str


def _count(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> int:
    return con.execute(sql, params or []).fetchone()[0]


def _examples(con: duckdb.DuckDBPyConnection, sql: str, n: int = 5) -> str:
    rows = con.execute(f"{sql} LIMIT {n}").fetchall()
    return "; ".join(", ".join(str(v) for v in r) for r in rows)


def run_checks(con: duckdb.DuckDBPyConnection, registry_attached: bool = False) -> list[Check]:
    """Checks on an open competition database. With `registry_attached`
    (registry ATTACHed as `reg`), also report players missing attributes."""
    checks: list[Check] = []

    def add(name: str, bad: int, detail_sql: str | None, ok_msg: str, bad_msg: str, level: str = "fail") -> None:
        if bad:
            detail = bad_msg.format(n=bad) + (f" e.g. {_examples(con, detail_sql)}" if detail_sql else "")
            checks.append(Check(name, level, detail))
        else:
            checks.append(Check(name, "ok", ok_msg))

    # Innings totals = sum of deliveries; scorecard batting runs + extras = innings total.
    sql = """SELECT i.match_id, i.innings_no, i.runs, d.r FROM innings i
             JOIN (SELECT match_id, innings_no, sum(runs_total) r FROM deliveries GROUP BY ALL) d USING (match_id, innings_no)
             WHERE i.runs <> d.r"""
    add("innings_totals", _count(con, f"SELECT count(*) FROM ({sql})"), sql,
        "innings totals match their deliveries", "{n} innings whose total differs from its deliveries:")
    sql = """SELECT i.match_id, i.innings_no FROM innings i
             JOIN (SELECT match_id, innings_no, sum(runs) r FROM batting_innings GROUP BY ALL) b USING (match_id, innings_no)
             WHERE b.r + i.extras <> i.runs"""
    add("scorecard_totals", _count(con, f"SELECT count(*) FROM ({sql})"), sql,
        "scorecard runs + extras match innings totals", "{n} innings where batters' runs + extras differ:")

    # Targets: with no DLS and no reduction, target = first-innings total + 1.
    sql = """SELECT m.match_id, i1.runs, i2.target_runs FROM matches m
             JOIN innings i1 ON i1.match_id = m.match_id AND i1.innings_no = 1
             JOIN innings i2 ON i2.match_id = m.match_id AND i2.innings_no = 2
             WHERE m.method IS NULL AND i2.target_runs IS NOT NULL AND i2.target_runs <> i1.runs + 1"""
    add("chase_targets", _count(con, f"SELECT count(*) FROM ({sql})"), sql,
        "every non-DLS target is first innings + 1", "{n} chases with an unexpected target:")

    # Wickets and balls within limits.
    sql = "SELECT match_id, innings_no, wickets FROM innings WHERE wickets > 10"
    add("wickets_limit", _count(con, f"SELECT count(*) FROM ({sql})"), sql, "no innings above 10 wickets",
        "{n} innings with more than 10 wickets:")
    sql = """SELECT match_id, innings_no, legal_balls, allocated_balls FROM innings
             WHERE NOT is_super_over AND legal_balls > allocated_balls"""
    add("balls_limit", _count(con, f"SELECT count(*) FROM ({sql})"), sql,
        "no innings exceeds its allocation",
        "{n} innings with more legal balls than allocated (usually umpire-miscounted overs):", level="warn")

    # Results consistency.
    sql = """SELECT match_id, winner, team1, team2 FROM matches
             WHERE winner IS NOT NULL AND winner NOT IN (team1, team2)"""
    add("winners", _count(con, f"SELECT count(*) FROM ({sql})"), sql, "every winner played the match",
        "{n} matches whose winner isn't one of the teams:")

    # Identity: every player resolved through the Cricsheet registry.
    sql = "SELECT player_id, name FROM players WHERE player_id LIKE 'name:%'"
    add("player_ids", _count(con, f"SELECT count(*) FROM ({sql})"), sql,
        "every player has a Cricsheet registry id",
        "{n} players without a registry id (keyed by name):", level="warn")

    # True stats baselines: exactly zero-sum per season.
    tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'").fetchall()}
    if "expected" in tables:
        missing = _count(con, """SELECT count(*) FROM deliveries d JOIN innings i USING (match_id, innings_no)
                                 LEFT JOIN expected e USING (match_id, innings_no, seq)
                                 WHERE NOT i.is_super_over AND e.seq IS NULL""")
        worst = con.execute("""
            SELECT max(abs(d)) FROM (
                SELECT m.season_year,
                       sum(d.runs_batter) FILTER (d.is_ball_faced) - sum(e.exp_bat_runs) AS d
                FROM deliveries d JOIN expected e USING (match_id, innings_no, seq) JOIN matches m USING (match_id)
                GROUP BY 1
                UNION ALL
                SELECT m.season_year, sum(d.bowler_runs) - sum(e.exp_bowl_runs)
                FROM deliveries d JOIN expected e USING (match_id, innings_no, seq) JOIN matches m USING (match_id)
                GROUP BY 1)""").fetchone()[0] or 0
        if missing or worst > 1e-6:
            checks.append(Check("true_stat_baselines", "fail",
                                f"{missing} deliveries without expectations; worst season imbalance {worst:.6f} runs "
                                "(run `pitchside rebuild`)"))
        else:
            checks.append(Check("true_stat_baselines", "ok", "expectations cover every ball and are zero-sum per season"))
    else:
        checks.append(Check("true_stat_baselines", "fail", "no `expected` table (run `pitchside rebuild`)"))

    # Names that may need an alias: raw team / venue names absent from aliases.toml.
    aliases = load_aliases()
    teams = {r[0] for r in con.execute("SELECT team1 FROM matches UNION SELECT team2 FROM matches").fetchall()}
    venues = {r[0] for r in con.execute("SELECT DISTINCT venue FROM matches").fetchall()}
    new_teams = sorted(teams - set(aliases["teams"]))
    new_venues = sorted(venues - set(aliases["venues"]))
    checks.append(Check("team_names", "warn" if new_teams else "ok",
                        f"not in aliases.toml (fine if genuinely new): {'; '.join(new_teams)}" if new_teams
                        else "every team name is in aliases.toml"))
    checks.append(Check("venue_names", "warn" if new_venues else "ok",
                        f"{len(new_venues)} not in aliases.toml (fine if no other spelling exists): "
                        f"{'; '.join(new_venues[:8])}" if new_venues else "every venue name is in aliases.toml"))

    if registry_attached:
        sql = """SELECT p.name, count(*) AS balls FROM deliveries d JOIN players p ON p.player_id = d.bowler_id
                 LEFT JOIN reg.player_attributes a ON a.player_id = d.bowler_id
                 WHERE a.bowl_type IS NULL GROUP BY 1 ORDER BY 2 DESC"""
        add("bowling_types", _count(con, f"SELECT count(*) FROM ({sql})"), sql, "every bowler has a bowling type",
            "{n} bowlers without a bowling type (fill data/player_attributes.csv):", level="warn")
        sql = """SELECT p.name, count(*) AS balls FROM deliveries d JOIN players p ON p.player_id = d.batter_id
                 LEFT JOIN reg.player_attributes a ON a.player_id = d.batter_id
                 WHERE a.batting_hand IS NULL GROUP BY 1 ORDER BY 2 DESC"""
        add("batting_hands", _count(con, f"SELECT count(*) FROM ({sql})"), sql, "every batter has a batting hand",
            "{n} batters without a batting hand (fill data/player_attributes.csv):", level="warn")
    return checks


def summarize(checks: list[Check]) -> str:
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    return f"{len(checks)} checks: {fails} fail, {warns} warn"
