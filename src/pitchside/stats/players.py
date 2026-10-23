"""Player-facing analyses: splits, profile (F14), leaderboards (F13), comparison (F15/F16).

All are thin layers over `splits.aggregate`, cached by (args, data versions).
"""

from __future__ import annotations

from pitchside.engine import Engine
from pitchside.stats import cache
from pitchside.stats.filters import FilterError
from pitchside.stats.splits import DEFAULT_METRICS, RATE_METRICS, NET_METRICS, aggregate
from pitchside.stats.table import Table

# Profile sections per role: (label, dimensions).
PROFILE_SECTIONS = {
    "batting": [("by season", ["season"]), ("by phase", ["phase"]), ("vs pace / spin", ["bowl_type"]),
                ("vs bowling subtype", ["bowl_subtype"]), ("by position", ["position"]),
                ("by entry point", ["entry_phase"]),
                ("setting / chasing", ["setting_chasing"])],
    "bowling": [("by season", ["season"]), ("by phase", ["phase"]),
                ("vs left / right-handers", ["batting_hand"]), ("setting / chasing", ["setting_chasing"])],
}
BOWLING_ONLY_METRICS = {"wickets", "economy", "wides", "noballs", "runs_saved", "exp_economy",
                        "net_economy", "exp_wickets", "net_wickets",
                        "economy_shrunk", "net_economy_shrunk", "net_wickets_shrunk"}
NET_SECTIONS = [("net stats by season", ["season"]), ("net stats by phase", ["phase"])]
MIN_ROLE_BALLS = 30  # below this, a role gets only its overall line


def _args(**kw) -> dict:
    return {k: v for k, v in kw.items() if v not in (None, [], {}, ())}


def splits(engine: Engine, player: str, role: str = "batting", by: list[str] | None = None,
           filters: dict | None = None, metrics: list[str] | None = None) -> Table:
    pid = engine.resolve_player(player)
    by = by or ["season"]
    title = f"{engine.name(pid)} {role} by {', '.join(by)}"
    return cache.cached("splits", _args(pid=pid, role=role, by=by, filters=filters, metrics=metrics),
                        engine.version_key,
                        lambda: aggregate(engine, role, filters, by=by, players=[pid], metrics=metrics, title=title))


def profile(engine: Engine, player: str, filters: dict | None = None) -> list[Table]:
    pid = engine.resolve_player(player)
    name = engine.name(pid)

    def compute() -> list[Table]:
        tables = []
        for role in ("batting", "bowling"):
            overall = aggregate(engine, role, filters, players=[pid], title=f"{name}: {role} overall")
            balls = overall.records()[0]["balls"] if overall.rows else 0
            if not balls:
                continue
            tables.append(overall)
            if balls < MIN_ROLE_BALLS:
                overall.notes.append(f"breakdowns skipped below {MIN_ROLE_BALLS} balls")
                continue
            label = player_archetype(engine, name, role, filters)
            if label:
                overall.notes.append(f"archetype: {label}")
            for label, by in PROFILE_SECTIONS[role]:
                tables.append(aggregate(engine, role, filters, by=by, players=[pid], title=f"{name}: {role} {label}"))
            for label, by in NET_SECTIONS:
                tables.append(aggregate(engine, role, filters, by=by, players=[pid], metrics=NET_METRICS[role],
                                        title=f"{name}: {role} {label}"))
        return tables

    return cache.cached("profile", _args(pid=pid, filters=filters), engine.version_key, compute)


def leaderboard(engine: Engine, metric: str, role: str | None = None, filters: dict | None = None,
                min_balls: int | None = None, top: int = 15, per: str | None = None) -> Table:
    """Top players by a metric. Rate metrics default to a 60-ball minimum."""
    role = role or ("bowling" if metric in BOWLING_ONLY_METRICS else "batting")
    if min_balls is None:
        # Shrunk metrics already discount small samples, so they need a lower bar.
        min_balls = 30 if metric.endswith("_shrunk") else 60 if metric in RATE_METRICS else 1
    title = f"Top {top} by {role} {metric}" + (f" per {per}" if per else "") + f" (min {min_balls} balls)"

    def compute() -> Table:
        is_net = metric.startswith(("net_", "exp_")) or metric.endswith("_saved")
        base = NET_METRICS[role] if is_net else DEFAULT_METRICS[role]
        metrics = base if metric in base else base + [metric]
        return aggregate(engine, role, filters, by=["player"], metrics=metrics, min_balls=min_balls,
                         order_by=metric, limit=top, per=per, title=title)

    return cache.cached("leaderboard", _args(metric=metric, role=role, filters=filters, min_balls=min_balls,
                                             top=top, per=per), engine.version_key, compute)


def compare(engine: Engine, players: list[str], role: str = "batting", filters: dict | None = None,
            by: list[str] | None = None, metrics: list[str] | None = None) -> Table:
    """Players side by side under the same filters, optionally split by dimensions."""
    if len(players) < 2:
        raise FilterError("compare needs at least two players")
    pids = [engine.resolve_player(p) for p in players]
    by = list(by or [])
    title = f"{role.capitalize()} comparison: " + " vs ".join(engine.name(p) for p in pids)

    def compute() -> Table:
        t = aggregate(engine, role, filters, by=["player"] + by, players=pids, metrics=metrics, title=title)
        # Keep the requested player order, then the split order within each player.
        order = {engine.name(p): i for i, p in enumerate(pids)}
        t.rows.sort(key=lambda r: order.get(r[0], 99))
        if "team" not in by:  # drop the latest-team column: clutter in a comparison
            i = t.columns.index("team")
            t.columns.pop(i)
            for r in t.rows:
                r.pop(i)
        return t

    return cache.cached("compare", _args(pids=pids, role=role, filters=filters, by=by, metrics=metrics),
                        engine.version_key, compute)


def net_stats(engine: Engine, player: str, role: str = "batting", by: list[str] | None = None,
               filters: dict | None = None) -> Table:
    """A player's net stats (actual, expected, difference), split by any dimensions."""
    return splits(engine, player, role, by or [], filters, NET_METRICS[role])


def player_archetype(engine: Engine, name: str, role: str, filters: dict | None = None) -> str | None:
    """The player's archetype under these filters, or None below the archetype ball minimum."""
    from pitchside.stats.archetypes import archetypes

    for r in archetypes(engine, role, filters).records():
        if r["player"] == name:
            return r["archetype"]
    return None
