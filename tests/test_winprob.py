import pytest

from pitchside import tools
from pitchside.config import get_tournament
from pitchside.engine import open_engine
from pitchside.model.winprob import CELLS, _solve, balls_band, outcome_distributions, pressure_band, solve_chase
from pitchside.stats import impact
from pitchside.stats.splits import aggregate


def test_solver():
    x = _solve([[2.0, 1.0], [1.0, 3.0]], [3.0, 5.0])
    assert x == pytest.approx([0.8, 1.4])
    with pytest.raises(ValueError):
        _solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0])


def test_chase_backward_induction():
    # Every ball a dot or a 2: needing 2 off 1 is a coin flip, 1 off 1 wins or ties.
    v = solve_chase(lambda w, r, b: [(1, 0, 0, 0.5), (1, 2, 0, 0.5)], 6)
    assert v(1, 5, 2) == pytest.approx(0.5)
    assert v(1, 5, 1) == pytest.approx(0.5 + 0.5 * 0.5)
    # A wide (no ball used) or a dot: the wide turns 2 off 1 into 1 off 1.
    v = solve_chase(lambda w, r, b: [(0, 1, 0, 0.5), (1, 0, 0, 0.5)], 6)
    assert v(1, 5, 2) == pytest.approx(0.5 * (0.5 + 0.5 * 0.5))
    # Last wicket falls with scores level: a tie. Target already reached: won.
    v = solve_chase(lambda w, r, b: [(1, 0, 1, 1.0)], 6)
    assert v(3, 1, 1) == 0.5 and v(3, 1, 2) == 0.0 and v(3, 4, 0) == 1.0
    # More balls and more wickets never hurt.
    v = solve_chase(lambda w, r, b: [(1, 0, 0, 0.4), (1, 1, 0, 0.3), (1, 4, 0, 0.2), (1, 0, 1, 0.1)], 12)
    assert v(12, 5, 20) > v(6, 5, 20) and v(6, 5, 20) > v(6, 1, 20)


def test_outcome_bands_and_shrinkage():
    assert [pressure_band(r, 6) for r in (6, 7, 9, 10, 12, 13, 18, 19)] == [0, 1, 1, 2, 2, 3, 3, 4]
    assert [balls_band(b) for b in (1, 2, 6, 7, 12, 13, 24)] == [0, 1, 1, 2, 2, 3, 3]
    rows = [(0, "G", 3, 0, 3, True, 0, False, 40), (0, "G", 3, 0, 3, True, 4, False, 10),
            (0, "H", 1, 2, 0, False, 1, False, 3)]
    by_era, by_venue = outcome_distributions(rows)
    for dist in [*by_era.values(), *by_venue.values()]:
        assert sum(dist.values()) == pytest.approx(1.0)
    assert len(by_era) == len(CELLS)
    # Each level sits between its parent and its own data (G: 80% dots), never past the data.
    era, ground = by_era[(0, 3, 0, 3)], by_venue[(0, "G", 3, 0, 3)]
    assert era[(True, 0, False)] < ground[(True, 0, False)] < 0.8


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
        # The first innings' last ball doesn't absorb a model gap at the handover (it averaged -0.06).
        last, n = q("""
            WITH a AS (SELECT match_id, max(allocated_balls) FILTER (innings_no = 1) AS a1,
                              max(allocated_balls) FILTER (innings_no = 2) AS a2 FROM ipl.innings GROUP BY 1),
            l AS (SELECT w.match_id, arg_max(w.wpa, w.seq) AS wpa, arg_max(d.wickets_before + d.is_wicket::INT, w.seq) AS wk
                  FROM ipl.winprob w JOIN ipl.deliveries d USING (match_id, innings_no, seq)
                  WHERE w.innings_no = 1 GROUP BY 1)
            SELECT avg(wpa), count(*) FROM l JOIN a USING (match_id) WHERE a1 = a2 AND wk < 10""").fetchone()
        assert n > 500 and abs(last) < 0.01, last
        # The model knows a chase gets harder as the required rate rises (same balls and wickets).
        coef = dict(q("SELECT feature, coef FROM ipl.winprob_model WHERE innings = 2").fetchall())
        assert coef["difficulty"] < 0
        # The last overs, where the logistic curve alone was 30 points too confident about 2 off 1.
        for balls_left, band, pred, actual, n in q("SELECT balls_left, runs_per_ball, predicted, actual, balls "
                                                   "FROM ipl.winprob_endgame").fetchall():
            if n >= 200:
                assert abs(pred - actual) < 0.1, (balls_left, band, pred, actual)
        pred, actual = q("""
            WITH s AS (SELECT d.match_id, min(d.seq) AS seq FROM ipl.deliveries d
                       WHERE d.innings_no = 2 AND d.balls_remaining = 1 AND d.runs_required = 2 GROUP BY 1)
            SELECT avg(w.wp_before), avg(CASE WHEN m.result = 'tie' THEN 0.5 WHEN m.winner = i.batting_team THEN 1.0
                                              ELSE 0.0 END)
            FROM s JOIN ipl.winprob w USING (match_id, seq) JOIN ipl.matches m USING (match_id)
            JOIN ipl.innings i ON i.match_id = s.match_id AND i.innings_no = 2
            WHERE w.innings_no = 2""").fetchone()
        assert abs(pred - actual) < 0.15, (pred, actual)
        assert "AB de Villiers" in tools.mvp(top=5)


def test_leverage_on_tiny_match(mini_engine):
    q = mini_engine.con.execute
    lo, hi, mean_li = q("SELECT min(leverage), max(leverage), avg(leverage_index) FROM test.winprob").fetchone()
    assert 0 <= lo <= hi <= 1 and mean_li == pytest.approx(1)
    # Leverage can't make WPA move: the before/after values are the same model as without it.
    assert q("SELECT count(*) FROM test.winprob WHERE leverage IS NULL").fetchone()[0] == 0
    row = aggregate(mini_engine, "batting", players=["a3"], by=["leverage"], metrics=["balls", "avg_leverage"]).records()
    assert row and all(r["leverage"] in ("1 low", "2 medium", "3 high", "4 very high") for r in row)


@pytest.mark.integration
def test_ipl_leverage():
    if not get_tournament("ipl").db_path.exists():
        pytest.skip("IPL database not loaded")
    with open_engine(["ipl"]) as e:
        q = e.con.execute
        # The biggest moments are at the end of chases; the first innings' death matters least.
        li = dict(((i, p), v) for i, p, v in q(
            "SELECT innings_no, phase, avg(leverage_index) FROM v_balls GROUP BY 1, 2").fetchall())
        assert li[(1, "powerplay")] > li[(1, "middle")] > li[(1, "death")]
        assert max(li[(2, p)] for p in ("powerplay", "middle", "death")) > li[(1, "powerplay")]
        # Swings follow leverage: the average |WPA| of a ball rises with its leverage bucket.
        swing = [r[0] for r in q("""SELECT avg(abs(wpa)) FROM v_balls WHERE leverage_index IS NOT NULL
                                    GROUP BY least(floor(leverage_index), 4) ORDER BY least(floor(leverage_index), 4)""").fetchall()]
        assert swing == sorted(swing)
        # Needing 4 off the last ball: a six wins, a wicket loses, so leverage is about 1.
        lev = q("""SELECT avg(w.leverage) FROM ipl.winprob w JOIN ipl.deliveries d USING (match_id, innings_no, seq)
                   WHERE d.innings_no = 2 AND d.balls_remaining = 1 AND d.runs_required BETWEEN 2 AND 6""").fetchone()[0]
        assert lev > 0.9
