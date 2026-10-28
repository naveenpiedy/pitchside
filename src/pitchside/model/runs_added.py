"""Runs added: net stats with wickets priced in runs.

Net stats keep runs and dismissals apart (net_runs, outs_saved). Runs added
joins them, pricing each dismissal by the runs the batting side can expect to
lose with it at that point of the innings:

    runs_added (batter) = net_runs + Σ (expected wicket cost − wicket_value × out)
    runs_added (bowler) = runs_saved + Σ (wicket_value × wickets − expected wicket cost)

with expected wicket cost = wicket_value × exp_out (exp_wkts for bowlers),
rescaled per season-over so the wicket part is zero-sum like the net stats.

It is the "Impact" of Himanish Ganjoo's T20 metrics primer (runs on the ball +
the change in projected runs to come, from a Duckworth-Lewis style curve)
rewritten around our own expectations: that Impact expands to (runs −
expected runs) + wicket value × (expected wickets − wickets), and here both
expectations are the net-stat ones (settle, chase pressure, venue...).

Run production R(b, w): runs a side scores from b legal balls left with w
wickets down, per rules era, fitted on first innings played to completion at
full length, in the Duckworth-Lewis form R = Z_w (1 − exp(−g b / Z_w)): an
asymptote Z_w per wickets down and one scoring rate g as balls run out, shared
by every w (least squares). The shared g is what keeps the curves from crossing
where data is thin (0 down with 12 balls left almost never happens). An era's
Z_w is shrunk towards the all-era one (scaled to the era's totals) by ERA_PRIOR
states, so thin cells (9 down) borrow from it, and Z_w may not rise with w. A wicket on a ball costs R(b', w) − R(b', w + 1), b' = balls left
after the ball, scaled by the venue's run factor and the season's totals.

Chases use the same curve: a wicket's cost is what it does to the side's run
production, not to the result (that is WPA).
"""

from __future__ import annotations

import math

import duckdb

from pitchside.model.baselines import era_labels

ERA_PRIOR = 3000     # pseudo-states pulling an era's Z_w towards the all-era one
MAX_BALLS = 120


def _golden(f, lo: float, hi: float, iters: int = 60) -> float:
    """Minimiser of a unimodal f on [lo, hi]."""
    r = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - r * (b - a), a + r * (b - a)
    fc, fd = f(c), f(d)
    for _ in range(iters):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - r * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + r * (b - a)
            fd = f(d)
    return (a + b) / 2


def _r(z: float, g: float, b: int) -> float:
    return z * (1 - math.exp(-g * b / z)) if z > 0 else 0.0


def _fit(cells: dict[int, list[tuple[int, int, float]]]) -> tuple[float, list[float | None], list[int]]:
    """Shared g and Z_w (None without data) minimising Σ n (ȳ − R(b, w))² over every (w, b) cell."""
    def best_z(pts, g):
        def sse(z):
            return sum(n * (y - _r(z, g, b)) ** 2 for b, n, y in pts)
        z = _golden(sse, 1.0, 800.0)
        return z, sse(z)

    def total(g):
        return sum(best_z(pts, g)[1] for pts in cells.values() if pts)

    g = _golden(total, 0.3, 5.0, iters=40)
    zs = [best_z(cells[w], g)[0] if cells.get(w) else None for w in range(10)]
    return g, zs, [sum(n for _, n, _ in cells.get(w, [])) for w in range(10)]


def _curves(rows: list[tuple[int, int, int, int, float]], eras: list[int]) -> dict[int, list[list[float]]]:
    """R[era][w][b] for b in 0..MAX_BALLS, w in 0..10, from (era, w, b, n, mean runs to come).
    An era without data gets the all-era curve."""
    by_era: dict[int, dict[int, list]] = {}
    for era, w, b, n, y in rows:
        by_era.setdefault(era, {}).setdefault(w, []).append((b, n, y))
    pooled: dict[int, list] = {}
    for w in range(10):
        acc: dict[int, list[float]] = {}
        for cells in by_era.values():
            for b, n, y in cells.get(w, []):
                a = acc.setdefault(b, [0, 0.0])
                a[0] += n
                a[1] += n * y
        pooled[w] = [(b, n, s / n) for b, (n, s) in sorted(acc.items())]
    base_g, base_z, _ = _fit(pooled)
    for w in range(10):  # a wickets-down count never seen at all: no runs to come
        base_z[w] = base_z[w] if base_z[w] is not None else 0.0

    curves = {}
    for era in eras:
        cells = by_era.get(era, {})
        g, zs, weight = _fit(cells) if cells else (base_g, [None] * 10, [0] * 10)
        scale = (_r(zs[0], g, MAX_BALLS) / _r(base_z[0], base_g, MAX_BALLS)
                 if zs[0] is not None and base_z[0] else 1.0)
        z = [((weight[w] * zs[w] if zs[w] is not None else 0.0) + ERA_PRIOR * scale * base_z[w])
             / (weight[w] + ERA_PRIOR) for w in range(10)]
        for w in range(1, 10):  # with a shared g, a smaller Z is a lower curve: wickets never add runs
            z[w] = min(z[w], z[w - 1])
        curves[era] = [[_r(z[w], g, b) for b in range(MAX_BALLS + 1)] for w in range(10)]
        curves[era].append([0.0] * (MAX_BALLS + 1))  # all out
    return curves


def build_runs_added(con: duckdb.DuckDBPyConnection, rule_eras: tuple[int, ...] | list[int] = (),
                     venue_aliases: dict[str, str] | None = None) -> None:
    """(Re)build `run_production` (era, balls_left, wickets_down, runs) and add `wkt_value` (runs a
    dismissal on that delivery costs the batting side) to `expected`. Needs `baseline_factors`."""
    era = " + ".join(f"(m.season_year >= {int(y)})::INT" for y in sorted(rule_eras)) or "0"
    con.execute("CREATE OR REPLACE TEMP TABLE _ra_venue_alias (name VARCHAR PRIMARY KEY, canonical VARCHAR)")
    if venue_aliases:
        con.executemany("INSERT INTO _ra_venue_alias VALUES (?, ?)", list(venue_aliases.items()))
    # Complete, full-length first innings: every state's runs to come is observed.
    complete = ("d.innings_no = 1 AND NOT i.is_super_over AND i.allocated_balls = m.overs * m.balls_per_over "
                f"AND m.overs * m.balls_per_over = {MAX_BALLS} AND (i.wickets = 10 OR i.legal_balls = i.allocated_balls)")
    def states(where: str):
        return con.execute(f"""
            WITH s AS (SELECT d.match_id, {era} AS era, d.wickets_before AS w,
                              least(d.balls_remaining, {MAX_BALLS}) AS b, any_value(i.runs - d.score_before) AS y
                       FROM deliveries d JOIN innings i USING (match_id, innings_no) JOIN matches m USING (match_id)
                       WHERE {where} GROUP BY ALL)
            SELECT era, w, b, count(*), avg(y) FROM s WHERE w < 10 GROUP BY ALL""").fetchall()

    # A competition with no complete first innings yet (or a test fixture) falls back to any first innings.
    rows = states(complete) or states("d.innings_no = 1 AND NOT i.is_super_over")
    eras = [r[0] for r in con.execute(f"SELECT DISTINCT {era} FROM matches m").fetchall()]
    curves = _curves(rows, eras)
    con.execute("CREATE OR REPLACE TABLE run_production (era INTEGER, balls_left INTEGER, "
                "wickets_down INTEGER, runs DOUBLE)")
    if curves:
        con.executemany("INSERT INTO run_production VALUES (?, ?, ?, ?)",
                        [(e, b, w, r[w][b]) for e, r in curves.items() for w in range(11) for b in range(MAX_BALLS + 1)])

    era_label = "CASE b.era " + " ".join(f"WHEN {i} THEN '{lab}'" for i, lab in enumerate(era_labels(rule_eras))) + " END"
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _ra_ball AS
        SELECT d.match_id, d.innings_no, d.seq, m.season_year, {era} AS era,
               coalesce(va.canonical, m.venue, 'unknown') AS venue,
               greatest(least(d.balls_remaining - d.is_legal::INT, {MAX_BALLS}), 0) AS b, least(d.wickets_before, 9) AS w,
               d.seq = 1 AND {complete} AS opens_complete
        FROM deliveries d JOIN innings i USING (match_id, innings_no) JOIN matches m USING (match_id)
        LEFT JOIN _ra_venue_alias va ON va.name = m.venue
        WHERE NOT i.is_super_over;

        -- Venue run factor from the net-stat baselines (bowler runs, shrunk), then a per-season
        -- scale so complete first innings average their curve-and-venue projection.
        CREATE OR REPLACE TEMP TABLE _ra_cond AS
        SELECT b.*, coalesce(v.factor, 1) AS venue_factor
        FROM _ra_ball b LEFT JOIN baseline_factors v
          ON v.target = 'bowl_runs' AND v.kind = 'venue' AND v.venue = b.venue AND v.era = {era_label};

        CREATE OR REPLACE TEMP TABLE _ra_season AS
        SELECT c.season_year, sum(i.runs) / nullif(sum(r.runs * c.venue_factor), 0) AS scale
        FROM _ra_cond c JOIN innings i USING (match_id, innings_no)
        JOIN run_production r ON r.era = c.era AND r.balls_left = {MAX_BALLS} AND r.wickets_down = 0
        WHERE c.opens_complete GROUP BY ALL;

        CREATE OR REPLACE TEMP TABLE _ra_exp AS
        SELECT e.*, c.season_year, d.is_wicket::INT AS wkt, d.is_bowler_wicket::INT AS bwkt,
               (r0.runs - r1.runs) * c.venue_factor * coalesce(s.scale, 1) AS wkt_value
        FROM expected e
        JOIN deliveries d USING (match_id, innings_no, seq)
        JOIN _ra_cond c USING (match_id, innings_no, seq)
        LEFT JOIN _ra_season s USING (season_year)
        JOIN run_production r0 ON r0.era = c.era AND r0.balls_left = c.b AND r0.wickets_down = c.w
        JOIN run_production r1 ON r1.era = c.era AND r1.balls_left = c.b AND r1.wickets_down = c.w + 1;

        -- Expected wicket cost per ball. Dismissal chances match actual dismissals on each factor's
        -- cells, but the price varies inside them (balls left within a phase), so the cost is rescaled
        -- per season-over like the net stats: zero-sum there, as an average player should be.
        CREATE OR REPLACE TABLE expected AS
        SELECT x.* EXCLUDE (season_year, wkt, bwkt, exp_wkt_bat, exp_wkt_bowl),
               x.exp_wkt_bat * coalesce(k.bat, 0) AS exp_bat_wkt_runs,
               x.exp_wkt_bowl * coalesce(k.bowl, 0) AS exp_bowl_wkt_runs
        FROM (SELECT *, wkt_value * exp_bat_out AS exp_wkt_bat, wkt_value * exp_bowl_wkt AS exp_wkt_bowl
              FROM _ra_exp) x
        JOIN (SELECT season_year, eff_over,
                     sum(wkt_value * wkt) / nullif(sum(wkt_value * exp_bat_out), 0) AS bat,
                     sum(wkt_value * bwkt) / nullif(sum(wkt_value * exp_bowl_wkt), 0) AS bowl
              FROM _ra_exp GROUP BY ALL) k USING (season_year, eff_over);
    """)
    for t in ("_ra_ball", "_ra_cond", "_ra_season", "_ra_exp", "_ra_venue_alias"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
