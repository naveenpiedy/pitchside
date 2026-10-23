"""Empirical-Bayes shrinkage priors for small samples (add-on A3).

A player's per-ball rate is pulled towards the population's rate with the
strength of k pseudo-balls:

    shrunk = (player total + k × population rate) / (player balls + k)

k = σ² / τ²: per-ball noise over the spread of true talent between players,
estimated by the method of moments over player-seasons (each a unit):

    E[Σ nᵢ (pᵢ − p̄)²] ≈ (N − 1) σ² + τ² (Σ nᵢ − Σ nᵢ² / Σ nᵢ)

A metric that separates players quickly (dot %) gets a small k; a noisy one
(dismissal rate) gets a large k. Stored per competition in `shrink_priors`.
"""

from __future__ import annotations

import duckdb

K_MIN, K_MAX = 20.0, 5000.0
# Player-seasons below this are left out of the estimate: part-timers with a
# handful of balls swamp the talent spread (IPL wicket rate: k ≈ 1400 with the
# cut, no detectable spread without it).
MIN_UNIT_BALLS = 60

# (role, component): (per-ball value, weight = which rows count as balls)
COMPONENTS: dict[tuple[str, str], tuple[str, str]] = {
    ("batting", "rpb"): ("runs_batter", "is_ball_faced::INT"),
    ("batting", "dpb"): ("coalesce(out_player_id = batter_id, false)::INT", "is_ball_faced::INT"),
    ("batting", "dot"): ("(is_ball_faced AND runs_batter = 0)::INT", "is_ball_faced::INT"),
    ("batting", "bnd"): ("(is_four OR is_six)::INT", "is_ball_faced::INT"),
    ("batting", "net_rpb"): ("runs_batter - exp_bat_runs", "is_ball_faced::INT"),
    ("batting", "net_dpb"): ("coalesce(out_player_id = batter_id, false)::INT - exp_bat_out", "is_ball_faced::INT"),
    ("bowling", "rpb"): ("bowler_runs", "is_legal::INT"),
    ("bowling", "wpb"): ("is_bowler_wicket::INT", "is_legal::INT"),
    ("bowling", "dot"): ("(is_legal AND bowler_runs = 0)::INT", "is_legal::INT"),
    ("bowling", "net_rpb"): ("exp_bowl_runs - bowler_runs", "is_legal::INT"),
    ("bowling", "net_wpb"): ("is_bowler_wicket::INT - exp_bowl_wkt", "is_legal::INT"),
}


def _component_sql(role: str, name: str, y: str, w: str) -> str:
    player = "batter_id" if role == "batting" else "bowler_id"
    return f"""
WITH balls AS (
    SELECT d.{player} AS player_id, m.season_year, ({y})::DOUBLE AS y, {w} AS w
    FROM deliveries d
    JOIN matches m USING (match_id)
    JOIN innings i USING (match_id, innings_no)
    JOIN expected e USING (match_id, innings_no, seq)
    WHERE NOT i.is_super_over
),
pooled AS (SELECT sum(y) / sum(w) AS p, sum(y * y) / sum(w) - pow(sum(y) / sum(w), 2) AS sigma2 FROM balls),
units AS (
    SELECT player_id, season_year, sum(w) AS n, sum(y) / sum(w) AS p
    FROM balls GROUP BY ALL HAVING sum(w) >= {MIN_UNIT_BALLS}
),
moments AS (
    SELECT count(*) AS n_units, sum(n) AS sn, sum(n * n) AS snn,
           sum(n * pow(u.p - pooled.p, 2)) AS s
    FROM units u, pooled
)
SELECT '{role}' AS role, '{name}' AS component, pooled.p AS population_rate, pooled.sigma2,
       (s - (n_units - 1) * pooled.sigma2) / nullif(sn - snn / sn, 0) AS tau2, n_units
FROM moments, pooled
"""


def build_priors(con: duckdb.DuckDBPyConnection) -> None:
    """(Re)build `shrink_priors` (role, component, k, population_rate, sigma2, tau2, n_units)."""
    rows = []
    for (role, name), (y, w) in COMPONENTS.items():
        role_, comp, rate, sigma2, tau2, n_units = con.execute(_component_sql(role, name, y, w)).fetchone()
        if tau2 is None or tau2 <= 0 or sigma2 is None:
            k = K_MAX
        else:
            k = min(max(sigma2 / tau2, K_MIN), K_MAX)
        rows.append((role_, comp, k, rate, sigma2, tau2, n_units))
    con.execute("""CREATE OR REPLACE TABLE shrink_priors (
        role VARCHAR, component VARCHAR, k DOUBLE, population_rate DOUBLE,
        sigma2 DOUBLE, tau2 DOUBLE, n_units INTEGER)""")
    con.executemany("INSERT INTO shrink_priors VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
