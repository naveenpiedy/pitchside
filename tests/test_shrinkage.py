import pytest

from pitchside.config import get_tournament
from pitchside.engine import open_engine
from pitchside.model.shrinkage import COMPONENTS, K_MAX
from pitchside.stats import players as pl
from pitchside.stats.filters import FilterError
from pitchside.stats.splits import _shrunk_rate, aggregate


def test_shrunk_rate_formula():
    assert _shrunk_rate(30, 10, 10, 1.0) == pytest.approx(2.0)   # (30 + 10*1) / (10 + 10)
    assert _shrunk_rate(30, 10, 0, 1.0) == pytest.approx(3.0)    # k = 0: no shrinkage
    assert _shrunk_rate(None, 10, 10, 1.0) is None


def test_priors_built_for_every_component(mini_engine):
    assert set(mini_engine.priors) == set(COMPONENTS)
    # One tiny match: no player-season reaches the unit minimum, so every prior is maximal.
    assert all(k == K_MAX for k in mini_engine.priors.values())


def test_shrunk_metrics_follow_the_formula(mini_engine):
    mini_engine.priors = {key: 10.0 for key in mini_engine.priors}
    q = mini_engine.con.execute
    pop_runs, pop_faced = q("SELECT sum(runs), sum(faced) FROM v_bat").fetchone()
    runs, faced = q("SELECT sum(runs), sum(faced) FROM v_bat WHERE player_id = 'a3'").fetchone()
    row = aggregate(mini_engine, "batting", players=["a3"], metrics=["strike_rate", "strike_rate_shrunk"]).records()[0]
    expected = 100 * (runs + 10 * pop_runs / pop_faced) / (faced + 10)
    assert row["strike_rate_shrunk"] == pytest.approx(expected, abs=0.01)
    assert row["strike_rate"] > row["strike_rate_shrunk"] > 100 * pop_runs / pop_faced  # pulled toward the mean


def test_ranking_by_shrunk_metric(mini_engine):
    t = aggregate(mini_engine, "batting", by=["player"], metrics=["balls", "net_sr", "net_sr_shrunk"],
                  order_by="net_sr_shrunk", limit=2)
    values = [r["net_sr_shrunk"] for r in t.records()]
    assert len(values) == 2 and values == sorted(values, reverse=True)
    assert any("k=" in n for n in t.notes)
    lb = pl.leaderboard(mini_engine, "net_economy_shrunk", min_balls=1)  # bowling inferred
    assert "net_economy_shrunk" in lb.columns and "wickets" in lb.columns


def test_missing_priors_is_a_clear_error(mini_engine):
    mini_engine.priors = {}
    with pytest.raises(FilterError, match="rebuild"):
        aggregate(mini_engine, "batting", metrics=["net_sr_shrunk"])
    # Metrics without shrinkage still work.
    assert aggregate(mini_engine, "batting", metrics=["runs"]).rows


@pytest.mark.integration
def test_ipl_priors_are_plausible():
    if not get_tournament("ipl").db_path.exists():
        pytest.skip("IPL database not loaded")
    with open_engine(["ipl"]) as e:
        k = e.priors
        assert 50 < k[("batting", "net_rpb")] < 1000      # net SR half-reliable within a few hundred balls
        assert k[("bowling", "wpb")] > 500                  # wicket-taking is far noisier
        assert k[("batting", "dot")] < k[("batting", "dpb")]  # dot % stabilises faster than dismissals
        # Shrinkage pulls a tiny sample further than a large one.
        t = aggregate(e, "batting", {"season": 2026}, by=["player"],
                      metrics=["balls", "net_sr", "net_sr_shrunk"], min_balls=20).records()
        small = [r for r in t if r["balls"] < 40 and r["net_sr"] is not None]
        large = [r for r in t if r["balls"] > 300]
        ratio = lambda r: abs(r["net_sr_shrunk"]) / max(abs(r["net_sr"]), 1e-9)
        assert max(ratio(r) for r in small) < min(ratio(r) for r in large)
