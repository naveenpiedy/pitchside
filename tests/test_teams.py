import pytest

from conftest import rename_team
from pitchside.config import get_tournament, load_aliases
from pitchside.engine import NameNotFound, open_engine
from pitchside.stats import teams
from pitchside.stats.splits import aggregate


def records(table):
    return table.records()


def test_results_from_each_side(mini_engine):
    # MINI_MATCH: tied, Blues won the super over; Reds batted first.
    reds = records(teams.results(mini_engine, {"team": "Reds"}))[0]
    assert (reds["matches"], reds["won"], reds["lost"], reds["tied"]) == (1, 0, 1, 1)
    blues = records(teams.results(mini_engine, {"team": "BL"}))[0]  # by code
    assert (blues["won"], blues["won_chasing"]) == (1, 1)
    # Ball-level filters are reported, not silently applied to results.
    t = teams.results(mini_engine, {"team": "Reds", "phase": "death"})
    assert any("phase" in n for n in t.notes)


def test_team_facts_both_sides(mini_engine):
    bat = records(aggregate(mini_engine, "team_batting", {"team": "Reds"}))[0]
    assert (bat["runs"], bat["balls"], bat["wickets"]) == (22, 7, 2)  # extras included
    bowl = records(aggregate(mini_engine, "team_bowling", {"team": "Blues"}))[0]
    assert (bowl["runs"], bowl["balls"], bowl["wickets"]) == (22, 7, 2)
    with pytest.raises(Exception, match="team-level"):
        aggregate(mini_engine, "team_batting", by=["player"])


SIDES_BY_INNINGS = """
SELECT innings_no, side, team, opposition, sum(runs) FROM v_team
GROUP BY innings_no, side, team, opposition ORDER BY innings_no, side
"""
# A CTE plus a window: the shape in which DuckDB 2.0's common_subplan gave the
# bowling rows the batting side's team.
SIDES_WINDOWED = """
WITH t AS (SELECT team, side, innings_no, phase, sum(runs) r, sum(balls) b
           FROM v_team WHERE innings_no <= 2 GROUP BY team, side, innings_no, phase)
SELECT innings_no, side, team, sum(r), sum(sum(b)) OVER (PARTITION BY side)
FROM t GROUP BY innings_no, side, team ORDER BY innings_no, side
"""


@pytest.mark.parametrize("optimizer", ["engine default", "common_subplan on"])
def test_team_sides_are_opposite(mini_engine, optimizer):
    q = mini_engine.con.execute
    if optimizer == "common_subplan on":
        q("RESET disabled_optimizers")
    # Reds batted first (22 runs); MINI_MATCH's chase is a single ball for 1.
    assert q(SIDES_BY_INNINGS).fetchall() == [
        (1, "batting", "Reds", "Blues", 22), (1, "bowling", "Blues", "Reds", 22),
        (2, "batting", "Blues", "Reds", 1), (2, "bowling", "Reds", "Blues", 1)]
    rows = q(SIDES_WINDOWED).fetchall()
    assert [r[:4] for r in rows] == [
        (1, "batting", "Reds", 22), (1, "bowling", "Blues", 22),
        (2, "batting", "Blues", 1), (2, "bowling", "Reds", 1)]
    assert len({r[4] for r in rows}) == 1  # both sides saw the same balls


def test_renamed_franchise_counts_as_one(build_engine, mini_match):
    e = build_engine({"1": mini_match, "2": rename_team(mini_match, "Reds", "Old Reds")})
    assert e.resolve_team("Old Reds") == e.resolve_team("RD") == "Reds"
    reds = records(teams.results(e, {"team": "Reds"}, by=["team"]))
    assert reds == [{**reds[0], "team": "Reds", "matches": 2}]
    names = {r["team_name"]: r["runs"] for r in records(aggregate(e, "team_batting", {"team": "Reds"}, by=["team_name"]))}
    assert names == {"Reds": 22, "Old Reds": 22}
    # Filtering on the old name selects the whole franchise.
    assert records(aggregate(e, "batting", {"opposition": "Old Reds"}, players=["b1"]))[0]["innings"] == 2


def test_profiles(mini_engine):
    profile = teams.team_profile(mini_engine, "Reds")
    assert [t.title.split(": ", 1)[1] for t in profile] == [
        "results by season", "results by opposition", "batting by phase",
        "bowling by phase (runs/wickets conceded)", "bowling resources by type", "batting depth (runs off the bat)"]
    assert teams.head_to_head(mini_engine, "Reds", "Blues")[0].rows

    overview = records(teams.venue_profile(mini_engine, "Test Ground")[0])[0]
    # A 5-over match: no full-length first innings for the par score; Blues chased and won.
    assert overview["matches"] == 1 and overview["avg_1st_innings"] is None and overview["chasing_win_pct"] == 100


def test_name_resolution_errors(mini_engine):
    with pytest.raises(NameNotFound, match="Candidates"):
        mini_engine.resolve_team("Greens")
    assert mini_engine.resolve_venue("test") == "Test Ground, Testville"


def test_alias_config_is_consistent():
    aliases = load_aliases()
    assert aliases["teams"]["Kings XI Punjab"] == "Punjab Kings"
    assert aliases["venues"]["Feroz Shah Kotla"] == "Arun Jaitley Stadium, Delhi"


@pytest.mark.integration
def test_ipl_teams_and_venues():
    if not get_tournament("ipl").db_path.exists():
        pytest.skip("IPL database not loaded")
    with open_engine(["ipl"]) as e:
        # Every raw team / venue name resolves to a canonical one, and renames collapse.
        franchises = {r[0] for r in e.con.execute("SELECT DISTINCT team FROM v_results").fetchall()}
        assert len(franchises) == 15 and "Delhi Daredevils" not in franchises
        venues = e.con.execute("SELECT count(DISTINCT venue), count(DISTINCT venue_raw) FROM v_matches").fetchone()
        assert venues[0] < venues[1]

        # Team totals reconcile with innings totals; wins with matches that have a winner.
        team_runs, inn_runs = e.con.execute(
            "SELECT (SELECT sum(runs) FROM v_team WHERE side = 'batting'),"
            " (SELECT sum(runs) FROM v_innings WHERE NOT is_super_over)").fetchone()
        assert team_runs == inn_runs
        # The bowling side is the other team (once mixed up by DuckDB's common_subplan).
        sides = e.con.execute("SELECT side, team, innings_no FROM v_team WHERE match_id = '1527676'"
                              " GROUP BY side, team, innings_no ORDER BY innings_no, side").fetchall()
        assert sides == [("batting", "Chennai Super Kings", 1), ("bowling", "Rajasthan Royals", 1),
                         ("batting", "Rajasthan Royals", 2), ("bowling", "Chennai Super Kings", 2)]
        mixed = e.con.execute("""
            WITH t AS (SELECT match_id, innings_no, side, any_value(team) AS team FROM v_team
                       WHERE season_year = 2026 GROUP BY ALL)
            SELECT count(*) OVER () FROM t a JOIN t b USING (match_id, innings_no)
            WHERE a.side = 'batting' AND b.side = 'bowling' AND a.team = b.team""").fetchall()
        assert mixed == []
        wins, decided = e.con.execute(
            "SELECT (SELECT count(*) FROM v_results WHERE outcome = 'won'),"
            " (SELECT count(*) FROM v_matches WHERE winner IS NOT NULL)").fetchone()
        assert wins == decided

        # RCB's 2025 title season: 11 won, 4 lost (one washout has no data).
        rcb = records(teams.results(e, {"team": "RCB", "season": 2025}))[0]
        assert (rcb["won"], rcb["lost"]) == (11, 4)
        assert records(teams.venue_profile(e, "Kotla")[0])[0]["matches"] > 60  # Kotla + Arun Jaitley
