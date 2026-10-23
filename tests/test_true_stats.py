import pytest

from pitchside.config import get_tournament
from pitchside.engine import open_engine
from pitchside.etl.load import connect, rebuild
from pitchside.model.baselines import TARGETS, era_labels
from pitchside.stats import players as pl
from pitchside.stats.splits import aggregate

BAT_SUMS = "sum(runs) - sum(exp_runs), sum(out) - sum(exp_out), sum(dot) - sum(exp_dot), sum(fours + sixes) - sum(exp_bnd)"
BOWL_SUMS = "sum(runs) - sum(exp_runs), sum(wickets) - sum(exp_wkts), sum(dot) - sum(exp_dot), sum(fours + sixes) - sum(exp_bnd)"


def test_era_labels():
    assert era_labels([]) == ["all"]
    assert era_labels([2023]) == ["before 2023", "2023+"]
    assert era_labels([2023, 2020]) == ["before 2020", "2020-2022", "2023+"]


def test_zero_sum_on_tiny_data(mini_engine):
    # Rescaling makes expected totals equal actual totals even on one match.
    for view, sums in (("v_bat", BAT_SUMS), ("v_bowl", BOWL_SUMS)):
        assert all(abs(v) < 1e-9 for v in mini_engine.con.execute(f"SELECT {sums} FROM {view}").fetchone())


def test_expectations_sit_on_the_right_balls(mini_engine):
    q = mini_engine.con.execute
    # A wide isn't faced: no batting expectation; it still carries a bowling one.
    wide = q("SELECT exp_bat_runs, exp_bowl_runs FROM v_balls WHERE match_id = '1' AND innings_no = 1 AND seq = 2").fetchone()
    assert wide[0] == 0 and wide[1] > 0
    # The non-striker run-out row is an actual out with no expectation attached.
    row = q("SELECT out, exp_runs, exp_out FROM v_bat WHERE player_id = 'a1' AND faced = 0 AND out = 1").fetchone()
    assert row == (1, 0, 0)


def test_thin_cells_shrink_towards_one(mini_engine):
    factors = [r[0] for r in mini_engine.con.execute(
        "SELECT factor FROM test.baseline_factors WHERE factor IS NOT NULL").fetchall()]
    assert factors and all(0.8 < f < 1.25 for f in factors)
    assert {r[0] for r in mini_engine.con.execute("SELECT DISTINCT target FROM test.baseline_factors").fetchall()} == set(TARGETS)


def test_venue_factor_uses_canonical_ground(mini_engine):
    # MINI_MATCH is at "Test Ground"; TEST_ALIASES maps it to the canonical name.
    venues = mini_engine.con.execute(
        "SELECT DISTINCT venue, kind FROM test.baseline_factors WHERE kind = 'venue'").fetchall()
    assert venues == [("Test Ground, Testville", "venue")]


def test_rule_eras_split_factors(mini_engine, test_tournament):
    from dataclasses import replace
    mini_engine.close()  # release the database before writing to it
    con = connect(test_tournament.db_path)
    try:
        rebuild(con, replace(test_tournament, rule_eras=(2025,)))
        eras = {r[0] for r in con.execute("SELECT DISTINCT era FROM baseline_factors").fetchall()}
    finally:
        con.close()
    assert eras == {"2025+"}  # the only season is in the new era


def test_true_metrics_and_tools(mini_engine):
    row = aggregate(mini_engine, "batting", players=["a3"], metrics=["runs", "exp_runs", "true_runs", "true_sr"]).records()[0]
    assert row["true_runs"] == pytest.approx(row["runs"] - row["exp_runs"], abs=0.1)
    t = pl.true_stats(mini_engine, "A3")
    assert {"strike_rate", "exp_sr", "true_sr"} <= set(t.columns)
    assert pl.leaderboard(mini_engine, "true_wickets").columns[:2] == ["player", "team"]  # bowling role inferred
    titles = [s.title for s in pl.profile(mini_engine, "B1")]
    assert not any("true stats" in x for x in titles)  # B1 bowled 5 balls: breakdowns skipped


@pytest.mark.integration
def test_ipl_true_stats_zero_sum_and_flat():
    t = get_tournament("ipl")
    if not t.db_path.exists():
        pytest.skip("IPL database not loaded")
    with open_engine(["ipl"]) as e:
        q = e.con.execute
        # Zero-sum per season, for every target, batting and bowling.
        for view, sums in (("v_bat", BAT_SUMS), ("v_bowl", BOWL_SUMS)):
            for row in q(f"SELECT season_year, {sums} FROM {view} GROUP BY 1").fetchall():
                assert all(abs(v) < 1e-6 for v in row[1:]), (view, row)
        # ...and per season and over, which is where the rescale works.
        worst = q("SELECT max(abs(d)) FROM (SELECT sum(d.runs_batter) FILTER (d.is_ball_faced) - sum(x.exp_bat_runs) AS d "
                  "FROM ipl.deliveries d JOIN ipl.expected x USING (match_id, innings_no, seq) "
                  "JOIN ipl.matches m USING (match_id) GROUP BY m.season_year, x.eff_over)").fetchone()[0]
        assert worst < 1e-6

        # Situation-adjusted: league true SR is ~flat across wickets down and setting/chasing.
        # (Wickets 8 and 9 share a factor, so only cells with real volume are checked.)
        # ball_bucket / entry_bucket: new and late-arriving batters aren't penalised for being new.
        for dim in ("wickets_down", "setting_chasing", "phase", "ball_bucket", "entry_bucket"):
            league = aggregate(e, "batting", by=[dim], metrics=["balls", "true_sr"]).records()
            assert all(abs(r["true_sr"]) < 3 for r in league if r["balls"] >= 5000), league
            league = aggregate(e, "bowling", by=[dim], metrics=["balls", "true_economy"]).records()
            assert all(abs(r["true_economy"]) < 0.3 for r in league if r["balls"] >= 5000), league

        # Venue-adjusted: grounds are ~flat too. Looser than above because venue factors are shrunk
        # hard (VENUE_PRIOR_BALLS), so a ground keeps part of its effect; unadjusted, Chepauk 2023+ was ~-12.
        league = aggregate(e, "batting", by=["venue"], metrics=["balls", "true_sr"]).records()
        assert all(abs(r["true_sr"]) < 4 for r in league if r["balls"] >= 5000), league
        league = aggregate(e, "bowling", by=["venue"], metrics=["balls", "true_economy"]).records()
        assert all(abs(r["true_economy"]) < 0.3 for r in league if r["balls"] >= 5000), league

        # Expectations follow the game: death overs expect more runs than the powerplay.
        sr = {r["phase"]: r["exp_sr"] for r in aggregate(e, "batting", by=["phase"], metrics=["exp_sr"]).records()}
        assert sr["death"] > sr["middle"]
