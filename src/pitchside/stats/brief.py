"""Pre-match brief (add-on A6): everything worth knowing before Team A v Team B, in one call.

Built from the existing analyses; nothing new is modelled here. "Recent" means
the last RECENT_SEASONS seasons either team played unless a `season` filter is
given. A team's squad is everyone who batted or bowled for it in its last
SQUAD_MATCHES matches (Cricsheet has no announced squads).
"""

from __future__ import annotations

from pitchside.engine import Engine
from pitchside.stats import cache
from pitchside.stats import teams as tm
from pitchside.stats.splits import aggregate
from pitchside.stats.table import Table

RECENT_SEASONS = 3
SQUAD_MATCHES = 10
FORM_MATCHES = 5
KEY_PLAYERS = 4
MATCHUP_MIN_BALLS = 12


def _squad(engine: Engine, team: str) -> list[str]:
    """Player ids who batted or bowled for `team` in its last SQUAD_MATCHES matches."""
    rows = engine.con.execute("""
        WITH recent AS (SELECT match_id FROM v_results WHERE team = ? ORDER BY match_date DESC LIMIT ?)
        SELECT DISTINCT player_id FROM v_bat WHERE team = ? AND match_id IN (SELECT match_id FROM recent)
        UNION
        SELECT DISTINCT player_id FROM v_bowl WHERE team = ? AND match_id IN (SELECT match_id FROM recent)
    """, [team, SQUAD_MATCHES, team, team]).fetchall()
    return [r[0] for r in rows]


def _form(engine: Engine, team: str) -> Table:
    rows = engine.con.execute("""
        SELECT r.match_date, r.opposition, r.venue, r.outcome,
               coalesce(m.win_by_runs || ' runs', m.win_by_wickets || ' wkts', m.result) AS margin
        FROM v_results r JOIN v_matches m USING (match_id)
        WHERE r.team = ? ORDER BY r.match_date DESC LIMIT ?""", [team, FORM_MATCHES]).fetchall()
    return Table(["date", "opposition", "venue", "result", "margin"], [list(r) for r in rows],
                 title=f"{team}: last {FORM_MATCHES} matches")


def _phase_profile(engine: Engine, team: str, recent: dict) -> Table:
    """Team batting run rate and bowling economy by phase, against the league average."""
    league = {r["phase"]: r["run_rate"] for r in
              aggregate(engine, "team_batting", recent, by=["phase"], metrics=["run_rate"]).records()}
    bat = {r["phase"]: r for r in aggregate(engine, "team_batting", {**recent, "team": team}, by=["phase"],
                                            metrics=["balls", "run_rate", "average"]).records()}
    bowl = {r["phase"]: r for r in aggregate(engine, "team_bowling", {**recent, "team": team}, by=["phase"],
                                             metrics=["balls", "run_rate", "average"]).records()}
    rows = []
    for phase in ("powerplay", "middle", "death"):
        b, w, lg = bat.get(phase, {}), bowl.get(phase, {}), league.get(phase)
        diff = lambda x: round(x - lg, 2) if x is not None and lg is not None else None
        rows.append([phase, b.get("run_rate"), diff(b.get("run_rate")), w.get("run_rate"),
                     diff(w.get("run_rate")), lg])
    return Table(["phase", "bat_run_rate", "bat_vs_league", "bowl_economy", "bowl_vs_league", "league_rate"], rows,
                 title=f"{team}: phase profile (recent)",
                 notes=["vs_league: + is above the league's run rate (good batting, bad bowling)"])


def _key_players(engine: Engine, team: str, squad: list[str], recent: dict) -> list[Table]:
    if not squad:
        return []
    # Ranked by value above an average player (volume), not WPA: over a few seasons WPA is too noisy
    # (a batter's clutch moments swing it more than his overall run-making).
    bat = aggregate(engine, "batting", recent, by=["player"], players=squad, min_balls=30,
                    metrics=["innings", "runs", "strike_rate", "true_runs", "true_sr_shrunk", "wpa"],
                    order_by="true_runs", limit=KEY_PLAYERS, title=f"{team}: key batters (recent, by true runs)")
    bowl = aggregate(engine, "bowling", recent, by=["player"], players=squad, min_balls=30,
                     metrics=["innings", "wickets", "economy", "runs_saved", "true_economy_shrunk", "wpa"],
                     order_by="runs_saved", limit=KEY_PLAYERS, title=f"{team}: key bowlers (recent, by runs saved)")
    return [_drop_team(bat), _drop_team(bowl)]


def _drop_team(t: Table) -> Table:
    """Squad tables are already per team; the 'latest team' column only confuses (e.g. traded players)."""
    if "team" in t.columns:
        i = t.columns.index("team")
        t.columns.pop(i)
        for r in t.rows:
            r.pop(i)
    return t


def _style_matchup(engine: Engine, bat_team: str, bat_squad: list[str], bowl_team: str, bowl_squad: list[str],
                   recent: dict) -> Table:
    """The batting side's top batters vs pace and spin, next to the bowling side's pace/spin mix."""
    mix = {r["bowl_type"]: r["balls"] for r in aggregate(
        engine, "bowling", recent, by=["bowl_type"], players=bowl_squad, metrics=["balls"]).records()}
    total = sum(mix.values()) or 1
    spin_share = round(100 * mix.get("spin", 0) / total)
    top = [r["player"] for r in aggregate(engine, "batting", recent, by=["player"], players=bat_squad,
                                          metrics=["balls"], order_by="balls", limit=6).records()]
    ids = [engine.resolve_player(n) for n in top]
    split = {(r["player"], r["bowl_type"]): r for r in aggregate(
        engine, "batting", recent, by=["player", "bowl_type"], players=ids,
        metrics=["balls", "strike_rate", "true_sr_shrunk"]).records()}
    rows = []
    for name in top:
        p, s = split.get((name, "pace"), {}), split.get((name, "spin"), {})
        rows.append([name, p.get("balls"), p.get("strike_rate"), p.get("true_sr_shrunk"),
                     s.get("balls"), s.get("strike_rate"), s.get("true_sr_shrunk")])
    return Table(["batter", "balls_v_pace", "sr_v_pace", "true_sr_v_pace", "balls_v_spin", "sr_v_spin",
                  "true_sr_v_spin"], rows,
                 title=f"{bat_team} batters v {bowl_team}'s attack ({spin_share}% of its recent balls are spin)",
                 notes=["true_sr_*: shrunk true strike rate (runs per 100 balls above an average batter)"])


def _head_to_heads(engine: Engine, bat_squad: list[str], bowl_squad: list[str], title: str) -> Table:
    """All-time batter v bowler records between the two squads, biggest samples first."""
    if not bat_squad or not bowl_squad:
        return Table(["batter"], [], title=title)
    t = aggregate(engine, "batting", {"bowler": bowl_squad}, by=["player", "bowler"], players=bat_squad,
                  metrics=["balls", "runs", "outs", "strike_rate", "true_sr"], min_balls=MATCHUP_MIN_BALLS,
                  order_by="balls", limit=8, title=title)
    t.notes.append(f"all seasons; pairs with at least {MATCHUP_MIN_BALLS} balls")
    return _drop_team(t)


def prematch_brief(engine: Engine, team_a: str, team_b: str, venue: str | None = None,
                   filters: dict | None = None) -> list[Table]:
    a, b = engine.resolve_team(team_a), engine.resolve_team(team_b)
    ground = engine.resolve_venue(venue) if venue else None
    filters = dict(filters or {})
    if "season" not in filters:
        last = engine.con.execute("SELECT max(season_year) FROM v_results WHERE team IN (?, ?)", [a, b]).fetchone()[0]
        filters["season"] = f"{last - RECENT_SEASONS + 1}+" if last else None
    recent = {k: v for k, v in filters.items() if v is not None}

    def compute() -> list[Table]:
        squad_a, squad_b = _squad(engine, a), _squad(engine, b)
        tables = [
            tm.results(engine, {"team": a, "opposition": b}, title=f"{a} v {b}: all-time ({a}'s record)"),
            tm.results(engine, {**recent, "team": a, "opposition": b}, by=["season"],
                       title=f"{a} v {b}: recent seasons ({a}'s record)"),
            _form(engine, a), _form(engine, b),
        ]
        if ground:
            tables.append(tm.venue_summary(engine, {**recent, "venue": ground}, title=f"{ground}: recent"))
            tables.append(tm.results(engine, {"team": [a, b], "venue": ground}, by=["team"],
                                     title=f"Both teams at {ground} (all-time)"))
        tables += [_phase_profile(engine, a, recent), _phase_profile(engine, b, recent)]
        tables += _key_players(engine, a, squad_a, recent) + _key_players(engine, b, squad_b, recent)
        tables += [
            _style_matchup(engine, a, squad_a, b, squad_b, recent),
            _style_matchup(engine, b, squad_b, a, squad_a, recent),
            _head_to_heads(engine, squad_a, squad_b, f"{a} batters v {b} bowlers (head to head)"),
            _head_to_heads(engine, squad_b, squad_a, f"{b} batters v {a} bowlers (head to head)"),
        ]
        return tables

    return cache.cached("prematch_brief", {"a": a, "b": b, "venue": ground, "filters": recent},
                        engine.version_key, compute)
