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

End of the chase: a smooth logistic curve can't see that one ball is lumpy
(needing 2 off 1 is mostly a dot or a single, not "2 per ball against a par of
1.4"), so over the last MARKOV_BALLS it is blended into a ball-by-ball Markov
model solved by backward induction. Per-ball outcome chances (dot, runs, extras,
wicket) come from chase balls in that window, per wickets-in-hand band and
runs-required-per-ball band, shrunk from all eras to the rules era to the
rules era at that ground. The Markov value is used alone from BLEND_FULL
balls left down, the logistic one from MARKOV_BALLS up, linearly in between.

End of the first innings: the handover to 1 - the chase's start would otherwise put
the gap between the two models on the last ball (the first-innings curve drifts
upwards over the last overs, so that ball averaged -0.06). Over the last
PROJECT_BALLS the logistic value is blended (alone from PROJECT_FULL down) into
1 - the chase model's starting value averaged over the projected target: runs
still to come ~ normal, with mean and spread per (balls left, wickets band).

Leverage (Tom Tango's idea, brought to T20 by Himanish Ganjoo's metrics primer):
how much one ball can swing the game, = the batting side's win probability if
the ball goes for six minus if it takes a wicket, both from the same blended
model as wp_before. `leverage_index` divides it by the competition's average
ball, so 1 is a typical ball and 3 one that can swing three times as much.
"""

from __future__ import annotations

import math
from collections import defaultdict

import duckdb
import pyarrow as pa

from pitchside.model.baselines import era_labels

L2 = 1.0           # ridge penalty on standardised coefficients
NEWTON_STEPS = 25
TOLERANCE = 1e-8

MARKOV_BALLS = 24        # last four overs of a chase are solved ball by ball
BLEND_FULL = 12          # at or below this many balls left the Markov value is used alone
GLOBAL_PRIOR = 50        # pseudo-balls pulling a (wickets, pressure) cell towards all window balls
ERA_PRIOR = 100          # ... a rules-era cell towards the all-era cell
VENUE_PRIOR = 240        # ... a ground's cell towards its era (~10 chases' last four overs; home sides
                         # bowl half of them, so a light prior would absorb team skill)
WICKET_BANDS = ["1-2", "3-4", "5-6", "7+"]               # wickets in hand
# Balls left: in a live chase dots fall and boundaries rise as balls run out, and on the
# last ball the batters always run (IPL: 1 dot in 62 live last balls vs ~20% four overs out).
BALLS_EDGES = (1, 6, 12)
BALLS_BANDS = ["1", "2-6", "7-12", "13-24"]
PRESSURE_EDGES = (1.0, 1.5, 2.0, 3.0)                   # runs required per ball
PRESSURE_BANDS = ["<=1", "1-1.5", "1.5-2", "2-3", ">3"]

PROJECT_BALLS = 24       # over the last four overs of a first innings the value hands over to the chase model
PROJECT_FULL = 12        # at or below this many balls left the projected value is used alone
PROJECT_PRIOR = 50       # pseudo-innings pulling a (balls left, wickets band) cell towards its balls-left cell
# Five-point Gauss-Hermite rule: E[f(X)] for X ~ N(0, 1) as Σ w·f(z) (probabilists' nodes, weights sum to 1).
HERMITE = [(0.0, 8 / 15)] + [(s * z, w) for z, w in ((1.3556261799742659, 0.2220759220056126),
                                                       (2.8569700138728056, 0.011257411327720691)) for s in (1, -1)]
WICKET_BAND_SQL = f"least(greatest({{w}} - 1, 0) // 2, {len(WICKET_BANDS) - 1})"

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
       max(d.score_before + d.runs_total) OVER (PARTITION BY d.match_id, d.innings_no)::DOUBLE AS final_score,
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


def wicket_band(wkts_in_hand: int) -> int:
    return min(max(wkts_in_hand - 1, 0) // 2, len(WICKET_BANDS) - 1)


def pressure_band(runs_required: int, balls_left: int) -> int:
    return sum(runs_required > e * balls_left for e in PRESSURE_EDGES)


def balls_band(balls_left: int) -> int:
    return sum(balls_left > e for e in BALLS_EDGES)


# Chase balls in the Markov window with their outcome, coded the way the solver steps:
# legal balls cost a ball, wides / no-balls don't; extras' runs are grouped so the state space stays small.
OUTCOME_SQL = f"""
SELECT {{era}} AS era, coalesce(va.canonical, m.venue, 'unknown') AS venue,
       least(greatest(10 - d.wickets_before - 1, 0) // 2, {len(WICKET_BANDS) - 1}) AS wb,
       {' + '.join(f'(d.runs_required > {e} * d.balls_remaining)::INT' for e in PRESSURE_EDGES)} AS pb,
       {' + '.join(f'(d.balls_remaining > {e})::INT' for e in BALLS_EDGES)} AS bb,
       d.is_legal AS legal,
       CASE WHEN d.is_legal AND d.is_wicket THEN least(d.runs_total, 2)
            WHEN d.is_legal THEN least(d.runs_total, 6)
            WHEN d.runs_total <= 1 THEN 1 WHEN d.runs_total <= 3 THEN 2
            WHEN d.runs_total <= 5 THEN 5 ELSE 7 END AS runs,
       d.is_wicket AS wicket, count(*) AS n
FROM deliveries d JOIN innings i USING (match_id, innings_no) JOIN matches m USING (match_id)
LEFT JOIN _wp_venue_alias va ON va.name = m.venue
WHERE d.innings_no = 2 AND NOT i.is_super_over AND d.runs_required > 0 AND d.wickets_before < 10
  AND d.balls_remaining BETWEEN 1 AND {MARKOV_BALLS}
GROUP BY ALL
"""


CELLS = [(wb, pb, bb) for wb in range(len(WICKET_BANDS)) for pb in range(len(PRESSURE_BANDS))
         for bb in range(len(BALLS_BANDS))]


def outcome_distributions(rows) -> tuple[dict, dict]:
    """Shrunk outcome chances from rows of (era, venue, wb, pb, bb, legal, runs, wicket, n), cell = (wb, pb, bb).
    Chain: all window balls -> (pressure, balls left) -> cell -> rules era -> rules era at the ground.
    Returns ({(era, *cell): {outcome: p}}, {(era, venue, *cell): {outcome: p}})."""
    def counter():
        return defaultdict(lambda: defaultdict(float))
    total, situation, cell, era_cell, venue_cell = defaultdict(float), counter(), counter(), counter(), counter()
    for era, venue, wb, pb, bb, legal, runs, wicket, n in rows:
        o = (bool(legal), int(runs), bool(wicket))
        total[o] += n
        situation[(pb, bb)][o] += n
        cell[(wb, pb, bb)][o] += n
        era_cell[(era, wb, pb, bb)][o] += n
        venue_cell[(era, venue, wb, pb, bb)][o] += n

    def shrink(counts, parent, prior):
        n = sum(counts.values())
        return {o: (counts.get(o, 0.0) + prior * p) / (n + prior) for o, p in parent.items()}

    grand = sum(total.values())
    if not grand:
        return {}, {}
    overall = {o: n / grand for o, n in total.items()}
    by_situation = {(pb, bb): shrink(situation[(pb, bb)], overall, GLOBAL_PRIOR) for _, pb, bb in CELLS}
    by_cell = {k: shrink(cell[k], by_situation[k[1:]], GLOBAL_PRIOR) for k in CELLS}
    eras = {k[0] for k in era_cell}
    by_era = {(era, *k): shrink(era_cell[(era, *k)], by_cell[k], ERA_PRIOR) for era in eras for k in CELLS}
    grounds = {k[:2] for k in venue_cell}
    by_venue = {(era, venue, *k): shrink(venue_cell[(era, venue, *k)], by_era[(era, *k)], VENUE_PRIOR)
                for era, venue in grounds for k in CELLS}
    return by_era, by_venue


def solve_chase(probs, max_balls: int):
    """Backward induction over chase states (balls left, wickets in hand, runs required).

    `probs(wickets_in_hand, runs_required, balls_left)` returns [(legal, runs, wicket, p)].
    Returns value(balls_left, wickets_in_hand, runs_required): the batting side's chance of winning,
    a tie counting half. Needing more than 6 a ball plus 8 is treated as lost (the true value is negligible)."""
    cap = [6 * b + 8 for b in range(max_balls + 1)]
    table: list[list[list[float]]] = [[[] for _ in range(11)] for _ in range(max_balls + 1)]

    def value(b: int, w: int, r: int) -> float:
        if r <= 0:
            return 1.0
        if b <= 0 or w <= 0:
            return 0.5 if r == 1 else 0.0
        if r > cap[b]:
            return 0.0
        return table[b][w][r]

    for b in range(1, max_balls + 1):
        for w in range(1, 11):
            row = table[b][w]
            row.append(1.0)  # r = 0
            for r in range(1, cap[b] + 1):
                v = 0.0
                for legal, runs, wicket, p in probs(w, r, b):
                    # Same-b moves (wides, no-balls) always lower r, and wickets lower w, so they're solved already.
                    v += p * value(b - legal, w - wicket, r - runs)
                row.append(v)
    return value


def build_markov(con: duckdb.DuckDBPyConnection, rule_eras, venue_aliases) -> None:
    """Markov chase values for every window ball (`_wp_markov`) and the era-level outcome
    chances behind them (`winprob_outcomes`)."""
    con.execute("CREATE OR REPLACE TEMP TABLE _wp_venue_alias (name VARCHAR PRIMARY KEY, canonical VARCHAR)")
    if venue_aliases:
        con.executemany("INSERT INTO _wp_venue_alias VALUES (?, ?)", list(venue_aliases.items()))
    era = " + ".join(f"(m.season_year >= {int(y)})::INT" for y in sorted(rule_eras)) or "0"
    by_era, by_venue = outcome_distributions(con.execute(OUTCOME_SQL.format(era=era)).fetchall())

    states = con.execute(f"""
        SELECT d.match_id, d.innings_no, d.seq, {era} AS era, coalesce(va.canonical, m.venue, 'unknown') AS venue,
               d.balls_remaining, 10 - d.wickets_before, d.runs_required
        FROM deliveries d JOIN innings i USING (match_id, innings_no) JOIN matches m USING (match_id)
        LEFT JOIN _wp_venue_alias va ON va.name = m.venue
        WHERE d.innings_no = 2 AND NOT i.is_super_over AND d.runs_required > 0 AND d.wickets_before < 10
          AND d.balls_remaining BETWEEN 1 AND {MARKOV_BALLS}""").fetchall()
    solvers = {}
    out = {"match_id": [], "innings_no": [], "seq": [], "wp_markov": [], "wp_markov_six": [], "wp_markov_wicket": []}
    for match_id, innings_no, seq, e, venue, b, w, r in states:
        if (e, venue) not in solvers:
            cells = {k: [(int(o[0]), o[1], int(o[2]), p)
                         for o, p in (by_venue.get((e, venue, *k)) or by_era[(e, *k)]).items()] for k in CELLS}
            solvers[(e, venue)] = solve_chase(
                lambda w_, r_, b_, c=cells: c[(wicket_band(w_), pressure_band(r_, b_), balls_band(b_))], MARKOV_BALLS)
        out["match_id"].append(match_id)
        out["innings_no"].append(innings_no)
        out["seq"].append(seq)
        value = solvers[(e, venue)]
        out["wp_markov"].append(value(b, w, r))
        out["wp_markov_six"].append(value(b - 1, w, r - 6))       # for leverage
        out["wp_markov_wicket"].append(value(b - 1, w - 1, r))
    schema = pa.schema([("match_id", pa.string()), ("innings_no", pa.int32()), ("seq", pa.int32()),
                        ("wp_markov", pa.float64()), ("wp_markov_six", pa.float64()),
                        ("wp_markov_wicket", pa.float64())])
    con.register("_wp_markov_arrow", pa.table(out, schema=schema))
    con.execute("CREATE OR REPLACE TEMP TABLE _wp_markov AS SELECT * FROM _wp_markov_arrow")
    con.unregister("_wp_markov_arrow")

    labels = era_labels(rule_eras)
    con.execute("CREATE OR REPLACE TABLE winprob_outcomes (era VARCHAR, wickets_in_hand VARCHAR, "
                "runs_per_ball VARCHAR, balls_left VARCHAR, legal BOOLEAN, runs INTEGER, wicket BOOLEAN, p DOUBLE)")
    outcome_rows = [(labels[e] if e < len(labels) else str(e), WICKET_BANDS[wb], PRESSURE_BANDS[pb], BALLS_BANDS[bb],
                     *o, p) for (e, wb, pb, bb), dist in sorted(by_era.items()) for o, p in sorted(dist.items())]
    if outcome_rows:
        con.executemany("INSERT INTO winprob_outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?)", outcome_rows)
    con.execute("DROP TABLE IF EXISTS _wp_venue_alias")


def build_projection(con: duckdb.DuckDBPyConnection) -> None:
    """Runs still to come in a first innings, in units of the season's runs per ball, per (balls left,
    wickets-in-hand band) over the last PROJECT_BALLS: mean and spread, each cell shrunk towards its
    balls-left cell (`_wp_project`, and `winprob_projection` with band labels)."""
    p = PROJECT_PRIOR
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _wp_project AS
        WITH obs AS (
            SELECT balls_left::INT AS balls_left, {WICKET_BAND_SQL.format(w='wkts_in_hand::INT')} AS wb,
                   (final_score - score) / rpb AS rest
            FROM _wp_state WHERE innings_no = 1 AND y IS NOT NULL AND balls_left BETWEEN 1 AND {PROJECT_BALLS}),
        parent AS (SELECT balls_left, avg(rest) AS m1, avg(rest * rest) AS m2 FROM obs GROUP BY 1),
        cell AS (SELECT balls_left, wb, count(*) AS n, sum(rest) AS s1, sum(rest * rest) AS s2 FROM obs GROUP BY 1, 2),
        shrunk AS (
            SELECT p.balls_left, b.wb::INT AS wb, coalesce(c.n, 0) AS n,
                   (coalesce(c.s1, 0) + {p} * p.m1) / (coalesce(c.n, 0) + {p}) AS mu,
                   (coalesce(c.s2, 0) + {p} * p.m2) / (coalesce(c.n, 0) + {p}) AS m2
            FROM parent p CROSS JOIN range({len(WICKET_BANDS)}) b(wb)
            LEFT JOIN cell c ON c.balls_left = p.balls_left AND c.wb = b.wb)
        SELECT balls_left, wb, n, mu, sqrt(greatest(m2 - mu * mu, 0)) AS sd FROM shrunk""")
    con.execute(f"""
        CREATE OR REPLACE TABLE winprob_projection AS
        SELECT balls_left, list_extract({WICKET_BANDS!r}, wb + 1) AS wickets_in_hand, n AS innings,
               round(mu, 3) AS mean_rest, round(sd, 3) AS sd_rest
        FROM _wp_project ORDER BY balls_left, wb""")


def _projected_sql(src: str, keys: str, wp2: str, nodes: str) -> str:
    """1 - the chase model's starting value, averaged over the target a first innings in state `src`
    is heading for (rows per `keys`, column wp_proj)."""
    return f"""
        SELECT {keys}, sum(w * (1 - {wp2})) AS wp_proj
        FROM (SELECT {', '.join('s.' + k.strip() for k in keys.split(','))}, n.w, s.rpb, 10.0::DOUBLE AS wkts_in_hand,
                     s.allocation AS balls_left, s.score + 1 + s.rpb * greatest(p.mu + p.sd * n.z, 0) AS runs_required
              FROM {src} s
              JOIN _wp_project p ON p.balls_left = s.balls_left
                                AND p.wb = {WICKET_BAND_SQL.format(w='s.wkts_in_hand::INT')}
              CROSS JOIN ({nodes}) n
              WHERE s.innings_no = 1)
        GROUP BY ALL"""


def build_winprob(con: duckdb.DuckDBPyConnection, rule_eras: tuple[int, ...] | list[int] = (),
                  venue_aliases: dict[str, str] | None = None) -> None:
    """(Re)build `winprob` (per delivery: wp_before, wp_after, wpa for the batting side),
    `winprob_model` (coefficients and fit), `winprob_outcomes` (end-of-chase ball outcome chances),
    `winprob_calibration` and `winprob_endgame` (calibration over the last overs of chases)."""
    con.execute(STATE_SQL)
    build_markov(con, rule_eras, venue_aliases)
    build_projection(con)
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
    markov_weight = f"least(greatest(({MARKOV_BALLS} - balls_left) / {MARKOV_BALLS - BLEND_FULL}, 0), 1)"
    project_weight = f"least(greatest(({PROJECT_BALLS} - balls_left) / {PROJECT_BALLS - PROJECT_FULL}, 0), 1)"
    nodes = " UNION ALL ".join(f"SELECT {z!r}::DOUBLE AS z, {w!r}::DOUBLE AS w" for z, w in HERMITE)
    con.execute(f"""
        CREATE OR REPLACE TABLE winprob AS
        WITH projected AS (
            -- 1 - the chase model's starting value, averaged over the target this innings is heading for
            {_projected_sql('_wp_state', 'match_id, innings_no, seq', wp[2], nodes)}
        ),
        before AS (
            SELECT match_id, innings_no, seq, y,
                   CASE WHEN innings_no = 1 AND pr.wp_proj IS NULL THEN {wp[1]}
                        WHEN innings_no = 1 THEN {project_weight} * pr.wp_proj + (1 - {project_weight}) * {wp[1]}
                        WHEN runs_required <= 0 THEN 1.0
                        WHEN mk.wp_markov IS NULL THEN {wp[2]}
                        ELSE {markov_weight} * mk.wp_markov + (1 - {markov_weight}) * {wp[2]} END AS wp_before
            FROM _wp_state LEFT JOIN _wp_markov mk USING (match_id, innings_no, seq)
            LEFT JOIN projected pr USING (match_id, innings_no, seq)
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

    build_leverage(con, wp, nodes, markov_weight, project_weight)

    # Calibration: predicted vs actual, by decile of predicted win probability.
    con.execute("""
        CREATE OR REPLACE TABLE winprob_calibration AS
        SELECT w.innings_no AS innings, least(floor(w.wp_before * 10), 9)::INT AS decile,
               count(*) AS balls, round(avg(w.wp_before), 3) AS predicted, round(avg(s.y), 3) AS actual
        FROM winprob w JOIN _wp_state s USING (match_id, innings_no, seq)
        GROUP BY ALL ORDER BY 1, 2""")
    # Deciles hide the last overs (few balls, big swings), so check them on their own.
    pressure = " + ".join(f"(s.runs_required > {e} * s.balls_left)::INT" for e in PRESSURE_EDGES)
    con.execute(f"""
        CREATE OR REPLACE TABLE winprob_endgame AS
        SELECT CASE WHEN s.balls_left = 1 THEN '1' WHEN s.balls_left <= 6 THEN '2-6' ELSE '7-12' END AS balls_left,
               CASE {pressure} {' '.join(f"WHEN {i} THEN '{lab}'" for i, lab in enumerate(PRESSURE_BANDS))} END
                   AS runs_per_ball,
               count(*) AS balls, round(avg(w.wp_before), 3) AS predicted, round(avg(s.y), 3) AS actual
        FROM winprob w JOIN _wp_state s USING (match_id, innings_no, seq)
        WHERE s.innings_no = 2 AND s.runs_required > 0 AND s.balls_left BETWEEN 1 AND {BLEND_FULL}
        GROUP BY ALL ORDER BY 1, 2""")
    # The first innings' last ball absorbs the gap between its value and the chase's start: mean ~0 if they agree.
    con.execute("""
        CREATE OR REPLACE TABLE winprob_handover AS
        WITH last AS (SELECT match_id, arg_max(wp_before, seq) AS before, arg_max(wp_after, seq) AS after,
                             arg_max(wpa, seq) AS wpa
                      FROM winprob WHERE innings_no = 1 GROUP BY 1)
        SELECT m.season_year, count(*) AS innings, round(avg(l.wpa), 3) AS last_ball_wpa,
               round(avg(abs(l.wpa)), 3) AS last_ball_abs_wpa, round(avg(l.before), 3) AS predicted_before,
               round(avg(l.after), 3) AS handed_over, round(avg(s.y), 3) AS actual
        FROM last l JOIN matches m USING (match_id)
        JOIN (SELECT DISTINCT match_id, y FROM _wp_state WHERE innings_no = 1) s USING (match_id)
        GROUP BY ALL ORDER BY 1""")
    for t in ("_wp_state", "_wp_train", "_wp_markov", "_wp_project", "_wp_hyp"):
        con.execute(f"DROP TABLE IF EXISTS {t}")


def build_leverage(con: duckdb.DuckDBPyConnection, wp: dict[int, str], nodes: str,
                   markov_weight: str, project_weight: str) -> None:
    """Add `leverage` (win probability after a six minus after a wicket, for the batting side) and
    `leverage_index` (leverage / the competition's mean) to `winprob`. The hypothetical states go
    through the same blend as wp_before; an innings they end is valued like the real ending."""
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _wp_hyp AS
        SELECT s.match_id, s.innings_no, s.seq, k.kind, s.rpb, s.allocation, s.y,
               s.score + CASE k.kind WHEN 'six' THEN 6 ELSE 0 END AS score,
               s.wkts_in_hand - CASE k.kind WHEN 'wicket' THEN 1 ELSE 0 END AS wkts_in_hand,
               s.balls_left - 1 AS balls_left,
               s.runs_required - CASE k.kind WHEN 'six' THEN 6 ELSE 0 END AS runs_required,
               CASE k.kind WHEN 'six' THEN mk.wp_markov_six ELSE mk.wp_markov_wicket END AS wp_markov
        FROM _wp_state s
        CROSS JOIN (SELECT 'six' AS kind UNION ALL SELECT 'wicket') k
        LEFT JOIN _wp_markov mk USING (match_id, innings_no, seq)
        WHERE s.y IS NOT NULL AND s.balls_left >= 1 AND s.wkts_in_hand >= 1""")
    keys = "match_id, innings_no, seq, kind"
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _wp_lev AS
        WITH projected AS ({_projected_sql('_wp_hyp', keys, wp[2], nodes)}),
        -- a first innings the outcome would end: hand over at the chase's start against score + 1
        ended AS (SELECT {keys}, 1 - {wp[2]} AS wp_end
                  FROM (SELECT {keys}, rpb, 10.0::DOUBLE AS wkts_in_hand, allocation AS balls_left,
                               score + 1 AS runs_required
                        FROM _wp_hyp WHERE innings_no = 1 AND (balls_left <= 0 OR wkts_in_hand <= 0))),
        valued AS (
            SELECT h.match_id, h.innings_no, h.seq, h.kind,
                   CASE WHEN h.innings_no = 1 AND e.wp_end IS NOT NULL THEN e.wp_end
                        WHEN h.innings_no = 1 AND pr.wp_proj IS NULL THEN {wp[1]}
                        WHEN h.innings_no = 1 THEN {project_weight} * pr.wp_proj + (1 - {project_weight}) * {wp[1]}
                        WHEN h.runs_required <= 0 THEN 1.0
                        WHEN h.balls_left <= 0 OR h.wkts_in_hand <= 0 THEN CASE WHEN h.runs_required = 1 THEN 0.5 ELSE 0.0 END
                        WHEN h.wp_markov IS NULL THEN {wp[2]}
                        ELSE {markov_weight} * h.wp_markov + (1 - {markov_weight}) * {wp[2]} END AS wp
            FROM _wp_hyp h
            LEFT JOIN projected pr USING ({keys})
            LEFT JOIN ended e USING ({keys}))
        SELECT match_id, innings_no, seq,
               greatest(max(wp) FILTER (kind = 'six') - max(wp) FILTER (kind = 'wicket'), 0) AS leverage
        FROM valued GROUP BY ALL""")
    con.execute("""
        CREATE OR REPLACE TABLE winprob AS
        SELECT w.*, coalesce(l.leverage, 0) AS leverage,
               -- every ball equal (a flat model) is index 1, not undefined
               coalesce(coalesce(l.leverage, 0) / nullif(avg(l.leverage) OVER (), 0), 1) AS leverage_index
        FROM winprob w LEFT JOIN _wp_lev l USING (match_id, innings_no, seq)""")
    con.execute("DROP TABLE IF EXISTS _wp_lev")
