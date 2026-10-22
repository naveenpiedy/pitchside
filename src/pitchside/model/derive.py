"""Precomputed tables built from the core tables after every load.

Each one is rebuilt in full: a whole competition is a few hundred thousand
deliveries, so a rebuild takes well under a second and avoids incremental
bookkeeping. Super overs are excluded from all of them.
"""

from __future__ import annotations

import duckdb

SEASON_YEAR = """
UPDATE matches AS m SET season_year = s.y
FROM (SELECT season, min(year(match_date)) AS y FROM matches GROUP BY season) AS s
WHERE m.season = s.season
"""

# One row per batter per innings, including batters who never faced a ball.
BATTING_INNINGS = """
CREATE OR REPLACE TABLE batting_innings AS
WITH d AS (
    SELECT match_id, innings_no, batter_id AS player_id,
           sum(runs_batter)::BIGINT                                   AS runs,
           count(*) FILTER (is_ball_faced)                    AS balls,
           count(*) FILTER (is_four)                          AS fours,
           count(*) FILTER (is_six)                           AS sixes,
           count(*) FILTER (is_ball_faced AND runs_batter = 0) AS dots
    FROM deliveries GROUP BY ALL
),
w AS (
    -- If a batter retired hurt and was later out, the real dismissal wins.
    SELECT * FROM (
        SELECT *, row_number() OVER (
            PARTITION BY match_id, innings_no, player_out_id
            ORDER BY counts_as_out DESC, seq DESC) AS rn
        FROM wickets
    ) WHERE rn = 1
)
SELECT bo.match_id, bo.innings_no, m.season_year, m.match_date,
       i.batting_team AS team, i.bowling_team AS opposition,
       bo.player_id, bo.position,
       coalesce(d.runs, 0)  AS runs,
       coalesce(d.balls, 0) AS balls,
       coalesce(d.fours, 0) AS fours,
       coalesce(d.sixes, 0) AS sixes,
       coalesce(d.dots, 0)  AS dots,
       coalesce(w.counts_as_out, false) AS is_out,
       w.kind AS dismissal,
       CASE WHEN w.bowler_credit THEN w.bowler_id END AS dismissed_by,
       w.fielders, w.fielder_ids,
       bo.entry_legal_balls, bo.entry_score, bo.entry_wickets
FROM batting_order bo
JOIN innings i USING (match_id, innings_no)
JOIN matches m USING (match_id)
LEFT JOIN d ON d.match_id = bo.match_id AND d.innings_no = bo.innings_no AND d.player_id = bo.player_id
LEFT JOIN w ON w.match_id = bo.match_id AND w.innings_no = bo.innings_no AND w.player_out_id = bo.player_id
WHERE NOT i.is_super_over
"""

BOWLING_INNINGS = """
CREATE OR REPLACE TABLE bowling_innings AS
WITH d AS (
    SELECT match_id, innings_no, bowler_id AS player_id,
           min(seq)                                     AS first_seq,
           count(*) FILTER (is_legal)                   AS balls,
           sum(bowler_runs)::BIGINT                             AS runs,
           count(*) FILTER (is_legal AND bowler_runs = 0) AS dots,
           count(*) FILTER (is_four)                    AS fours,
           count(*) FILTER (is_six)                     AS sixes,
           count(*) FILTER (wides > 0)                  AS wides,
           count(*) FILTER (noballs > 0)                AS noballs
    FROM deliveries GROUP BY ALL
),
wk AS (
    SELECT match_id, innings_no, bowler_id AS player_id, count(*) AS wickets
    FROM wickets WHERE bowler_credit GROUP BY ALL
),
overs AS (
    SELECT match_id, innings_no, over_no, any_value(bowler_id) AS player_id,
           count(DISTINCT bowler_id) AS bowlers,
           count(*) FILTER (is_legal) AS legal, sum(bowler_runs)::BIGINT AS runs
    FROM deliveries GROUP BY ALL
),
maidens AS (
    SELECT o.match_id, o.innings_no, o.player_id, count(*) AS maidens
    FROM overs o JOIN matches m USING (match_id)
    WHERE o.bowlers = 1 AND o.legal = m.balls_per_over AND o.runs = 0
    GROUP BY ALL
)
SELECT d.match_id, d.innings_no, m.season_year, m.match_date,
       i.bowling_team AS team, i.batting_team AS opposition, d.player_id,
       row_number() OVER (PARTITION BY d.match_id, d.innings_no ORDER BY d.first_seq) AS bowling_order,
       d.balls, d.runs, coalesce(wk.wickets, 0) AS wickets, coalesce(mo.maidens, 0) AS maidens,
       d.dots, d.fours, d.sixes, d.wides, d.noballs
FROM d
JOIN innings i USING (match_id, innings_no)
JOIN matches m USING (match_id)
LEFT JOIN wk ON wk.match_id = d.match_id AND wk.innings_no = d.innings_no AND wk.player_id = d.player_id
LEFT JOIN maidens mo ON mo.match_id = d.match_id AND mo.innings_no = d.innings_no AND mo.player_id = d.player_id
WHERE NOT i.is_super_over
"""

SEASON_BATTING = """
CREATE OR REPLACE TABLE season_batting AS
SELECT season_year, player_id,
       string_agg(DISTINCT team, ', ')             AS teams,
       count(DISTINCT match_id)                    AS matches,
       count(*)                                    AS innings,
       count(*) FILTER (NOT is_out)                AS not_outs,
       sum(runs)::BIGINT                                   AS runs,
       sum(balls)::BIGINT                                  AS balls,
       count(*) FILTER (is_out)                    AS outs,
       max(runs)                                   AS high_score,
       NOT arg_max(is_out, runs * 2 + (NOT is_out)::INT) AS high_score_not_out,
       count(*) FILTER (runs >= 50 AND runs < 100) AS fifties,
       count(*) FILTER (runs >= 100)               AS hundreds,
       sum(fours)::BIGINT                                  AS fours,
       sum(sixes)::BIGINT                                  AS sixes,
       sum(dots)::BIGINT                                   AS dots,
       round(sum(runs) / nullif(count(*) FILTER (is_out), 0), 2)     AS average,
       round(100.0 * sum(runs) / nullif(sum(balls), 0), 2)           AS strike_rate,
       round(100.0 * (sum(fours) + sum(sixes)) / nullif(sum(balls), 0), 2) AS boundary_pct,
       round(100.0 * sum(dots) / nullif(sum(balls), 0), 2)           AS dot_pct
FROM batting_innings
GROUP BY ALL
"""

SEASON_BOWLING = """
CREATE OR REPLACE TABLE season_bowling AS
SELECT season_year, player_id,
       string_agg(DISTINCT team, ', ')           AS teams,
       count(DISTINCT match_id)                  AS matches,
       count(*)                                  AS innings,
       sum(balls)::BIGINT                                AS balls,
       sum(runs)::BIGINT                                 AS runs,
       sum(wickets)::BIGINT                              AS wickets,
       sum(maidens)::BIGINT                              AS maidens,
       sum(dots)::BIGINT                                 AS dots,
       sum(fours)::BIGINT                                AS fours,
       sum(sixes)::BIGINT                                AS sixes,
       count(*) FILTER (wickets >= 4)            AS four_wkt_hauls,
       arg_max(wickets, wickets * 1000 - runs)   AS best_wickets,
       arg_max(runs, wickets * 1000 - runs)      AS best_runs,
       round(sum(runs) / nullif(sum(wickets), 0), 2)            AS average,
       round(6.0 * sum(runs) / nullif(sum(balls), 0), 2)        AS economy,
       round(sum(balls) / nullif(sum(wickets), 0), 2)           AS strike_rate,
       round(100.0 * sum(dots) / nullif(sum(balls), 0), 2)      AS dot_pct
FROM bowling_innings
GROUP BY ALL
"""

STEPS = [SEASON_YEAR, BATTING_INNINGS, BOWLING_INNINGS, SEASON_BATTING, SEASON_BOWLING]


def refresh(con: duckdb.DuckDBPyConnection) -> None:
    for sql in STEPS:
        con.execute(sql)
