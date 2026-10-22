"""Win probability and win probability added (add-on A1).

Two logistic regressions per competition, fitted on its own history, giving
the batting side's chance of winning before every ball:

- first innings: runs ahead of the era's pace so far, wickets in hand, balls
  left (and their interaction);
- chase: how hard the required rate is relative to that season's scoring
  rate, wickets in hand, balls left (and interactions).

"Era" is the season's runs per legal ball, so needing 10 an over in 2010 is
harder than in 2025. Ties count as half a win; no-result matches are left out.

Win probability added (WPA) per ball = batting side's win probability after
the ball minus before. The first innings ends at the chase's starting
probability (from the other side), the chase ends at the result. Batters get
the WPA of balls they face (none for wides); bowlers get minus the WPA of
every ball they bowl. Units are wins: a season WPA of +1.5 is a match and a
half won above an average replacement of the same balls.

Fitted by Newton's method with the gradient and Hessian as DuckDB aggregates
(small L2 penalty), so no numpy/scikit-learn dependency.
"""

from __future__ import annotations

import math

import duckdb

L2 = 1.0           # ridge penalty on standardised coefficients
NEWTON_STEPS = 25
TOLERANCE = 1e-8

# Per-ball state, one row per non-super-over delivery of a decided or tied match.
STATE_SQL = """
CREATE OR REPLACE TEMP TABLE _wp_state AS
WITH season_rate AS (
    SELECT m.season_year, sum(d.runs_total)::DOUBLE / sum(d.is_legal::INT) AS rpb
    FROM deliveries d JOIN matches m USING (match_id) JOIN innings i USING (match_id, innings_no)
    WHERE NOT i.is_super_over GROUP BY 1
)
SELECT d.match_id, d.innings_no, d.seq,
       d.score_before::DOUBLE AS score, (10 - d.wickets_before)::DOUBLE AS wkts_in_hand,
       greatest(d.balls_remaining, 0)::DOUBLE AS balls_left, i.allocated_balls::DOUBLE AS allocation,
       d.runs_required::DOUBLE AS runs_required, sr.rpb,
       CASE WHEN m.result = 'no result' OR m.result IS NULL THEN NULL
            WHEN m.result = 'tie' OR m.winner IS NULL THEN 0.5
            WHEN m.winner = i.batting_team THEN 1.0 ELSE 0.0 END AS y
FROM deliveries d
JOIN innings i USING (match_id, innings_no)
JOIN matches m USING (match_id)
JOIN season_rate sr ON sr.season_year = m.season_year
WHERE NOT i.is_super_over AND d.innings_no IN (1, 2)
"""

# Features per innings (SQL over _wp_state). Index 0 is the intercept.
FEATURES: dict[int, list[tuple[str, str]]] = {
    1: [
        ("intercept", "1.0"),
        ("runs_ahead", "(score - rpb * (allocation - balls_left)) / (rpb * 120)"),
        ("wickets", "wkts_in_hand / 10"),
        ("balls_left", "balls_left / 120"),
        ("wickets_x_balls", "(wkts_in_hand / 10) * (balls_left / 120)"),
        ("ahead_x_balls", "((score - rpb * (allocation - balls_left)) / (rpb * 120)) * (balls_left / 120)"),
    ],
    2: [
        ("intercept", "1.0"),
        ("difficulty", "ln((runs_required + 0.5) / ((balls_left + 0.5) * rpb))"),
        ("wickets", "wkts_in_hand / 10"),
        ("log_balls", "ln(balls_left + 1)"),
        ("difficulty_x_wickets", "ln((runs_required + 0.5) / ((balls_left + 0.5) * rpb)) * (wkts_in_hand / 10)"),
        ("difficulty_x_balls", "ln((runs_required + 0.5) / ((balls_left + 0.5) * rpb)) * ln(balls_left + 1)"),
        ("wickets_x_balls", "(wkts_in_hand / 10) * ln(balls_left + 1)"),
    ],
}


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Solve a·x = b by Gaussian elimination with partial pivoting (small systems)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        if abs(m[col][col]) < 1e-12:
            raise ValueError("singular system")
        for r in range(n):
            if r != col:
                factor = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= factor * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def _logit(cols: list[str], beta: list[float]) -> str:
    return " + ".join(f"({b!r}) * {c}" for c, b in zip(cols, beta))


def fit_innings(con: duckdb.DuckDBPyConnection, innings: int) -> tuple[list[str], list[float], dict]:
    """Fit one innings' model on standardised features; return (names, coefficients on raw features, stats)."""
    feats = FEATURES[innings]
    names = [n for n, _ in feats]
    k = len(feats)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _wp_train AS
        SELECT {', '.join(f'{sql} AS f{j}' for j, (_, sql) in enumerate(feats))}, y
        FROM _wp_state WHERE innings_no = {innings} AND y IS NOT NULL
          AND (innings_no = 1 OR runs_required > 0)""")
    # Standardise (except the intercept) so the ridge penalty and Newton steps are well scaled.
    stats = con.execute("SELECT " + ", ".join(f"avg(f{j}), stddev_pop(f{j})" for j in range(1, k)) +
                        " FROM _wp_train").fetchone()
    mean = [0.0] + [stats[2 * (j - 1)] for j in range(1, k)]
    std = [1.0] + [stats[2 * (j - 1) + 1] or 1.0 for j in range(1, k)]
    cols = ["1.0"] + [f"((f{j} - {mean[j]!r}) / {std[j]!r})" for j in range(1, k)]

    beta = [0.0] * k
    for _ in range(NEWTON_STEPS):
        z = _logit(cols, beta)
        grad_sql = ", ".join(f"sum((y - p) * {cols[j]})" for j in range(k))
        hess_sql = ", ".join(f"sum(p * (1 - p) * {cols[i]} * {cols[j]})" for i in range(k) for j in range(i, k))
        row = con.execute(f"SELECT {grad_sql}, {hess_sql} FROM (SELECT *, 1 / (1 + exp(-({z}))) AS p FROM _wp_train)"
                          ).fetchone()
        grad = [row[j] - (L2 * beta[j] if j else 0.0) for j in range(k)]
        hess = [[0.0] * k for _ in range(k)]
        idx = k
        for i in range(k):
            for j in range(i, k):
                hess[i][j] = hess[j][i] = row[idx]
                idx += 1
        for j in range(1, k):
            hess[j][j] += L2
        step = _solve(hess, grad)
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < TOLERANCE:
            break

    # Back to raw-feature coefficients: b0' = b0 - Σ bj·mean_j/std_j, bj' = bj/std_j
    raw = [beta[0] - sum(beta[j] * mean[j] / std[j] for j in range(1, k))] + [beta[j] / std[j] for j in range(1, k)]
    fit = con.execute(f"""
        SELECT count(*), avg(-(y * ln(greatest(p, 1e-12)) + (1 - y) * ln(greatest(1 - p, 1e-12)))),
               avg(pow(p - y, 2))
        FROM (SELECT y, 1 / (1 + exp(-({_logit(cols, beta)}))) AS p FROM _wp_train)""").fetchone()
    return names, raw, {"n": fit[0], "log_loss": fit[1], "brier": fit[2]}


def build_winprob(con: duckdb.DuckDBPyConnection) -> None:
    """(Re)build `winprob` (per delivery: wp_before, wp_after, wpa for the batting side),
    `winprob_model` (coefficients and fit) and `winprob_calibration`."""
    con.execute(STATE_SQL)
    models = {}
    rows = []
    for innings in (1, 2):
        names, coefs, fit = fit_innings(con, innings)
        models[innings] = coefs
        rows += [(innings, n, c, fit["n"], fit["log_loss"], fit["brier"]) for n, c in zip(names, coefs)]
    con.execute("CREATE OR REPLACE TABLE winprob_model (innings INTEGER, feature VARCHAR, coef DOUBLE, "
                "n BIGINT, log_loss DOUBLE, brier DOUBLE)")
    con.executemany("INSERT INTO winprob_model VALUES (?, ?, ?, ?, ?, ?)", rows)

    wp = {i: "1 / (1 + exp(-(" + _logit([sql for _, sql in FEATURES[i]], models[i]) + ")))" for i in (1, 2)}
    con.execute(f"""
        CREATE OR REPLACE TABLE winprob AS
        WITH before AS (
            SELECT match_id, innings_no, seq, y,
                   CASE WHEN innings_no = 1 THEN {wp[1]}
                        WHEN runs_required <= 0 THEN 1.0
                        ELSE {wp[2]} END AS wp_before
            FROM _wp_state
        ),
        chase_start AS (SELECT match_id, arg_min(wp_before, seq) AS wp FROM before WHERE innings_no = 2 GROUP BY 1),
        after AS (
            SELECT b.*,
                   coalesce(lead(wp_before) OVER (PARTITION BY match_id, innings_no ORDER BY seq),
                            CASE WHEN innings_no = 1 THEN 1 - c.wp ELSE y END) AS wp_after
            FROM before b LEFT JOIN chase_start c USING (match_id)
        )
        SELECT match_id, innings_no, seq, wp_before, wp_after, wp_after - wp_before AS wpa
        FROM after WHERE y IS NOT NULL
    """)

    # Calibration: predicted vs actual, by decile of predicted win probability.
    con.execute("""
        CREATE OR REPLACE TABLE winprob_calibration AS
        SELECT w.innings_no AS innings, least(floor(w.wp_before * 10), 9)::INT AS decile,
               count(*) AS balls, round(avg(w.wp_before), 3) AS predicted, round(avg(s.y), 3) AS actual
        FROM winprob w JOIN _wp_state s USING (match_id, innings_no, seq)
        GROUP BY ALL ORDER BY 1, 2""")
    for t in ("_wp_state", "_wp_train"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
