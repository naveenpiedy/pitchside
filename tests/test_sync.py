import copy
import json
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta

import duckdb
import pytest

from pitchside import config
from pitchside.etl import schedule
from pitchside.etl import sync as sync_mod
from pitchside.etl.quality import run_checks


def write_zip(path, matches: dict[str, dict]):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("README.txt", "test")
        for mid, m in matches.items():
            z.writestr(f"{mid}.json", json.dumps(m))
    return path


@pytest.fixture
def ongoing(mini_engine, monkeypatch, tmp_path, test_tournament):
    """The MINI_MATCH competition as an 'ongoing' tournament, its engine closed so sync can write."""
    mini_engine.close()
    t = replace(test_tournament, status="ongoing")
    monkeypatch.setattr(config, "load_tournaments", lambda: {"test": t})
    monkeypatch.setattr(sync_mod, "REGISTRY_DB", tmp_path / "registry.duckdb")
    return t


def test_choose_source():
    now = datetime(2026, 5, 1)
    assert sync_mod.choose_source(False, None, now) == "full"                       # first load
    assert sync_mod.choose_source(True, now - timedelta(days=3), now) == "recent"
    assert sync_mod.choose_source(True, now - timedelta(days=40), now) == "full"   # recent zip may have gaps
    assert sync_mod.choose_source(True, now - timedelta(days=3), now, full=True) == "full"


def test_sync_from_recent_zip(ongoing, mini_match, tmp_path):
    other_event = copy.deepcopy(mini_match)
    other_event["info"]["event"] = {"name": "Some Other League"}
    revised = copy.deepcopy(mini_match)
    revised["meta"]["revision"] = 2
    recent = write_zip(tmp_path / "recent.zip", {"1": revised, "2": mini_match, "3": other_event})

    results = sync_mod.sync(recent_zip=recent, fetch_attributes=False, log=lambda m: None)
    r = results[0]
    assert (r.competition, r.source, r.added, r.updated) == ("test", "recent", 1, 1)
    assert not r.failed and {c.name for c in r.checks} >= {"innings_totals", "true_stat_baselines", "bowling_types"}

    again = sync_mod.sync(recent_zip=recent, fetch_attributes=False, log=lambda m: None)[0]
    assert (again.added, again.updated, again.data_version) == (0, 0, r.data_version)  # idempotent

    con = duckdb.connect(str(ongoing.db_path), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM matches").fetchone()[0] == 2
        assert con.execute("SELECT value FROM meta WHERE key = 'last_sync_source'").fetchone()[0] == "recent.zip"
    finally:
        con.close()


def test_sync_reports_errors_per_competition(ongoing, tmp_path):
    results = sync_mod.sync(recent_zip=tmp_path / "missing.zip", fetch_attributes=False, log=lambda m: None)
    assert results[0].failed and results[0].error
    with pytest.raises(KeyError):
        sync_mod.sync(["nope"], recent_zip=tmp_path / "missing.zip", log=lambda m: None)


def test_quality_checks_catch_inconsistencies(ongoing):
    con = duckdb.connect(str(ongoing.db_path))
    try:
        assert all(c.status != "fail" for c in run_checks(con))
        con.execute("UPDATE innings SET runs = runs + 1 WHERE match_id = '1' AND innings_no = 1")
        con.execute("UPDATE matches SET winner = 'Greens' WHERE match_id = '1'")
        failed = {c.name for c in run_checks(con) if c.status == "fail"}
    finally:
        con.close()
    assert {"innings_totals", "scorecard_totals", "winners"} <= failed


def test_schedule_command_and_validation():
    if schedule.shutil.which("uv"):
        exe, args = schedule.sync_command()
        assert exe == "cmd.exe" and "pitchside sync" in args and str(schedule.LOG_FILE) in args
    with pytest.raises(schedule.ScheduleError):
        schedule.install("9am")
