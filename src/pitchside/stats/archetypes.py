"""Player archetypes (add-on A2).

Rule-based, not clustered: every label is explainable from the features shown
next to it, and the same player gets the same label for the same data.

Batters: role from WHERE they bat (entry point, share of death-overs balls),
style from HOW (shrunk net strike rate, balls per innings).
Bowlers: role from WHEN they bowl (phase shares) and type (pace/spin),
quality tag from shrunk net economy.

Thresholds are module constants in the units people talk in (net SR points,
% of balls, runs per over) so they're easy to read and tune.
"""

from __future__ import annotations

from pitchside.engine import Engine
from pitchside.stats import cache
from pitchside.stats.filters import FilterError, compile_filters
from pitchside.stats.splits import aggregate
from pitchside.stats.table import Table

MIN_BATTING_BALLS, MIN_BOWLING_BALLS = 200, 240

# Batting: style by shrunk net SR (runs per 100 balls above an average batter in the same spots).
AGGRESSOR_NET_SR = 8.0
ANCHOR_NET_SR = -5.0
ANCHOR_BALLS_PER_INNINGS = 18.0   # anchors bat long; slow + short is just slow
FINISHER_DEATH_SHARE = 0.40       # share of balls faced in overs 16-20

# Bowling: role by phase shares of balls bowled, tag by shrunk net economy (runs/over saved).
DEATH_SHARE, POWERPLAY_SHARE = 0.35, 0.45
SPIN_POWERPLAY_SHARE, SPIN_DEATH_SHARE = 0.30, 0.20
ECONOMICAL, EXPENSIVE = 0.5, -0.5


def batting_archetype(f: dict) -> str:
    entry, death, tsr, bpi = f["avg_entry_over"], f["death_share"], f["net_sr_shrunk"], f["balls_per_innings"]
    if f["opener_share"] >= 0.5:
        role = "opener"
    elif death >= FINISHER_DEATH_SHARE or entry >= 13:
        role = "finisher"
    elif entry <= 6:
        role = "top order"
    else:
        role = "middle order"

    if tsr >= AGGRESSOR_NET_SR:
        style = {"opener": "Powerplay enforcer", "top order": "Top-order aggressor",
                 "middle order": "Middle-overs aggressor", "finisher": "Finisher"}[role]
    elif tsr <= ANCHOR_NET_SR and bpi >= ANCHOR_BALLS_PER_INNINGS and role != "finisher":
        style = {"opener": "Anchor opener", "top order": "Top-order anchor", "middle order": "Middle-order anchor"}[role]
    elif role == "finisher":
        style = "Lower-order hitter" if tsr > ANCHOR_NET_SR else "Lower-order batter"
    else:
        style = {"opener": "Opener", "top order": "Top-order accumulator",
                 "middle order": "Middle-order accumulator"}[role]
    return style


def bowling_archetype(f: dict) -> str:
    pp, death, kind = f["powerplay_share"], f["death_share"], f["bowl_type"]
    if kind == "spin":
        role = ("Powerplay spinner" if pp >= SPIN_POWERPLAY_SHARE else
                "Death-overs spinner" if death >= SPIN_DEATH_SHARE else "Middle-overs spinner")
    elif kind == "pace":
        if pp >= POWERPLAY_SHARE * 0.67 and death >= DEATH_SHARE * 0.86:
            role = "All-phase seamer"
        elif death >= DEATH_SHARE:
            role = "Death specialist"
        elif pp >= POWERPLAY_SHARE:
            role = "New-ball seamer"
        else:
            role = "Middle-overs seamer"
    else:
        role = "Bowler (type unknown)"
    te = f["net_economy_shrunk"]
    tag = " (economical)" if te is not None and te >= ECONOMICAL else \
        " (expensive)" if te is not None and te <= EXPENSIVE else ""
    return role + tag


def _shares(engine: Engine, role: str, filters: dict | None, min_balls: int) -> dict[str, dict]:
    """Per player: phase shares, entry point, balls per innings, bowling type."""
    src, balls = ("v_bat", "faced") if role == "batting" else ("v_bowl", "balls")
    where, params = compile_filters(filters, engine.resolve_player, engine.resolve_team)
    extra = ("avg(entry_over) FILTER (faced = 1 AND nth_ball = 1) AS avg_entry_over, "
             "avg((entry_balls = 0)::INT) FILTER (faced = 1 AND nth_ball = 1) AS opener_share, "
             if role == "batting" else "any_value(bowl_type) AS bowl_type, ")
    rows = engine.con.execute(f"""
        SELECT player_id, {extra}
               coalesce(sum({balls}) FILTER (phase = 'powerplay'), 0) / sum({balls}) AS powerplay_share,
               coalesce(sum({balls}) FILTER (phase = 'middle'), 0) / sum({balls}) AS middle_share,
               coalesce(sum({balls}) FILTER (phase = 'death'), 0) / sum({balls}) AS death_share,
               sum({balls}) / count(DISTINCT match_id || ':' || innings_no) AS balls_per_innings
        FROM {src} WHERE {where}
        GROUP BY player_id HAVING sum({balls}) >= ?""", params + [min_balls])
    cols = [c[0] for c in rows.description]
    return {engine.name(r[0]): dict(zip(cols, r)) for r in rows.fetchall()}


def archetypes(engine: Engine, role: str = "batting", filters: dict | None = None,
               min_balls: int | None = None, archetype: str | None = None) -> Table:
    """Every qualifying player with an archetype and the features behind it."""
    if role not in ("batting", "bowling"):
        raise FilterError("role must be batting or bowling")
    min_balls = min_balls or (MIN_BATTING_BALLS if role == "batting" else MIN_BOWLING_BALLS)

    def compute() -> Table:
        quality = (["balls", "strike_rate", "net_sr", "net_sr_shrunk"] if role == "batting"
                   else ["balls", "economy", "net_economy", "net_economy_shrunk", "wickets"])
        stats = {r["player"]: r for r in aggregate(engine, role, filters, by=["player"], metrics=quality,
                                                   min_balls=min_balls).records()}
        shares = _shares(engine, role, filters, min_balls)
        rows = []
        for name, s in stats.items():
            f = {**s, **shares.get(name, {})}
            if role == "batting":
                if f.get("avg_entry_over") is None:
                    continue
                label = batting_archetype(f)
                rows.append([name, s["team"], label, f["balls"], round(f["avg_entry_over"], 1),
                             round(100 * f["opener_share"]), round(100 * f["death_share"]),
                             round(f["balls_per_innings"], 1), f["strike_rate"], f["net_sr"], f["net_sr_shrunk"]])
            else:
                label = bowling_archetype(f)
                rows.append([name, s["team"], label, f["balls"], f.get("bowl_type"),
                             round(100 * f["powerplay_share"]), round(100 * f["middle_share"]),
                             round(100 * f["death_share"]), f["economy"], f["net_economy_shrunk"], f["wickets"]])
        if archetype:
            rows = [r for r in rows if archetype.lower() in r[2].lower()]
        rows.sort(key=lambda r: (r[2], -(r[3] or 0)))
        columns = (["player", "team", "archetype", "balls", "avg_entry_over", "opener_pct", "death_pct",
                    "balls_per_innings", "strike_rate", "net_sr", "net_sr_shrunk"] if role == "batting" else
                   ["player", "team", "archetype", "balls", "type", "powerplay_pct", "middle_pct", "death_pct",
                    "economy", "net_economy_shrunk", "wickets"])
        notes = [f"min {min_balls} balls; rules in stats/archetypes.py"]
        if role == "batting":
            notes.append(f"style from net_sr_shrunk: aggressor >= +{AGGRESSOR_NET_SR:g}, anchor <= "
                         f"{ANCHOR_NET_SR:g} with >= {ANCHOR_BALLS_PER_INNINGS:g} balls/innings; finisher: "
                         f">= {FINISHER_DEATH_SHARE:.0%} of balls at the death or arriving from over 13")
        else:
            notes.append(f"tag from net_economy_shrunk: economical >= +{ECONOMICAL}, expensive <= {EXPENSIVE} runs/over")
        title = f"{role.capitalize()} archetypes" + (f" matching {archetype!r}" if archetype else "")
        return Table(columns, rows, title=title, notes=notes)

    return cache.cached("archetypes", {"role": role, "filters": filters or {}, "min_balls": min_balls,
                                       "archetype": archetype}, engine.version_key, compute)


def archetype_summary(engine: Engine, role: str = "batting", filters: dict | None = None,
                      min_balls: int | None = None) -> Table:
    """How many players fall in each archetype, with their typical numbers."""
    t = archetypes(engine, role, filters, min_balls)
    groups: dict[str, list] = {}
    for r in t.records():
        groups.setdefault(r["archetype"], []).append(r)
    key = "net_sr_shrunk" if role == "batting" else "net_economy_shrunk"
    rows = []
    for label, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        vals = [r[key] for r in rs if r[key] is not None]
        best = max(rs, key=lambda r: r[key] if r[key] is not None else float("-inf"))
        rows.append([label, len(rs), round(sum(vals) / len(vals), 2) if vals else None, best["player"]])
    return Table(["archetype", "players", f"avg_{key}", "best"], rows, title=f"{role.capitalize()} archetype summary",
                 notes=t.notes)
