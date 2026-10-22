"""The shared filter grammar.

A filter set is a dict of {key: value}, the same from the CLI (`-f season=2022-2025`)
and from MCP tool calls. Keys are validated against FILTERS, values may be:

- a scalar:            2024, "death", "Wankhede"
- a comma list:        "2022,2024", "OB,LB"            (any of)
- an integer range:    "2022-2025", "16-20", "2022+"   (inclusive; "+" = and above)
- a date range:        "2024-01-01..2024-06-30"
- a JSON list / dict:  [2022, 2024], {"min": 16, "max": 20}

Everything compiles to a parameterised WHERE clause over v_bat / v_bowl,
which share column names, so one filter means the same thing for both roles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


class FilterError(ValueError):
    pass


@dataclass(frozen=True)
class Spec:
    column: str
    kind: str  # 'int', 'enum', 'text', 'exact', 'date', 'player'
    help: str
    choices: tuple[str, ...] = ()
    aliases: tuple[tuple[str, str], ...] = ()


ENTRY_PHASE_SQL = ("CASE WHEN entry_balls = 0 THEN 'opener' WHEN entry_over <= 6 THEN 'powerplay' "
                   "WHEN entry_over <= 15 THEN 'middle' ELSE 'death' END")

FILTERS: dict[str, Spec] = {
    "season": Spec("season_year", "int", "season year(s), e.g. 2024, 2022-2025, 2020+"),
    "competition": Spec("competition", "exact", "competition key, e.g. ipl"),
    "phase": Spec("phase", "enum", "powerplay / middle / death", ("powerplay", "middle", "death"),
                  (("pp", "powerplay"),)),
    "over": Spec("over_no + 1", "int", "over number 1-20, e.g. 16-20"),
    "innings": Spec("innings_no", "enum", "setting (1st innings) / chasing (2nd)", ("1", "2"),
                    (("setting", "1"), ("first", "1"), ("chasing", "2"), ("second", "2"))),
    "team": Spec("team", "team", "own team: name, old name or code (RCB, KXIP); renamed franchises count as one"),
    "opposition": Spec("opposition", "team", "opposing team: name, old name or code"),
    "venue": Spec("venue", "text", "ground name (contains; grounds have one canonical name)"),
    "city": Spec("city", "text", "city (contains)"),
    "stage": Spec("stage", "enum", "league / playoffs", ("league", "playoffs")),
    "bowl_type": Spec("bowl_type", "enum", "bowler type: pace / spin / unknown", ("pace", "spin", "unknown"),
                      (("seam", "pace"), ("fast", "pace"))),
    "bowl_subtype": Spec("bowl_subtype", "enum",
                         "RF RFM RMF RM LF LFM LMF LM (pace), OB LB SLA LWS (spin)",
                         ("RF", "RFM", "RMF", "RM", "LF", "LFM", "LMF", "LM", "OB", "LB", "SLA", "LWS")),
    "bowling_arm": Spec("bowling_arm", "enum", "left / right", ("left", "right")),
    "batting_hand": Spec("batting_hand", "enum", "batter's hand: left / right", ("left", "right"),
                         (("lhb", "left"), ("rhb", "right"))),
    "position": Spec("position", "int", "batting position 1-11, e.g. 1-3"),
    "nth_ball": Spec("nth_ball", "int", "the batter's nth ball of the innings, e.g. 1-5 (first five balls)"),
    "wickets_down": Spec("wickets_before", "int", "team wickets down before the ball"),
    "entry_phase": Spec(ENTRY_PHASE_SQL, "enum", "when the batter walked in: opener / powerplay / middle / death",
                        ("opener", "powerplay", "middle", "death")),
    "entry_over": Spec("entry_over", "int", "over the batter walked in (1-20; openers too), e.g. 7-10"),
    "entry_wickets": Spec("entry_wickets", "int", "team wickets down when the batter walked in"),
    "date": Spec("match_date", "date", "date or range YYYY-MM-DD..YYYY-MM-DD"),
    "match": Spec("match_id", "exact", "Cricsheet match id(s)"),
    "bowler": Spec("bowler_id", "player", "facing a specific bowler (name or id)"),
    "batter": Spec("batter_id", "player", "bowling to a specific batter (name or id)"),
}


def describe() -> str:
    return "\n".join(f"{k:<13} {s.help}" for k, s in FILTERS.items())


def _items(value) -> list:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [value]


def _int_condition(col: str, value, params: list) -> str:
    if isinstance(value, dict):
        parts = []
        if value.get("min") is not None:
            parts.append(f"{col} >= ?"); params.append(int(value["min"]))
        if value.get("max") is not None:
            parts.append(f"{col} <= ?"); params.append(int(value["max"]))
        return " AND ".join(parts) or "TRUE"
    ors = []
    for item in _items(value):
        s = str(item)
        if m := re.fullmatch(r"(\d+)\s*-\s*(\d+)", s):
            ors.append(f"{col} BETWEEN ? AND ?"); params += [int(m[1]), int(m[2])]
        elif m := re.fullmatch(r"(\d+)\+", s):
            ors.append(f"{col} >= ?"); params.append(int(m[1]))
        elif re.fullmatch(r"\d+", s):
            ors.append(f"{col} = ?"); params.append(int(s))
        else:
            raise FilterError(f"Bad number or range {s!r} (use 2024, 2022-2025, 2020+)")
    return "(" + " OR ".join(ors) + ")"


def _date_condition(col: str, value, params: list) -> str:
    s = str(value)
    try:
        if ".." in s:
            lo, hi = s.split("..", 1)
            parts = []
            if lo:
                parts.append(f"{col} >= ?"); params.append(date.fromisoformat(lo))
            if hi:
                parts.append(f"{col} <= ?"); params.append(date.fromisoformat(hi))
            return " AND ".join(parts) or "TRUE"
        params.append(date.fromisoformat(s))
        return f"{col} = ?"
    except ValueError as e:
        raise FilterError(f"Bad date {s!r} (use YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD)") from e


def compile_filters(filters: dict | None, resolve_player=None, resolve_team=None) -> tuple[str, list]:
    """Return (sql, params) for a WHERE clause.

    `resolve_player` maps a player name to an id, `resolve_team` a team name or
    code to its canonical franchise name.
    """
    clauses, params = [], []
    for key, value in (filters or {}).items():
        if value is None or value == "" or value == []:
            continue
        spec = FILTERS.get(key)
        if spec is None:
            raise FilterError(f"Unknown filter {key!r}. Valid filters:\n{describe()}")
        if spec.kind == "int":
            clauses.append(_int_condition(spec.column, value, params))
        elif spec.kind == "date":
            clauses.append(_date_condition(spec.column, value, params))
        elif spec.kind == "enum":
            aliases = dict(spec.aliases)
            vals = []
            for item in _items(value):
                v = aliases.get(str(item).lower(), str(item))
                match = next((c for c in spec.choices if c.lower() == v.lower()), None)
                if match is None:
                    raise FilterError(f"Bad {key} {item!r}; choose from {', '.join(spec.choices)}")
                vals.append(int(match) if spec.column == "innings_no" else match)
            clauses.append(f"{spec.column} IN ({', '.join('?' * len(vals))})")
            params += vals
        elif spec.kind == "text":
            items = _items(value)
            clauses.append("(" + " OR ".join(f"{spec.column} ILIKE ?" for _ in items) + ")")
            params += [f"%{i}%" for i in items]
        elif spec.kind == "exact":
            items = [str(i) for i in _items(value)]
            clauses.append(f"{spec.column} IN ({', '.join('?' * len(items))})")
            params += items
        elif spec.kind in ("player", "team"):
            resolve = resolve_player if spec.kind == "player" else resolve_team
            if resolve is None:
                raise FilterError(f"Filter {key!r} needs {spec.kind} resolution")
            ids = [resolve(str(i)) for i in _items(value)]
            clauses.append(f"{spec.column} IN ({', '.join('?' * len(ids))})")
            params += ids
    return (" AND ".join(clauses) or "TRUE"), params


def parse_cli_filters(pairs: list[str] | None) -> dict:
    """['season=2022-2025', 'phase=death'] -> {'season': '2022-2025', 'phase': 'death'}"""
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise FilterError(f"Filters look like key=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out
