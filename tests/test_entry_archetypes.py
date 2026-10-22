import pytest

from pitchside.config import get_tournament
from pitchside.engine import open_engine
from pitchside.stats import archetypes as ar
from pitchside.stats import players as pl
from pitchside.stats.splits import aggregate


def test_entry_point_columns(mini_engine):
    # MINI_MATCH innings 1: A1/A2 open; A3 arrives after 3 legal balls; A4 after 6.
    rows = dict(mini_engine.con.execute(
        "SELECT player_id, any_value(entry_balls) FROM v_bat WHERE innings_no = 1 GROUP BY 1").fetchall())
    assert rows == {"a1": 0, "a2": 0, "a3": 3, "a4": 6}
    phases = {r["entry_phase"]: r["balls"] for r in aggregate(
        mini_engine, "batting", {"innings": 1}, by=["entry_phase"], metrics=["balls"]).records()}
    assert phases == {"opener": 5, "powerplay": 3}
    # The non-striker run-out row keeps the batter's entry point.
    out_row = mini_engine.con.execute("SELECT entry_balls FROM v_bat WHERE player_id = 'a1' AND faced = 0").fetchone()
    assert out_row == (0,)
    # Filters
    openers = {r["player"] for r in aggregate(mini_engine, "batting", {"entry_phase": "opener"}, by=["player"],
                                              metrics=["balls"]).records()}
    assert openers == {"A1", "A2", "B1"}  # B2 opened innings 2 but never faced a ball
    assert {r["player"] for r in aggregate(mini_engine, "batting", {"entry_over": 2}, by=["player"],
                                           metrics=["balls"]).records()} == {"A4"}


def test_batting_archetype_rules():
    base = {"avg_entry_over": 1.0, "opener_share": 1.0, "death_share": 0.05, "balls_per_innings": 25.0}
    assert ar.batting_archetype({**base, "true_sr_shrunk": 20}) == "Powerplay enforcer"
    assert ar.batting_archetype({**base, "true_sr_shrunk": -10}) == "Anchor opener"
    assert ar.batting_archetype({**base, "true_sr_shrunk": 0}) == "Opener"
    finisher = {"avg_entry_over": 15.0, "opener_share": 0, "death_share": 0.6, "balls_per_innings": 10.0}
    assert ar.batting_archetype({**finisher, "true_sr_shrunk": 12}) == "Finisher"
    assert ar.batting_archetype({**finisher, "true_sr_shrunk": -10}) == "Lower-order batter"
    middle = {"avg_entry_over": 9.0, "opener_share": 0, "death_share": 0.25, "balls_per_innings": 12.0}
    # Slow but short innings is not an anchor.
    assert ar.batting_archetype({**middle, "true_sr_shrunk": -10}) == "Middle-order accumulator"
    assert ar.batting_archetype({**middle, "true_sr_shrunk": 10}) == "Middle-overs aggressor"


def test_bowling_archetype_rules():
    f = {"bowl_type": "pace", "powerplay_share": 0.1, "death_share": 0.6, "true_economy_shrunk": 1.0}
    assert ar.bowling_archetype(f) == "Death specialist (economical)"
    assert ar.bowling_archetype({**f, "powerplay_share": 0.6, "death_share": 0.1,
                                 "true_economy_shrunk": -0.8}) == "New-ball seamer (expensive)"
    assert ar.bowling_archetype({**f, "powerplay_share": 0.35, "death_share": 0.32}) == "All-phase seamer (economical)"
    assert ar.bowling_archetype({"bowl_type": "spin", "powerplay_share": 0.4, "death_share": 0.0,
                                 "true_economy_shrunk": 0}) == "Powerplay spinner"
    assert ar.bowling_archetype({"bowl_type": "unknown", "powerplay_share": 0.4, "death_share": 0.0,
                                 "true_economy_shrunk": None}) == "Bowler (type unknown)"


def test_archetypes_table(mini_engine):
    t = ar.archetypes(mini_engine, "batting", min_balls=1)
    assert t.columns[:3] == ["player", "team", "archetype"] and t.rows
    assert ar.archetype_summary(mini_engine, "bowling", min_balls=1).rows


@pytest.mark.integration
def test_ipl_entry_and_archetypes():
    if not get_tournament("ipl").db_path.exists():
        pytest.skip("IPL database not loaded")
    with open_engine(["ipl"]) as e:
        assert e.con.execute("SELECT count(*) FROM v_bat WHERE entry_balls IS NULL").fetchone()[0] == 0
        labels = {r["player"]: r["archetype"] for r in ar.archetypes(e, "batting", {"season": "2024+"}).records()}
        assert labels["Abhishek Sharma"] == "Powerplay enforcer"
        assert labels["RD Gaikwad"] == "Anchor opener"
        assert labels["AD Russell"] == "Finisher"
        bowl = {r["player"]: r["archetype"] for r in ar.archetypes(e, "bowling", {"season": "2024+"}).records()}
        assert bowl["M Pathirana"].startswith("Death specialist")
        assert bowl["SP Narine"].startswith("Middle-overs spinner")
        assert pl.player_archetype(e, "TA Boult", "bowling", {"season": "2024+"}) == "New-ball seamer"
