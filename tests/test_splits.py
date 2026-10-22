import pytest

from pitchside.config import get_tournament
from pitchside.engine import PlayerNotFound, open_engine
from pitchside.stats import players as pl
from pitchside.stats.filters import FilterError
from pitchside.stats.splits import aggregate


def one(table):
    assert len(table.rows) == 1, table.render()
    return table.records()[0]


def test_batting_counts_non_striker_run_outs(mini_engine):
    a1 = one(aggregate(mini_engine, "batting", players=["a1"]))
    assert (a1["runs"], a1["balls"], a1["outs"]) == (8, 3, 1)  # run out at the non-striker's end
    a2 = one(aggregate(mini_engine, "batting", players=["a2"]))
    assert (a2["runs"], a2["balls"], a2["outs"]) == (1, 2, 0)  # retired hurt isn't out


def test_bowling_and_super_overs_excluded(mini_engine):
    rows = {r["player"]: r for r in aggregate(mini_engine, "bowling", by=["player"]).records()}
    assert (rows["B1"]["balls"], rows["B1"]["runs"], rows["B1"]["wickets"]) == (5, 21, 0)
    assert (rows["B2"]["balls"], rows["B2"]["wickets"]) == (2, 1)
    assert rows["A1"]["balls"] == 1 and "A3" not in rows  # A3 only bowled the super over


def test_attribute_dimensions_and_filters(mini_engine):
    by_type = {r["bowl_type"]: r for r in aggregate(mini_engine, "batting", by=["bowl_type"], players=["a3"]).records()}
    assert by_type["pace"]["runs"] == 10
    vs_spin = aggregate(mini_engine, "batting", {"bowl_type": "spin"}, players=["a3", "a4", "a2"], by=["player"])
    assert {r["player"]: r["balls"] for r in vs_spin.records()} == {"A2": 1, "A4": 1}
    hands = {r["batting_hand"]: r["balls"] for r in aggregate(mini_engine, "bowling", by=["batting_hand"]).records()}
    assert hands == {"left": 2, "right": 1, "unknown": 5}  # a1 left, b1 right, the rest unclassified
    empty = aggregate(mini_engine, "batting", {"phase": "death"})
    assert empty.rows == [] and "no data" in empty.notes[0]


def test_leaderboard_compare_and_profile(mini_engine):
    top = pl.leaderboard(mini_engine, "runs", top=2)
    assert [r["player"] for r in top.records()] == ["A3", "A1"]
    assert pl.leaderboard(mini_engine, "wickets").records()[0]["player"] == "B2"  # role inferred

    cmp = pl.compare(mini_engine, ["A3", "A1"])
    assert [r["player"] for r in cmp.records()] == ["A3", "A1"] and "team" not in cmp.columns

    sections = pl.profile(mini_engine, "A1")
    assert sections and all(t.title.startswith("A1:") for t in sections)

    with pytest.raises(FilterError):
        pl.compare(mini_engine, ["A1"])
    with pytest.raises(FilterError, match="Unknown batting metric"):
        aggregate(mini_engine, "batting", metrics=["wickets"])


def test_player_resolution(mini_engine):
    assert mini_engine.resolve_player("a1") == "a1"
    assert mini_engine.resolve_player("A3") == "a3"
    with pytest.raises(PlayerNotFound):
        mini_engine.resolve_player("Nobody At All")


def test_results_are_cached(mini_engine, monkeypatch):
    first = pl.leaderboard(mini_engine, "runs", top=3)

    def boom(*a, **k):
        raise AssertionError("should have come from the cache")

    monkeypatch.setattr(pl, "aggregate", boom)
    assert pl.leaderboard(mini_engine, "runs", top=3).rows == first.rows
    # A new data version misses the cache.
    mini_engine.data_versions["test"] += 1
    with pytest.raises(AssertionError):
        pl.leaderboard(mini_engine, "runs", top=3)


@pytest.mark.integration
def test_ipl_splits_reconcile_with_season_tables():
    if not get_tournament("ipl").db_path.exists():
        pytest.skip("IPL database not loaded")
    with open_engine(["ipl"]) as e:
        bat = aggregate(e, "batting", by=["season", "player"], metrics=["runs", "balls", "outs"])
        got = {(r["season"], r["player"]): (r["runs"], r["balls"], r["outs"]) for r in bat.records()}
        want = {(s, e.name(p)): (r, b, o) for s, p, r, b, o in e.con.execute(
            "SELECT season_year, player_id, runs, balls, outs FROM ipl.season_batting"
            " WHERE balls > 0 OR outs > 0").fetchall()}  # skip batters who came in but never faced
        assert got == want

        bowl = aggregate(e, "bowling", by=["season", "player"], metrics=["balls", "runs", "wickets"])
        got = {(r["season"], r["player"]): (r["balls"], r["runs"], r["wickets"]) for r in bowl.records()}
        want = {(s, e.name(p)): (b, r, w) for s, p, b, r, w in e.con.execute(
            "SELECT season_year, player_id, balls, runs, wickets FROM ipl.season_bowling").fetchall()}
        assert got == want

        assert e.name(e.resolve_player("Rashid")) == "Rashid Khan"
        assert e.name(e.resolve_player("Virat Kohli")) == "V Kohli"
