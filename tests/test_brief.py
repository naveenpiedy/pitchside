import pytest

from pitchside.config import get_tournament
from pitchside.engine import NameNotFound, open_engine
from pitchside.stats.brief import _squad, prematch_brief


def test_brief_sections(mini_engine):
    tables = prematch_brief(mini_engine, "RD", "Blues", venue="Test Ground")
    titles = [t.title for t in tables]
    assert titles[0] == "Reds v Blues: all-time (Reds's record)"
    for expected in ("Reds: last 5 matches", "Blues: last 5 matches", "Test Ground, Testville: recent",
                     "Both teams at Test Ground, Testville (all-time)", "Reds: phase profile (recent)",
                     "Reds: key batters (recent, by net runs)", "Blues: key bowlers (recent, by runs saved)",
                     "Reds batters v Blues bowlers (head to head)"):
        assert expected in titles, expected
    # Squad tables are per team, so no 'latest team' column.
    key = next(t for t in tables if t.title.startswith("Reds: key batters"))
    assert "team" not in key.columns
    # Without a venue, the venue sections are left out.
    assert not any("Test Ground" in t.title for t in prematch_brief(mini_engine, "Reds", "Blues"))


def test_squads_and_errors(mini_engine):
    assert set(_squad(mini_engine, "Reds")) == {"a1", "a2", "a3", "a4"}       # batted or bowled for Reds
    assert set(_squad(mini_engine, "Blues")) == {"b1", "b2"}
    with pytest.raises(NameNotFound):
        prematch_brief(mini_engine, "Reds", "Greens")


@pytest.mark.integration
def test_ipl_brief():
    if not get_tournament("ipl").db_path.exists():
        pytest.skip("IPL database not loaded")
    with open_engine(["ipl"]) as e:
        tables = {t.title: t for t in prematch_brief(e, "CSK", "MI", venue="Wankhede")}
        mi_bowlers = tables["Mumbai Indians: key bowlers (recent, by runs saved)"].records()
        assert mi_bowlers[0]["player"] == "JJ Bumrah"
        h2h = tables["Chennai Super Kings v Mumbai Indians: all-time (Chennai Super Kings's record)"].records()[0]
        assert h2h["matches"] >= 40
