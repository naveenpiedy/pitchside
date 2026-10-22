"""Turn one Cricsheet match JSON into rows for the core tables.

Everything that depends on ball order (scores before each ball, batting
positions, the batter's running score) is derived here in a single pass, so the
database never has to reconstruct innings state with window functions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

# Dismissals credited to the bowler.
BOWLER_WICKETS = frozenset(
    {"bowled", "caught", "caught and bowled", "lbw", "stumped", "hit wicket"}
)
# Dismissal kinds that don't end an innings as "out".
NOT_OUT_KINDS = frozenset({"retired hurt", "retired not out"})


@dataclass
class MatchRows:
    """Rows for every core table, as lists of dicts keyed by column name."""

    matches: list[dict] = field(default_factory=list)
    match_players: list[dict] = field(default_factory=list)
    innings: list[dict] = field(default_factory=list)
    deliveries: list[dict] = field(default_factory=list)
    wickets: list[dict] = field(default_factory=list)
    batting_order: list[dict] = field(default_factory=list)
    replacements: list[dict] = field(default_factory=list)
    players: dict[str, str] = field(default_factory=dict)  # id -> name

    def extend(self, other: MatchRows) -> None:
        for name in (
            "matches", "match_players", "innings", "deliveries",
            "wickets", "batting_order", "replacements",
        ):
            getattr(self, name).extend(getattr(other, name))
        self.players.update(other.players)


def phase_of(over_no: int, overs: int, pp_end: int | None = None) -> str:
    """Phase of a 0-based over in an innings of `overs` overs.

    T20: powerplay 1-6, middle 7-15, death 16-20 (1-based). Shortened innings
    keep the same proportions; `pp_end` (overs in the powerplay, as recorded
    by Cricsheet) overrides the powerplay length when known.
    """
    if overs == 50:
        default_pp, death_start = 10, 40
    else:
        default_pp, death_start = round(overs * 0.3), overs - round(overs / 4)
    if over_no < (pp_end or default_pp):
        return "powerplay"
    if over_no < death_start:
        return "middle"
    return "death"


def overs_to_balls(overs: float, balls_per_over: int) -> int:
    """Convert an overs figure such as 16.3 into legal balls."""
    whole = int(overs)
    part = round((overs - whole) * 10)
    return whole * balls_per_over + part


def parse_outcome(outcome: dict) -> dict:
    by = outcome.get("by", {})
    if "winner" in outcome:
        result = "win"
        winner = outcome["winner"]
    else:
        result = outcome.get("result", "unknown")
        winner = outcome.get("eliminator") or outcome.get("bowl_out")
    return {
        "result": result,
        "winner": winner,
        "win_by_runs": by.get("runs"),
        "win_by_wickets": by.get("wickets"),
        "method": outcome.get("method"),
    }


def parse_match(match_id: str, data: dict, competition: str, source_file: str | None = None) -> MatchRows:
    info = data["info"]
    meta = data.get("meta", {})
    people: dict[str, str] = info.get("registry", {}).get("people", {})

    def pid(name: str) -> str:
        return people.get(name) or f"name:{name}"

    out = MatchRows()
    overs = int(info.get("overs") or 20)
    bpo = int(info.get("balls_per_over") or 6)
    event = info.get("event", {})
    teams = info["teams"]

    out.matches.append(
        {
            "match_id": match_id,
            "competition": competition,
            "event_name": event.get("name"),
            "season": str(info["season"]),
            "season_year": None,
            "match_date": date.fromisoformat(info["dates"][0]),
            "match_number": event.get("match_number"),
            "stage": event.get("stage"),
            "match_type": info.get("match_type"),
            "gender": info.get("gender"),
            "overs": overs,
            "balls_per_over": bpo,
            "venue": info.get("venue"),
            "city": info.get("city"),
            "team1": teams[0],
            "team2": teams[1],
            "toss_winner": info.get("toss", {}).get("winner"),
            "toss_decision": info.get("toss", {}).get("decision"),
            **parse_outcome(info.get("outcome", {})),
            "player_of_match": [pid(p) for p in info.get("player_of_match", [])],
            "data_version": meta.get("data_version"),
            "revision": int(meta.get("revision", 1)),
            "source_file": source_file,
        }
    )

    for team, names in info.get("players", {}).items():
        for name in names:
            out.match_players.append(
                {"match_id": match_id, "team": team, "player_id": pid(name), "player_name": name}
            )
            out.players[pid(name)] = name

    # A match shortened before the start (rain, no DLS) gives both sides the chase's reduced overs.
    reduced_overs = None
    if not info.get("outcome", {}).get("method"):
        for inn in data.get("innings", []):
            t = (inn.get("target") or {}).get("overs")
            if t is not None and not inn.get("super_over") and float(t) < overs:
                reduced_overs = float(t)

    for innings_no, inn in enumerate(data.get("innings", []), start=1):
        _parse_innings(out, match_id, innings_no, inn, teams, overs, bpo, pid, reduced_overs)

    return out


def powerplay_overs(inn: dict) -> int | None:
    """Overs (rounded up) in the innings' mandatory powerplay, if Cricsheet records it."""
    for pp in inn.get("powerplays", []):
        if pp.get("type") == "mandatory" and pp.get("to") is not None:
            return int(float(pp["to"])) + 1
    return None


def _parse_innings(out: MatchRows, match_id: str, innings_no: int, inn: dict,
                   teams: list[str], overs: int, bpo: int, pid, reduced_overs: float | None = None) -> None:
    batting_team = inn["team"]
    bowling_team = teams[1] if teams[0] == batting_team else teams[0]
    is_super_over = bool(inn.get("super_over"))
    target = inn.get("target") or {}
    target_runs = target.get("runs")
    target_overs = float(target["overs"]) if target.get("overs") is not None else None

    if is_super_over:
        innings_overs = 1.0
    elif target_overs is not None:
        innings_overs = target_overs
    elif reduced_overs is not None:
        innings_overs = reduced_overs
    else:
        innings_overs = float(overs)
    allocation = overs_to_balls(innings_overs, bpo)
    phase_overs = math.ceil(innings_overs)
    pp_end = powerplay_overs(inn)

    score = wickets = legal = extras_total = seq = 0
    position: dict[str, int] = {}
    bat_runs: dict[str, int] = {}
    bat_balls: dict[str, int] = {}

    def arrive(player_id: str) -> None:
        if player_id not in position:
            position[player_id] = len(position) + 1
            out.batting_order.append(
                {
                    "match_id": match_id, "innings_no": innings_no, "player_id": player_id,
                    "position": position[player_id], "entry_legal_balls": legal,
                    "entry_score": score, "entry_wickets": wickets,
                }
            )

    for over in inn.get("overs", []):
        over_no = over["over"]
        for ball_in_over, d in enumerate(over["deliveries"], start=1):
            seq += 1
            batter, non_striker, bowler = pid(d["batter"]), pid(d["non_striker"]), pid(d["bowler"])
            arrive(batter)
            arrive(non_striker)

            runs = d["runs"]
            ex = d.get("extras", {})
            wides, noballs = ex.get("wides", 0), ex.get("noballs", 0)
            byes, legbyes, penalty = ex.get("byes", 0), ex.get("legbyes", 0), ex.get("penalty", 0)
            is_legal = not wides and not noballs
            boundary = not runs.get("non_boundary", False)

            ball_wickets = d.get("wickets", [])
            counted = [w for w in ball_wickets if w["kind"] not in NOT_OUT_KINDS]
            row = {
                "match_id": match_id, "innings_no": innings_no, "seq": seq,
                "over_no": over_no, "ball_in_over": ball_in_over,
                "is_legal": is_legal, "legal_balls_before": legal,
                "phase": phase_of(over_no, phase_overs, pp_end),
                "batter_id": batter, "non_striker_id": non_striker, "bowler_id": bowler,
                "batting_position": position[batter],
                "non_striker_position": position[non_striker],
                "runs_batter": runs["batter"], "runs_extras": runs["extras"], "runs_total": runs["total"],
                "wides": wides, "noballs": noballs, "byes": byes, "legbyes": legbyes, "penalty": penalty,
                "bowler_runs": runs["total"] - byes - legbyes - penalty,
                "is_ball_faced": not wides,
                "is_four": boundary and runs["batter"] == 4,
                "is_six": boundary and runs["batter"] == 6,
                "is_wicket": bool(counted),
                "is_bowler_wicket": any(w["kind"] in BOWLER_WICKETS for w in counted),
                "is_batter_out": any(pid(w["player_out"]) == batter for w in counted),
                "out_player_id": pid(counted[0]["player_out"]) if counted else None,
                "score_before": score, "wickets_before": wickets,
                "batter_runs_before": bat_runs.get(batter, 0),
                "batter_balls_before": bat_balls.get(batter, 0),
                "balls_remaining": max(allocation - legal, 0),
                "runs_required": target_runs - score if target_runs is not None else None,
            }
            out.deliveries.append(row)

            # Advance the innings state past this ball.
            score += runs["total"]
            extras_total += runs["extras"]
            bat_runs[batter] = bat_runs.get(batter, 0) + runs["batter"]
            if not wides:
                bat_balls[batter] = bat_balls.get(batter, 0) + 1
            if is_legal:
                legal += 1

            for w in ball_wickets:
                counts = w["kind"] not in NOT_OUT_KINDS
                if counts:
                    wickets += 1
                fielders = [f.get("name") for f in w.get("fielders", []) if f.get("name")]
                out.wickets.append(
                    {
                        "match_id": match_id, "innings_no": innings_no, "seq": seq,
                        "player_out_id": pid(w["player_out"]), "kind": w["kind"],
                        "bowler_id": bowler, "bowler_credit": w["kind"] in BOWLER_WICKETS,
                        "counts_as_out": counts, "fielders": fielders,
                        "fielder_ids": [pid(f) for f in fielders],
                        "score_at_fall": score, "wicket_number": wickets if counts else None,
                        "legal_balls_at_fall": legal,
                    }
                )

            for kind, items in d.get("replacements", {}).items():
                for r in items:
                    out.replacements.append(
                        {
                            "match_id": match_id, "innings_no": innings_no, "seq": seq, "kind": kind,
                            "team": r.get("team"),
                            "player_in_id": pid(r["in"]) if r.get("in") else None,
                            "player_out_id": pid(r["out"]) if r.get("out") else None,
                            "reason": r.get("reason"), "role": r.get("role"),
                        }
                    )

    out.innings.append(
        {
            "match_id": match_id, "innings_no": innings_no,
            "batting_team": batting_team, "bowling_team": bowling_team,
            "is_super_over": is_super_over,
            "target_runs": target_runs, "target_overs": target_overs,
            "allocated_balls": allocation,
            "runs": score, "wickets": wickets, "legal_balls": legal, "extras": extras_total,
            "absent_hurt": [pid(p) for p in inn.get("absent_hurt", [])],
        }
    )
