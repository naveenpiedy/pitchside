"""Per-ball expectations for net stats.

For every delivery we estimate what an average player in the same competition
would produce, for eight targets: the batter's runs, dismissal, dot and
boundary chances, and the bowler's runs, wicket, dot and boundary chances.

    expected = level(season, over) × factor(rules era, phase, wickets down, innings)
               × settle(rules era, phase, balls faced) × chase(rules era, phase, required rate)
               × venue(rules era, ground)

1. level   : the competition's rate for that over in that season. Era
             normalisation comes from here: 2010 and 2025 are each judged
             against their own season.
2. factor  : how much better or worse than the over's level the same situation
             is, given wickets down (each of 0-7, then 8+) and setting vs chasing.
             Coarser buckets (0-2, 3-4...) left openers ~+10 and No. 3s ~-8 in
             net SR, so each wicket gets its own factor.
             Estimated within each rules era (tournaments.toml `rule_eras`,
             e.g. IPL 2023 Impact Player) and per phase so cells stay large,
             then shrunk towards 1 by PRIOR_BALLS of pseudo-data.
   settle  : a second factor for how many balls the striker has already faced
             (1-5 new, 6-10 getting in, 11+ set), per rules era and phase.
             Without it a batter's first five balls run ~18 net-SR points
             below expectation, so late arrivals (finishers) read low and
             bowlers look better against new batters. It stops at 11+ on
             purpose: batters who reach 30 or 40 balls are disproportionately
             the good ones, and a finer split would absorb that skill.
   chase   : a factor per (rules era, phase, required-rate bucket) for chases;
             first innings share one 'setting' cell. The required rate is
             taken relative to the season's run rate. Without it a chaser
             cruising at 5 an over read ~-13 net SR and one needing 12-15
             ~+15: the target sets the tempo, not the batter.
   venue   : a third factor per (rules era, ground), so a batter based at a
             flat ground isn't credited with the pitch (2022-25 IPL grounds
             spanned ~25 batting runs per 120 balls, Chepauk to Eden Gardens).
             Grounds are canonicalised through aliases.toml. Shrunk much harder
             (VENUE_PRIOR_BALLS) because half a ground's balls belong to the
             home side: a lightly shrunk factor would absorb the home team's skill.
3. rescale : within each (season, over), expectations are scaled so their sum
             equals the actual total. The factors and the rescale alternate
             (iterative proportional fitting) so expectations match actuals on
             every margin: situation cells, settle cells and season-overs. The
             last step is always the rescale, so net stats sum to exactly zero
             per season and over, and an "average" player scores 0.

Overs of shortened innings are mapped onto the full-length scale (`eff_over`)
so a 5-over game's last over is compared with other last overs.
"""

from __future__ import annotations

import duckdb

PRIOR_BALLS = 50   # pseudo-balls pulling thin factor cells towards 1
VENUE_PRIOR_BALLS = 2400  # ~10 matches: a ground with 10 matches keeps about half its observed effect
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
# Chase pressure: required runs per ball ÷ the season's runs per legal ball (IPL ~1.4, so 1.0 ≈ 8.4 an over).
CHASE_BOUNDS = [0.6, 0.8, 0.95, 1.1, 1.3, 1.6, 1.9, 2.2, 3.0]
CHASE_BUCKETS = ["setting"] + [f"<{CHASE_BOUNDS[0]}"] + [
    f"{a}-{b}" for a, b in zip(CHASE_BOUNDS, CHASE_BOUNDS[1:])] + [f"{CHASE_BOUNDS[-1]}+"]

# alias: (cell keys, prior in pseudo-balls). Fitted in this order, then the season-over rescale.
FACTORS: dict[str, tuple[str, int]] = {
    "f": ("era, phase, wb, inns", PRIOR_BALLS),   # situation
    "g": ("era, phase, bf", PRIOR_BALLS),         # settle
    "c": ("era, phase, rr", PRIOR_BALLS),         # chase pressure
    "v": ("era, venue", VENUE_PRIOR_BALLS),       # venue
}


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
    # Required rate relative to the season's scoring rate, so a bucket means the
    # same pressure in 2008 and 2026. A ball with nothing left to bowl falls in the top bucket.
    ratio = "(d.runs_required / nullif(d.balls_remaining, 0)) / sr.rpb"
    chase = "CASE WHEN d.innings_no = 1 THEN 0 " + " ".join(
        f"WHEN {ratio} < {b} THEN {i + 1}" for i, b in enumerate(CHASE_BOUNDS)) + f" ELSE {len(CHASE_BOUNDS) + 1} END"
    return f"""
CREATE OR REPLACE TEMP TABLE _bb AS
WITH sr AS (SELECT m.season_year, sum(d.runs_total) / nullif(sum(d.is_legal::INT), 0) AS rpb
            FROM deliveries d JOIN matches m USING (match_id) GROUP BY ALL)
SELECT d.match_id, d.innings_no, d.seq, m.season_year, {era} AS era, d.phase,
       least(m.overs - 1, floor(d.over_no * m.overs * m.balls_per_over / nullif(i.allocated_balls, 0)))::INT AS eff_over,
       least(d.wickets_before, 8) AS wb,
       d.innings_no AS inns,
       coalesce(va.canonical, m.venue, 'unknown') AS venue,
       CASE WHEN d.batter_balls_before < 5 THEN 0 WHEN d.batter_balls_before < 10 THEN 1 ELSE 2 END AS bf,
       {chase} AS rr,
       {cols}
FROM deliveries d
JOIN matches m USING (match_id)
JOIN innings i USING (match_id, innings_no)
JOIN sr USING (season_year)
LEFT JOIN _venue_alias va ON va.name = m.venue
WHERE NOT i.is_super_over
"""


def _target_sql(t: str) -> str:
    y, w = f"y_{t}", f"w_{t}"
    inits = "\n".join(f"CREATE OR REPLACE TEMP TABLE _{a}_{t} AS SELECT DISTINCT {k}, 1.0 AS factor, 0 AS weight FROM _bb;"
                      for a, (k, _) in FACTORS.items())
    return f"""
-- 1. level per season and over
CREATE OR REPLACE TEMP TABLE _l_{t} AS
SELECT season_year, eff_over, sum({y}) / nullif(sum({w}), 0) AS level, sum({w}) AS weight
FROM _bb GROUP BY ALL;

-- prediction from the level alone
CREATE OR REPLACE TEMP TABLE _p_{t} AS
SELECT b.match_id, b.innings_no, b.seq, b.season_year, b.eff_over, b.era, b.phase, b.wb, b.inns, b.bf, b.rr, b.venue,
       b.{y} AS y, b.{w} AS w, coalesce(l.level, 0) * b.{w} AS pred
FROM _bb b JOIN _l_{t} l USING (season_year, eff_over);

CREATE OR REPLACE TEMP TABLE _s_{t} AS
SELECT season_year, eff_over, 1.0 AS scale FROM _l_{t};
{inits}
"""


def _factor(prior: int) -> str:
    """Factor for a cell given everything else: (actual + prior) / (predicted + prior)."""
    return f"""(sum(y) + {prior} * sum(sp) / nullif(sum(w), 0))
         / nullif(sum(sp) + {prior} * sum(sp) / nullif(sum(w), 0), 0) AS factor"""


def _product(t: str, skip: str | None = None) -> tuple[str, str]:
    """(joins, product) of every factor table except `skip`."""
    names = [a for a in FACTORS if a != skip]
    joins = " ".join(f"JOIN _{a}_{t} {a} USING ({FACTORS[a][0]})" for a in names)
    return joins, " * ".join(f"coalesce({a}.factor, 1)" for a in names)


def _fit_sql(t: str) -> str:
    """One round of iterative proportional fitting: each factor given the
    others and the current season-over scales, then scales given the factors."""
    sql = []
    for a, (keys, prior) in FACTORS.items():
        joins, prod = _product(t, skip=a)
        sql.append(f"""
CREATE OR REPLACE TEMP TABLE _{a}_{t} AS
WITH p AS (SELECT p.*, p.pred * s.scale * {prod} AS sp FROM _p_{t} p
           JOIN _s_{t} s USING (season_year, eff_over) {joins})
SELECT {keys}, sum(w) AS weight, {_factor(prior)}
FROM p GROUP BY ALL;""")
    joins, prod = _product(t)
    sql.append(f"""
-- rescale so each (season, over) sums to its actual total
CREATE OR REPLACE TEMP TABLE _s_{t} AS
SELECT p.season_year, p.eff_over, coalesce(sum(p.y) / nullif(sum(p.pred * {prod}), 0), 0) AS scale
FROM _p_{t} p {joins}
GROUP BY ALL;""")
    return "\n".join(sql)


def _expected_sql(t: str) -> str:
    joins, prod = _product(t)
    return f"""
CREATE OR REPLACE TEMP TABLE _e_{t} AS
SELECT p.match_id, p.innings_no, p.seq, p.eff_over, p.pred * {prod} * s.scale AS exp_{t}
FROM _p_{t} p {joins}
JOIN _s_{t} s USING (season_year, eff_over);
"""


def build_expectations(con: duckdb.DuckDBPyConnection, rule_eras: tuple[int, ...] | list[int] = (),
                       venue_aliases: dict[str, str] | None = None) -> None:
    """(Re)build `expected` (one row per delivery) and the inspection tables
    `baseline_levels` and `baseline_factors` in a competition database.
    `venue_aliases` maps each venue name to its canonical ground (aliases.toml)."""
    con.execute("CREATE OR REPLACE TEMP TABLE _venue_alias (name VARCHAR PRIMARY KEY, canonical VARCHAR)")
    if venue_aliases:
        con.executemany("INSERT INTO _venue_alias VALUES (?, ?)", list(venue_aliases.items()))
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
    rr_case = "CASE rr " + " ".join(f"WHEN {i} THEN '{lab}'" for i, lab in enumerate(CHASE_BUCKETS)) + " END"
    con.execute("CREATE OR REPLACE TABLE baseline_factors AS " + " UNION ALL ".join(
        f"SELECT '{t}' AS target, 'situation' AS kind, {era_case} AS era, phase, {wb_case} AS wickets_down, "
        f"CASE inns WHEN 1 THEN 'setting' ELSE 'chasing' END AS innings, NULL::VARCHAR AS balls_faced, "
        f"NULL::VARCHAR AS required_rate, NULL::VARCHAR AS venue, factor, weight FROM _f_{t} UNION ALL "
        f"SELECT '{t}', 'settle', {era_case}, phase, NULL, NULL, {bf_case}, NULL, NULL, factor, weight FROM _g_{t} UNION ALL "
        f"SELECT '{t}', 'chase', {era_case}, phase, NULL, NULL, NULL, {rr_case}, NULL, factor, weight FROM _c_{t} UNION ALL "
        f"SELECT '{t}', 'venue', {era_case}, NULL, NULL, NULL, NULL, NULL, venue, factor, weight FROM _v_{t}"
        for t in TARGETS))
    for t in TARGETS:
        for prefix in ("_l", "_p", "_s", "_e", *(f"_{a}" for a in FACTORS)):
            con.execute(f"DROP TABLE IF EXISTS {prefix}_{t}")
    con.execute("DROP TABLE IF EXISTS _bb")
    con.execute("DROP TABLE IF EXISTS _venue_alias")
