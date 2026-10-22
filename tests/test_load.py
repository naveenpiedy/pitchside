import pytest

from pitchside.config import get_tournament
from pitchside.etl.load import connect, iter_zip, load_matches
from pitchside.stats import scorecard as sc


def test_load_is_idempotent_and_applies_revisions(tmp_path, mini_match, test_tournament):
    con = connect(tmp_path / "t.duckdb")
    r = load_matches(con, test_tournament, [("1", mini_match, "1.json")], source="test")
    assert (r.added, r.updated, r.data_version) == (1, 0, 1)

    r = load_matches(con, test_tournament, [("1", mini_match, "1.json")], source="test")
    assert (r.added, r.updated, r.skipped, r.data_version) == (0, 0, 1, 1)

    mini_match["meta"]["revision"] = 2
    r = load_matches(con, test_tournament, [("1", mini_match, "1.json")], source="test")
    assert (r.added, r.updated, r.data_version) == (0, 1, 2)
    assert con.execute("SELECT count(*) FROM matches").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM deliveries WHERE match_id = '1'").fetchone()[0] == 11


def test_event_filter_and_format_filter(tmp_path, mini_match, test_tournament):
    con = connect(tmp_path / "t.duckdb")
    other = {**mini_match, "info": {**mini_match["info"], "event": {"name": "Other Cup"}}}
    odi = {**mini_match, "info": {**mini_match["info"], "match_type": "ODI"}}
    r = load_matches(con, test_tournament, [("2", other, "2.json"), ("3", odi, "3.json")],
                     source="test", filter_event=True)
    assert r.added == 0


def test_scorecard_tables(tmp_path, mini_match, test_tournament):
    con = connect(tmp_path / "t.duckdb")
    load_matches(con, test_tournament, [("1", mini_match, "1.json")], source="test")

    bat = {r[0]: r[1:] for r in con.execute(
        "SELECT player_id, runs, balls, is_out, dismissal FROM batting_innings WHERE innings_no = 1").fetchall()}
    assert bat["a1"] == (8, 3, True, "run out")       # wide not faced, leg-bye ball is
    assert bat["a2"] == (1, 2, False, "retired hurt")
    assert bat["a3"] == (10, 2, False, None)
    assert bat["a4"] == (0, 1, True, "caught")

    bowl = {r[0]: r[1:] for r in con.execute(
        "SELECT player_id, balls, runs, wickets FROM bowling_innings WHERE innings_no = 1").fetchall()}
    assert bowl["b1"] == (5, 21, 0) and bowl["b2"] == (2, 0, 1)

    # Super overs stay out of the scorecard tables.
    assert con.execute("SELECT count(*) FROM batting_innings WHERE innings_no = 3").fetchone()[0] == 0

    text = sc.render_scorecard(sc.match_summary(con, "1"))
    assert "run out (B2)" in text and "c B3 b B2" in text and "Reds 22/2 (1.1 ov)" in text


@pytest.mark.integration
def test_ipl_reconciles_with_published_records():
    t = get_tournament("ipl")
    if not t.zip_path.exists():
        pytest.skip("IPL zip not downloaded")
    con = connect(t.db_path, read_only=True) if t.db_path.exists() else None
    if con is None:
        pytest.skip("IPL database not loaded")

    leaders = dict(con.execute("""
        SELECT season_year, p.name FROM season_batting s JOIN players p USING (player_id)
        QUALIFY row_number() OVER (PARTITION BY season_year ORDER BY runs DESC) = 1""").fetchall())
    assert leaders[2016] == "V Kohli" and leaders[2023] == "Shubman Gill"
    runs = dict(con.execute("""SELECT season_year, runs FROM season_batting s JOIN players p USING (player_id)
                               WHERE p.name = 'V Kohli'""").fetchall())
    assert runs[2016] == 973 and runs[2024] == 741

    wkts = con.execute("""SELECT wickets FROM season_bowling s JOIN players p USING (player_id)
                          WHERE p.name = 'DJ Bravo' AND season_year = 2013""").fetchone()[0]
    assert wkts == 32

    final = sc.match_summary(con, "1426312")
    assert [i["total"] for i in final["innings"]] == ["113/10 (18.3 ov)", "114/2 (10.3 ov)"]
    assert len(list(iter_zip(t.zip_path))) == con.execute("SELECT count(*) FROM matches").fetchone()[0]
