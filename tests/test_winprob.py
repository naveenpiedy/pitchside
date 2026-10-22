import pytest

from pitchside import tools
from pitchside.config import get_tournament
from pitchside.engine import open_engine
from pitchside.model.winprob import _solve
from pitchside.stats import impact
from pitchside.stats.splits import aggregate


def test_solver():
    x = _solve([[2.0, 1.0], [1.0, 3.0]], [3.0, 5.0])
    assert x == pytest.approx([0.8, 1.4])
    with pytest.raises(ValueError):
        _solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0])


def test_winprob_on_tiny_match(mini_engine):
    q = mini_engine.con.execute
    rows = q("SELECT innings_no, seq, wp_before, wp_after, wpa FROM test.winprob ORDER BY 1, 2").fetchall()
    assert rows and all(0 <= r[2] <= 1 and 0 <= r[3] <= 1 for r in rows)
    # Telescoping: each innings' WPA sums to its end minus its start.
    for inn in (1, 2):
        part = [r for r in rows if r[0] == inn]
        assert sum(r[4] for r in part) == pytest.approx(part[-1][3] - part[0][2], abs=1e-12)
    # MINI_MATCH is a tie: the chase ends at 0.5; the first innings ends at 1 - the chase's start.
    chase = [r for r in rows if r[0] == 2]
    assert chase[-1][3] == 0.5
    assert [r for r in rows if r[0] == 1][-1][3] == pytest.approx(1 - chase[0][2])
    # Batters get WPA only on balls faced; bowlers get the opposite sign of every ball.
    wide_wpa = q("SELECT wpa FROM v_balls WHERE innings_no = 1 AND seq = 2").fetchone()[0]
    assert q("SELECT wpa FROM v_bat WHERE innings_no = 1 AND seq = 2 AND player_id = 'a1'").fetchone()[0] == 0
    assert q("SELECT wpa FROM v_bowl WHERE innings_no = 1 AND seq = 2").fetchone()[0] == pytest.approx(-wide_wpa)


def test_mvp_and_match_impact(mini_engine):
    t = impact.mvp(mini_engine, top=5)
    assert t.columns[:6] == ["player", "team", "matches", "bat_wpa", "bowl_wpa", "total_wpa"] and t.rows
    tables = impact.match_impact(mini_engine, "1")
    assert [x.title.split(" | ")[-1] for x in tables] == [
        "win probability by over", "Biggest contributors (WPA in wins)", "Biggest swings"]
    with pytest.raises(LookupError):
        impact.match_impact(mini_engine, "nope")
    assert "wpa" in aggregate(mini_engine, "batting", metrics=["runs", "wpa"]).columns


@pytest.mark.integration
def test_ipl_winprob_calibrated_and_consistent():
    t = get_tournament("ipl")
    if not t.db_path.exists():
        pytest.skip("IPL database not loaded")
    with open_engine(["ipl"]) as e:
        q = e.con.execute
        for inn, pred, actual, n in q("SELECT innings, predicted, actual, balls FROM ipl.winprob_calibration").fetchall():
            if n >= 5000:
                assert abs(pred - actual) < 0.05, (inn, pred, actual)
        # Per match, team A's batting WPA (innings 1) + bowling WPA (innings 2) = A's result - A's start.
        worst = q("""
            WITH w AS (SELECT w.*, i.batting_team FROM ipl.winprob w JOIN ipl.innings i USING (match_id, innings_no)),
            a AS (SELECT match_id,
                         sum(wpa) FILTER (innings_no = 1) - sum(wpa) FILTER (innings_no = 2) AS total,
                         arg_min(wp_before, innings_no * 1000 + seq) AS start,
                         1 - arg_max(wp_after, innings_no * 1000 + seq) AS result
                  FROM w GROUP BY 1 HAVING count(DISTINCT innings_no) = 2)
            SELECT max(abs(total - (result - start))) FROM a""").fetchone()[0]
        assert worst < 1e-9
        # The model knows a chase gets harder as the required rate rises (same balls and wickets).
        coef = dict(q("SELECT feature, coef FROM ipl.winprob_model WHERE innings = 2").fetchall())
        assert coef["difficulty"] < 0
        assert "AB de Villiers" in tools.mvp(top=5)
