"""Impact from win probability (add-on A1): MVP tables and single-match impact.

WPA (win probability added) is in wins: +1.0 = one whole match swung from
certain defeat to certain victory. A batter earns the change in the batting
side's win probability on balls they face; a bowler earns minus the change
on balls they bowl. See model/winprob.py.
"""

from __future__ import annotations

from pitchside.engine import Engine
from pitchside.stats import cache
from pitchside.stats.filters import compile_filters
from pitchside.stats.table import Table


def mvp(engine: Engine, filters: dict | None = None, top: int = 15, min_matches: int = 1) -> Table:
    """Players by total WPA (batting + bowling) under any filters."""
    where, params = compile_filters(filters, engine.resolve_player, engine.resolve_team)

    def compute() -> Table:
        rows = engine.con.execute(f"""
            WITH bat AS (SELECT player_id, match_id, team_name, match_date, wpa, runs, faced, 0 AS balls,
                                0 AS wickets FROM v_bat WHERE {where}),
                 bowl AS (SELECT player_id, match_id, team_name, match_date, wpa, 0, 0, balls, wickets
                          FROM v_bowl WHERE {where}),
                 both_sides AS (SELECT *, 'bat' AS side FROM bat UNION ALL SELECT *, 'bowl' FROM bowl)
            SELECT player_id, arg_max(team_name, match_date) AS team, count(DISTINCT match_id) AS matches,
                   round(sum(wpa) FILTER (side = 'bat'), 2) AS bat_wpa,
                   round(sum(wpa) FILTER (side = 'bowl'), 2) AS bowl_wpa,
                   round(sum(wpa), 2) AS total_wpa,
                   round(sum(wpa) / count(DISTINCT match_id), 3) AS wpa_per_match,
                   sum(runs) AS runs, sum(wickets) AS wickets
            FROM both_sides GROUP BY player_id HAVING count(DISTINCT match_id) >= ?
            ORDER BY total_wpa DESC LIMIT ?""", params + params + [min_matches, top]).fetchall()
        out = [[engine.name(r[0])] + list(r[1:]) for r in rows]
        return Table(["player", "team", "matches", "bat_wpa", "bowl_wpa", "total_wpa", "wpa_per_match", "runs",
                      "wickets"], out, title=f"MVP by win probability added (top {top})",
                     notes=["WPA in wins: the change in the side's win probability on the player's balls; "
                            "batting = balls faced, bowling = balls bowled; fielding isn't credited"])

    return cache.cached("mvp", {"filters": filters or {}, "top": top, "min_matches": min_matches},
                        engine.version_key, compute)


def match_impact(engine: Engine, match_id: str, top: int = 5) -> list[Table]:
    """For one match: win-probability path by over, top contributors, biggest swings."""
    m = engine.con.execute("SELECT team1, team2, result, winner, match_date FROM v_matches WHERE match_id = ?",
                           [match_id]).fetchone()
    if not m:
        raise LookupError(f"No match {match_id!r}")

    def compute() -> list[Table]:
        first = engine.con.execute("SELECT batting_team FROM v_innings WHERE match_id = ? AND innings_no = 1",
                                   [match_id]).fetchone()
        if not first or engine.con.execute("SELECT count(*) FROM v_balls WHERE match_id = ? AND wp_before IS NOT NULL",
                                           [match_id]).fetchone()[0] == 0:
            return [Table(["note"], [["no win probability for this match (no result or no data)"]])]
        team_a = first[0]
        path = engine.con.execute("""
            SELECT innings_no, over_no + 1 AS over, batting_team,
                   arg_max(wp_before + wpa, seq) AS wp_batting_after
            FROM v_balls WHERE match_id = ? GROUP BY ALL ORDER BY 1, 2""", [match_id]).fetchall()
        path_rows = [[inn, over, round(100 * (wp if bt == team_a else 1 - wp), 1)] for inn, over, bt, wp in path]

        contributors = engine.con.execute("""
            WITH x AS (SELECT player_id, team_name, wpa, 'bat' AS side FROM v_bat WHERE match_id = ?
                       UNION ALL SELECT player_id, team_name, wpa, 'bowl' FROM v_bowl WHERE match_id = ?)
            SELECT player_id, any_value(team_name), round(sum(wpa) FILTER (side = 'bat'), 3),
                   round(sum(wpa) FILTER (side = 'bowl'), 3), round(sum(wpa), 3) AS total
            FROM x GROUP BY player_id ORDER BY abs(total) DESC LIMIT ?""", [match_id, match_id, top * 2]).fetchall()
        contrib_rows = [[engine.name(r[0])] + list(r[1:]) for r in contributors]

        swings = engine.con.execute("""
            SELECT innings_no, over_no || '.' || ball_in_over AS ball, batter_id, bowler_id, runs_total,
                   CASE WHEN is_wicket THEN 'W' ELSE '' END AS wicket, batting_team, round(wpa, 3) AS wpa
            FROM v_balls WHERE match_id = ? ORDER BY abs(wpa) DESC LIMIT ?""", [match_id, top]).fetchall()
        swing_rows = [[i, b, engine.name(bt), engine.name(bw), r, w, round(100 * (x if t == team_a else -x), 1)]
                      for i, b, bt, bw, r, w, t, x in swings]

        title = f"{m[0]} v {m[1]}, {m[4]}: {m[3] + ' won' if m[3] else m[2]}"
        return [
            Table(["innings", "over", f"{team_a}_win_pct"], path_rows, title=f"{title} | win probability by over"),
            Table(["player", "team", "bat_wpa", "bowl_wpa", "total_wpa"], contrib_rows,
                  title="Biggest contributors (WPA in wins)"),
            Table(["innings", "ball", "batter", "bowler", "runs", "wicket", f"{team_a}_win_pct_change"], swing_rows,
                  title="Biggest swings"),
        ]

    return cache.cached("match_impact", {"match": match_id, "top": top}, engine.version_key, compute)
