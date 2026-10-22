import asyncio

import pytest
from mcp import Client

from pitchside import config, tools
from pitchside.mcp_server import TOOLS, build_server


@pytest.fixture
def mini_tools(mini_engine, monkeypatch, tmp_path, test_tournament):
    """Tool layer over the MINI_MATCH engine setup, with a temp export dir."""
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(config, "load_tournaments", lambda: {"test": test_tournament})
    mini_engine.close()  # tools open their own engine per call
    return tools


def call(name: str, args: dict):
    async def go():
        async with Client(build_server()) as c:
            return await c.call_tool(name, args)
    return asyncio.run(go())


def is_error(result) -> bool:
    return bool(getattr(result, "is_error", getattr(result, "isError", False)))


def test_server_registers_every_tool_read_only_except_export():
    async def go():
        async with Client(build_server()) as c:
            return (await c.list_tools()).tools, c
    listed, _ = asyncio.run(go())
    assert {t.name for t in listed} == set(TOOLS)
    for t in listed:
        ann = t.annotations
        assert ann.read_only_hint is (t.name != "export"), t.name
        assert ann.destructive_hint is False


def test_query_is_read_only_and_capped(mini_tools):
    out = mini_tools.query("SELECT player_id, runs FROM v_bat", limit=3)
    lines = out.splitlines()
    assert lines[0] == "player_id|runs" and len([x for x in lines if not x.startswith("note")]) == 4
    assert "more rows exist" in out
    for bad in ("DELETE FROM v_bat", "CREATE TABLE x AS SELECT 1", "SELECT 1; SELECT 2",
                "COPY (SELECT 1) TO 'x.csv'", "ATTACH 'x.duckdb'"):
        with pytest.raises(tools.ToolError):
            mini_tools.query(bad)
    with pytest.raises(tools.ToolError, match="disabled|Permission"):
        mini_tools.query("SELECT * FROM read_csv('pyproject.toml')")


def test_export_writes_file_and_returns_only_shape(mini_tools, tmp_path):
    out = mini_tools.export("SELECT * FROM v_bat", "csv", "all bat")
    path = next(line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("path: "))
    assert path.startswith(str(tmp_path / "exports")) and "all_bat_" in path
    assert "rows: 11" in out  # 10 deliveries outside the super over + 1 non-striker run-out row
    with pytest.raises(tools.ToolError):
        mini_tools.export("SELECT 1", "xlsx")


def test_discovery_tools(mini_tools):
    assert "test|" in mini_tools.overview()
    assert "v_bat|" in mini_tools.describe_schema()
    assert "exp_runs|" in mini_tools.describe_schema("v_bat")
    values = mini_tools.describe_schema("v_bat", "bowl_type")
    assert "pace|" in values and "spin|" in values
    with pytest.raises(tools.ToolError):
        mini_tools.describe_schema("v_bat; DROP TABLE x")
    with pytest.raises(tools.ToolError, match="Unknown column"):
        mini_tools.describe_schema("v_bat", "nope")
    assert "a1|A1|exact" in mini_tools.find_player("A1")
    assert "Reds|" in mini_tools.find_team("RD")
    assert "Test Ground, Testville|" in mini_tools.find_venue("test")
    assert "1|2025-04-01|Reds|Blues" in mini_tools.find_matches(team="Reds")
    assert "true_sr" in mini_tools.grammar()


def test_analysis_tools(mini_tools):
    assert "## A3 batting by season" in mini_tools.true_stats("A3")
    assert "true_wickets" in mini_tools.leaderboard("true_wickets")
    assert "Reds: results by season" in mini_tools.team_profile("Reds")
    assert "Reds v Blues" in mini_tools.head_to_head("Reds", "Blues")
    assert "Test Ground, Testville: overview" in mini_tools.venue_profile("Test Ground")
    card = mini_tools.scorecard("1", partnerships=True, overs=True)
    assert "Reds v Blues" in card and "## Partnerships, innings 1" in card and "## Overs, innings 1" in card
    with pytest.raises(tools.ToolError, match="No match"):
        mini_tools.scorecard("999")


def test_errors_reach_the_client_as_tool_errors(mini_tools):
    r = call("player_profile", {"player": "Nobody At All"})
    assert is_error(r) and "No unique player" in r.content[0].text
    r = call("leaderboard", {"metric": "runs", "filters": {"sesaon": 2025}})
    assert is_error(r) and "Unknown filter" in r.content[0].text
    r = call("leaderboard", {"metric": "runs", "top": 2})
    assert not is_error(r) and r.content[0].text.startswith("## Top 2")
