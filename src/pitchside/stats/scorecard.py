"""Match view (F19): scorecard, fall of wickets, partnerships, over progression.

Functions return plain dicts/lists so the same data can back the CLI now and
the MCP tools later; `render_scorecard` turns a summary into compact text.
"""

from __future__ import annotations

import duckdb


def _overs(legal_balls: int, bpo: int = 6) -> str:
    return f"{legal_balls // bpo}.{legal_balls % bpo}" if legal_balls % bpo else str(legal_balls // bpo)


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list) -> list[dict]:
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def dismissal_text(kind: str | None, bowler: str | None, fielders: list[str] | None) -> str:
    if kind is None:
        return "not out"
    f = ", ".join(fielders or [])
    return {
        "caught": f"c {f} b {bowler}",
        "caught and bowled": f"c & b {bowler}",
        "bowled": f"b {bowler}",
        "lbw": f"lbw b {bowler}",
        "stumped": f"st {f} b {bowler}",
        "hit wicket": f"hit wicket b {bowler}",
        "run out": f"run out ({f})" if f else "run out",
    }.get(kind, kind)


def match_summary(con: duckdb.DuckDBPyConnection, match_id: str) -> dict:
    match = _rows(con, "SELECT * FROM matches WHERE match_id = ?", [match_id])
    if not match:
        raise KeyError(f"No match {match_id!r}")
    match = match[0]
    names = dict(con.execute("SELECT player_id, name FROM players").fetchall())
    match["player_of_match"] = [names.get(p, p) for p in match["player_of_match"] or []]
    bpo = match["balls_per_over"]

    innings = []
    for inn in _rows(con, "SELECT * FROM innings WHERE match_id = ? ORDER BY innings_no", [match_id]):
        n = inn["innings_no"]
        batting = _rows(con, """
            SELECT b.position, p.name, b.runs, b.balls, b.fours, b.sixes, b.is_out,
                   b.dismissal, bw.name AS bowler, b.fielders
            FROM batting_innings b
            JOIN players p USING (player_id)
            LEFT JOIN players bw ON bw.player_id = b.dismissed_by
            WHERE b.match_id = ? AND b.innings_no = ?
            ORDER BY b.position""", [match_id, n])
        for b in batting:
            b["how_out"] = dismissal_text(b.pop("dismissal"), b.pop("bowler"), b.pop("fielders"))
            b["strike_rate"] = round(100 * b["runs"] / b["balls"], 1) if b["balls"] else None

        bowling = _rows(con, """
            SELECT p.name, b.balls, b.maidens, b.runs, b.wickets, b.dots, b.wides, b.noballs
            FROM bowling_innings b JOIN players p USING (player_id)
            WHERE b.match_id = ? AND b.innings_no = ?
            ORDER BY b.bowling_order""", [match_id, n])
        for b in bowling:
            b["overs"] = _overs(b["balls"], bpo)
            b["economy"] = round(bpo * b["runs"] / b["balls"], 2) if b["balls"] else None

        fow = _rows(con, """
            SELECT w.wicket_number, w.score_at_fall, p.name, w.legal_balls_at_fall
            FROM wickets w JOIN players p ON p.player_id = w.player_out_id
            WHERE w.match_id = ? AND w.innings_no = ? AND w.counts_as_out
            ORDER BY w.seq""", [match_id, n])
        for w in fow:
            w["over"] = _overs(w.pop("legal_balls_at_fall"), bpo)

        did_not_bat = [r[0] for r in con.execute("""
            SELECT mp.player_name FROM match_players mp
            WHERE mp.match_id = ? AND mp.team = ?
              AND mp.player_id NOT IN (SELECT player_id FROM batting_order WHERE match_id = ? AND innings_no = ?)
            """, [match_id, inn["batting_team"], match_id, n]).fetchall()]

        innings.append({
            "innings_no": n,
            "batting_team": inn["batting_team"],
            "super_over": inn["is_super_over"],
            "total": f"{inn['runs']}/{inn['wickets']} ({_overs(inn['legal_balls'], bpo)} ov)",
            "target": inn["target_runs"],
            "extras": inn["extras"],
            "batting": batting,
            "did_not_bat": did_not_bat,
            "fall_of_wickets": fow,
            "bowling": bowling,
        })
    return {"match": match, "innings": innings}


def partnerships(con: duckdb.DuckDBPyConnection, match_id: str, innings_no: int) -> list[dict]:
    """Partnership per wicket: runs, balls, and each batter's share off the bat."""
    names = dict(con.execute("SELECT player_id, name FROM players").fetchall())
    rows = _rows(con, """
        SELECT wickets_before + 1 AS wicket,
               sum(runs_total)            AS runs,
               count(*) FILTER (is_legal) AS balls,
               list_distinct(flatten(list([batter_id, non_striker_id]))) AS batter_ids
        FROM deliveries WHERE match_id = ? AND innings_no = ?
        GROUP BY wickets_before ORDER BY wickets_before""", [match_id, innings_no])
    for r in rows:
        r["batters"] = [names.get(p, p) for p in r.pop("batter_ids")]
    shares = _rows(con, """
        SELECT wickets_before + 1 AS wicket, p.name, sum(runs_batter) AS runs,
               count(*) FILTER (is_ball_faced) AS balls
        FROM deliveries d JOIN players p ON p.player_id = d.batter_id
        WHERE match_id = ? AND innings_no = ? GROUP BY ALL""", [match_id, innings_no])
    for r in rows:
        r["contributions"] = {s["name"]: f"{s['runs']} ({s['balls']})" for s in shares if s["wicket"] == r["wicket"]}
    return rows


def over_progression(con: duckdb.DuckDBPyConnection, match_id: str, innings_no: int) -> list[dict]:
    return _rows(con, """
        SELECT over_no + 1 AS over, sum(runs_total) AS runs,
               count(*) FILTER (is_wicket) AS wickets,
               sum(sum(runs_total)) OVER (ORDER BY over_no) AS score,
               sum(count(*) FILTER (is_wicket)) OVER (ORDER BY over_no) AS wickets_down
        FROM deliveries WHERE match_id = ? AND innings_no = ?
        GROUP BY over_no ORDER BY over_no""", [match_id, innings_no])


def render_scorecard(summary: dict) -> str:
    m = summary["match"]
    lines = [
        f"{m['team1']} v {m['team2']} | {m['event_name']} {m['season']}"
        + (f" {m['stage']}" if m["stage"] else f" #{m['match_number']}" if m["match_number"] else ""),
        f"{m['match_date']} | {m['venue']}",
        f"Toss: {m['toss_winner']} ({m['toss_decision']})",
    ]
    if m["result"] == "win":
        margin = f"{m['win_by_runs']} runs" if m["win_by_runs"] else f"{m['win_by_wickets']} wickets" if m["win_by_wickets"] else ""
        lines.append(f"Result: {m['winner']} won" + (f" by {margin}" if margin else "") + (f" ({m['method']})" if m["method"] else ""))
    else:
        lines.append(f"Result: {m['result']}" + (f", {m['winner']} won the eliminator" if m["winner"] else ""))
    if m["player_of_match"]:
        lines.append(f"Player of the match: {', '.join(m['player_of_match'])}")

    for inn in summary["innings"]:
        label = "Super over" if inn["super_over"] else f"Innings {inn['innings_no']}"
        lines += ["", f"{label}: {inn['batting_team']} {inn['total']}"]
        for b in inn["batting"]:
            lines.append(f"  {b['name']:<24} {b['how_out']:<34} {b['runs']:>3} ({b['balls']}) {b['fours']}x4 {b['sixes']}x6")
        lines.append(f"  Extras {inn['extras']}")
        if inn["did_not_bat"]:
            lines.append(f"  Did not bat: {', '.join(inn['did_not_bat'])}")
        if inn["fall_of_wickets"]:
            lines.append("  FoW: " + ", ".join(
                f"{w['wicket_number']}-{w['score_at_fall']} ({w['name']}, {w['over']})" for w in inn["fall_of_wickets"]))
        for b in inn["bowling"]:
            lines.append(f"  {b['name']:<24} {b['overs']:>4}-{b['maidens']}-{b['runs']}-{b['wickets']}  econ {b['economy']}")
    return "\n".join(lines)
