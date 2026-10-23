import copy

import pytest

from pitchside.config import Tournament


def ball(batter, bowler, non_striker, runs=0, extras=None, wickets=None, non_boundary=False):
    extras = extras or {}
    extra_runs = sum(extras.values())
    d = {
        "batter": batter, "bowler": bowler, "non_striker": non_striker,
        "runs": {"batter": runs, "extras": extra_runs, "total": runs + extra_runs},
    }
    if non_boundary:
        d["runs"]["non_boundary"] = True
    if extras:
        d["extras"] = extras
    if wickets:
        d["wickets"] = wickets
    return d


# A tiny made-up T20 that exercises the awkward cases: wide, no-ball, leg-byes,
# a non-striker run out, retired hurt, a DLS-reduced chase and a super over.
MINI_MATCH = {
    "meta": {"data_version": "1.2.0", "created": "2025-04-01", "revision": 1},
    "info": {
        "balls_per_over": 6, "city": "Testville", "dates": ["2025-04-01"],
        "event": {"name": "Test League", "match_number": 1}, "gender": "male",
        "match_type": "T20", "overs": 20,
        "outcome": {"result": "tie", "eliminator": "Blues"},
        "player_of_match": ["A1"],
        "players": {"Reds": ["A1", "A2", "A3", "A4"], "Blues": ["B1", "B2", "B3"]},
        "registry": {"people": {
            "A1": "a1", "A2": "a2", "A3": "a3", "A4": "a4", "B1": "b1", "B2": "b2", "B3": "b3",
        }},
        "season": "2025", "team_type": "club", "teams": ["Reds", "Blues"],
        "toss": {"decision": "bat", "winner": "Reds"}, "venue": "Test Ground",
    },
    "innings": [
        {"team": "Reds", "overs": [
            {"over": 0, "deliveries": [
                ball("A1", "B1", "A2", runs=4),
                ball("A1", "B1", "A2", extras={"wides": 1}),
                ball("A1", "B1", "A2", runs=4, extras={"noballs": 1}),
                ball("A1", "B1", "A2", extras={"legbyes": 1}),
                ball("A2", "B1", "A1", runs=1,
                     wickets=[{"player_out": "A1", "kind": "run out", "fielders": [{"name": "B2"}]}]),
                ball("A3", "B1", "A2", runs=6),
                ball("A3", "B1", "A2", runs=4, non_boundary=True),
            ]},
            {"over": 1, "deliveries": [
                ball("A2", "B2", "A3", wickets=[{"player_out": "A2", "kind": "retired hurt"}]),
                ball("A4", "B2", "A3", wickets=[{"player_out": "A4", "kind": "caught", "fielders": [{"name": "B3"}]}]),
            ]},
        ]},
        {"team": "Blues", "target": {"overs": 5, "runs": 23}, "overs": [
            {"over": 0, "deliveries": [ball("B1", "A1", "B2", runs=1)]},
        ]},
        {"team": "Blues", "super_over": True, "overs": [
            {"over": 0, "deliveries": [ball("B1", "A3", "B2", runs=6)]},
        ]},
    ],
}


@pytest.fixture
def mini_match():
    return copy.deepcopy(MINI_MATCH)


@pytest.fixture
def test_tournament(tmp_path):
    return Tournament(key="test", name="Test League", cricsheet="test", event="Test League",
                      format="T20", status="ongoing")


ATTRIBUTES = """player_id,name,batting_hand,bowling_arm,bowling_style,bowl_type,bowl_subtype,source,wiki_title
a1,A1,left,right,Right-arm offbreak,spin,OB,manual,
b1,B1,right,right,Right-arm fast,pace,RF,manual,
b2,B2,right,right,Right-arm offbreak,spin,OB,manual,
"""


TEST_ALIASES = {
    "teams": {"Reds": "Reds", "Old Reds": "Reds", "RD": "Reds", "Blues": "Blues", "BL": "Blues"},
    "venues": {"Test Ground, Testville": "Test Ground, Testville", "Test Ground": "Test Ground, Testville"},
}


def rename_team(match: dict, old: str, new: str) -> dict:
    """A copy of a match with one team renamed everywhere it appears."""
    m = copy.deepcopy(match)
    info = m["info"]
    info["teams"] = [new if t == old else t for t in info["teams"]]
    info["players"] = {new if t == old else t: p for t, p in info["players"].items()}
    for k in ("winner",):
        if info["toss"].get(k) == old:
            info["toss"][k] = new
    for k in ("winner", "eliminator"):
        if info["outcome"].get(k) == old:
            info["outcome"][k] = new
    for inn in m["innings"]:
        if inn["team"] == old:
            inn["team"] = new
    return m


@pytest.fixture
def build_engine(tmp_path, monkeypatch, test_tournament):
    """Factory: an Engine over a temp competition DB holding the given matches,
    with a temp registry (ATTRIBUTES), test aliases and a temp cache."""
    import duckdb

    from pitchside import config
    from pitchside import engine as eng
    from pitchside.etl.load import connect, load_matches
    from pitchside.etl.register import load_attributes
    from pitchside.model import derive
    from pitchside.stats import cache

    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    monkeypatch.setattr(cache, "CACHE_DB", tmp_path / "cache.duckdb")
    csv_path = tmp_path / "attributes.csv"
    csv_path.write_text(ATTRIBUTES)
    registry = tmp_path / "registry.duckdb"
    reg = duckdb.connect(str(registry))
    load_attributes(reg, csv_path)
    reg.close()
    monkeypatch.setattr(eng, "REGISTRY_DB", registry)
    monkeypatch.setattr(eng, "load_tournaments", lambda: {"test": test_tournament})
    monkeypatch.setattr(eng, "load_aliases", lambda: TEST_ALIASES)
    monkeypatch.setattr(derive, "load_aliases", lambda: TEST_ALIASES)
    opened = []

    def build(matches: dict[str, dict]):
        con = connect(test_tournament.db_path)
        load_matches(con, test_tournament, [(mid, m, f"{mid}.json") for mid, m in matches.items()], source="test")
        con.close()
        opened.append(eng.open_engine())
        return opened[-1]

    yield build
    for e in opened:
        e.close()


@pytest.fixture
def mini_engine(build_engine, mini_match):
    """An Engine over MINI_MATCH alone."""
    return build_engine({"1": mini_match})
