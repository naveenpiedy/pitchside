"""Per-ball expectations for true stats.

For every delivery we estimate what an average player in the same competition
would produce, for eight targets: the batter's runs, dismissal, dot and
boundary chances, and the bowler's runs, wicket, dot and boundary chances.

    expected = level(season, over) × factor(rules era, phase, wickets down, innings)

1. level   : the competition's rate for that over in that season. Era
             normalisation comes from here: 2010 and 2025 are each judged
             against their own season.
2. factor  : how much better or worse than the over's level the same situation
             is, given wickets down (each of 0-7, then 8+) and setting vs chasing.
             Coarser buckets (0-2, 3-4...) left openers ~+10 and No. 3s ~-8 in
             true SR, so each wicket gets its own factor.
             Estimated within each rules era (tournaments.toml `rule_eras`,
             e.g. IPL 2023 Impact Player) and per phase so cells stay large,
             then shrunk towards 1 by PRIOR_BALLS of pseudo-data.
   settle  : a second factor for how many balls the striker has already faced
             (1-5 new, 6-10 getting in, 11+ set), per rules era and phase.
             Without it a batter's first five balls run ~18 true-SR points
             below expectation, so late arrivals (finishers) read low and
             bowlers look better against new batters. It stops at 11+ on
             purpose: batters who reach 30 or 40 balls are disproportionately
             the good ones, and a finer split would absorb that skill.
3. rescale : within each (season, over), expectations are scaled so their sum
             equals the actual total. The factors and the rescale alternate
             (iterative proportional fitting) so expectations match actuals on
             every margin: situation cells, settle cells and season-overs. The
             last step is always the rescale, so true stats sum to exactly zero
             per season and over, and an "average" player scores 0.

Overs of shortened innings are mapped onto the full-length scale (`eff_over`)
so a 5-over game's last over is compared with other last overs.
"""

from __future__ import annotations

import duckdb

PRIOR_BALLS = 50   # pseudo-balls pulling thin factor cells towards 1
FIT_ROUNDS = 10    # alternating factor / scale fits; converges well within this

# target: (value per delivery, weight: which deliveries carry the expectation)
TARGETS: dict[str, tuple[str, str]] = {
    "bat_runs": ("runs_batter", "is_ball_faced::INT"),
    "bat_out": ("is_wicket::INT", "is_ball_faced::INT"),
    "bat_dot": ("(is_ball_faced AND runs_batter = 0)::INT", "is_ball_faced::INT"),
    "bat_bnd": ("(is_four OR is_six)::INT", "is_ball_faced::INT"),
    "bowl_runs": ("bowler_runs", "1"),
    "bowl_wkt": ("is_bowler_wicket::INT", "1"),
    "bowl_dot": ("(is_legal AND bowler_runs = 0)::INT", "1"),
    "bowl_bnd": ("(is_four OR is_six)::INT", "1"),
}
WICKET_BUCKETS = ["0", "1", "2", "3", "4", "5", "6", "7", "8+"]  # wickets down; 8 and 9 pooled
SETTLE_BUCKETS = ["1-5", "6-10", "11+"]  # the striker's nth ball


def era_labels(rule_eras: tuple[int, ...] | list[int]) -> list[str]:
    """Labels for era ids 0..n: [2023] -> ['before 2023', '2023+']."""
    bounds = sorted(rule_eras)
    if not bounds:
        return ["all"]
    labels = [f"before {bounds[0]}"]
    labels += [f"{a}-{b - 1}" for a, b in zip(bounds, bounds[1:])]
    labels.append(f"{bounds[-1]}+")
    return labels


def _base_sql(rule_eras: tuple[int, ...] | list[int]) -> str:
    era = " + ".join(f"(m.season_year >= {int(y)})::INT" for y in sorted(rule_eras)) or "0"
    cols = ",\n".join(f"{v} AS y_{t}, {w} AS w_{t}" for t, (v, w) in TARGETS.items())
    return f"""
CREATE OR REPLACE TEMP TABLE _bb AS
SELECT d.match_id, d.innings_no, d.seq, m.season_year, {era} AS era, d.phase,
       least(m.overs - 1, floor(d.over_no * m.overs * m.balls_per_over / nullif(i.allocated_balls, 0)))::INT AS eff_over,
       least(d.wickets_before, 8) AS wb,
       d.innings_no AS inns,
       CASE WHEN d.batter_balls_before < 5 THEN 0 WHEN d.batter_balls_before < 10 THEN 1 ELSE 2 END AS bf,
       {cols}
FROM deliveries d
JOIN matches m USING (match_id)
JOIN innings i USING (match_id, innings_no)
WHERE NOT i.is_super_over
"""


def _target_sql(t: str) -> str:
    y, w = f"y_{t}", f"w_{t}"
    return f"""
-- 1. level per season and over
CREATE OR REPLACE TEMP TABLE _l_{t} AS
SELECT season_year, eff_over, sum({y}) / nullif(sum({w}), 0) AS level, sum({w}) AS weight
FROM _bb GROUP BY ALL;

-- prediction from the level alone
CREATE OR REPLACE TEMP TABLE _p_{t} AS
SELECT b.match_id, b.innings_no, b.seq, b.season_year, b.eff_over, b.era, b.phase, b.wb, b.inns, b.bf,
       b.{y} AS y, b.{w} AS w, coalesce(l.level, 0) * b.{w} AS pred
FROM _bb b JOIN _l_{t} l USING (season_year, eff_over);

CREATE OR REPLACE TEMP TABLE _s_{t} AS
SELECT season_year, eff_over, 1.0 AS scale FROM _l_{t};
CREATE OR REPLACE TEMP TABLE _g_{t} AS
SELECT DISTINCT era, phase, bf, 1.0 AS factor, 0 AS weight FROM _bb;
"""

# Factor for a cell given everything else: (actual + prior) / (predicted + prior).
_FACTOR = f"""(sum(y) + {PRIOR_BALLS} * sum(sp) / nullif(sum(w), 0))
         / nullif(sum(sp) + {PRIOR_BALLS} * sum(sp) / nullif(sum(w), 0), 0) AS factor"""


def _fit_sql(t: str) -> str:
    """One round of iterative proportional fitting: each factor given the
    others and the current season-over scales, then scales given the factors."""
    return f"""
-- 2a. situation factor per (era, phase, wickets, innings), with PRIOR_BALLS pseudo-balls at 1
CREATE OR REPLACE TEMP TABLE _f_{t} AS
WITH p AS (SELECT p.*, p.pred * s.scale * g.factor AS sp FROM _p_{t} p
           JOIN _s_{t} s USING (season_year, eff_over) JOIN _g_{t} g USING (era, phase, bf))
SELECT era, phase, wb, inns, sum(w) AS weight, {_FACTOR}
FROM p GROUP BY ALL;

-- 2b. settle factor per (era, phase, balls already faced)
CREATE OR REPLACE TEMP TABLE _g_{t} AS
WITH p AS (SELECT p.*, p.pred * s.scale * coalesce(f.factor, 1) AS sp FROM _p_{t} p
           JOIN _s_{t} s USING (season_year, eff_over) JOIN _f_{t} f USING (era, phase, wb, inns))
SELECT era, phase, bf, sum(w) AS weight, {_FACTOR}
FROM p GROUP BY ALL;

-- 3. rescale so each (season, over) sums to its actual total
CREATE OR REPLACE TEMP TABLE _s_{t} AS
SELECT p.season_year, p.eff_over,
       coalesce(sum(p.y) / nullif(sum(p.pred * coalesce(f.factor, 1) * coalesce(g.factor, 1)), 0), 0) AS scale
FROM _p_{t} p JOIN _f_{t} f USING (era, phase, wb, inns) JOIN _g_{t} g USING (era, phase, bf)
GROUP BY ALL;
"""


def _expected_sql(t: str) -> str:
    return f"""
CREATE OR REPLACE TEMP TABLE _e_{t} AS
SELECT p.match_id, p.innings_no, p.seq, p.eff_over,
       p.pred * coalesce(f.factor, 1) * coalesce(g.factor, 1) * s.scale AS exp_{t}
FROM _p_{t} p
JOIN _f_{t} f USING (era, phase, wb, inns)
JOIN _g_{t} g USING (era, phase, bf)
JOIN _s_{t} s USING (season_year, eff_over);
"""


def build_expectations(con: duckdb.DuckDBPyConnection, rule_eras: tuple[int, ...] | list[int] = ()) -> None:
    """(Re)build `expected` (one row per delivery) and the inspection tables
    `baseline_levels` and `baseline_factors` in a competition database."""
    con.execute(_base_sql(rule_eras))
    for t in TARGETS:
        con.execute(_target_sql(t))
        for _ in range(FIT_ROUNDS):
            con.execute(_fit_sql(t))
        con.execute(_expected_sql(t))

    exp_cols = ", ".join(f"_e_{t}.exp_{t}" for t in TARGETS)
    joins = " ".join(f"JOIN _e_{t} USING (match_id, innings_no, seq)" for t in list(TARGETS)[1:])
    first = next(iter(TARGETS))
    con.execute(f"CREATE OR REPLACE TABLE expected AS SELECT match_id, innings_no, seq, _e_{first}.eff_over, {exp_cols} "
                f"FROM _e_{first} {joins}")

    con.execute("CREATE OR REPLACE TABLE baseline_levels AS " + " UNION ALL ".join(
        f"SELECT '{t}' AS target, season_year, eff_over + 1 AS over, level, weight FROM _l_{t}" for t in TARGETS))
    labels = era_labels(rule_eras)
    era_case = "CASE era " + " ".join(f"WHEN {i} THEN '{lab}'" for i, lab in enumerate(labels)) + " END"
    wb_case = "CASE wb " + " ".join(f"WHEN {i} THEN '{lab}'" for i, lab in enumerate(WICKET_BUCKETS)) + " END"
    bf_case = "CASE bf " + " ".join(f"WHEN {i} THEN '{lab}'" for i, lab in enumerate(SETTLE_BUCKETS)) + " END"
    con.execute("CREATE OR REPLACE TABLE baseline_factors AS " + " UNION ALL ".join(
        f"SELECT '{t}' AS target, 'situation' AS kind, {era_case} AS era, phase, {wb_case} AS wickets_down, "
        f"CASE inns WHEN 1 THEN 'setting' ELSE 'chasing' END AS innings, NULL::VARCHAR AS balls_faced, "
        f"factor, weight FROM _f_{t} UNION ALL "
        f"SELECT '{t}', 'settle', {era_case}, phase, NULL, NULL, {bf_case}, factor, weight FROM _g_{t}"
        for t in TARGETS))
    for t in TARGETS:
        for prefix in ("_l", "_p", "_f", "_g", "_s", "_e"):
            con.execute(f"DROP TABLE IF EXISTS {prefix}_{t}")
    con.execute("DROP TABLE IF EXISTS _bb")
