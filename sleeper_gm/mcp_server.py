"""MCP server (stdio): lets Claude or any MCP client read your Sleeper Lock-In matchups,
see what the manager would do and why, and (only when you allow writes) run it.

  claude mcp add sleeper-gm -- sleeper-gm mcp

Read tools never change anything. `run_manager` writes to Sleeper only when called with
live=true AND `sleeper-gm on` has been set; otherwise it is a dry run.
"""
from __future__ import annotations

import numpy as np

from . import api, config

INSTRUCTIONS = (
    "Tools for a Sleeper fantasy basketball league in Lock-In mode (each starter counts one game a week; "
    "a finished game can be locked until that player's next tip). Start with list_leagues, then matchup. "
    "A pending game's 'open_value' is what keeping that slot open is expected to be worth; the manager "
    "locks when the game beats it, adjusted for the chance of winning the matchup. Never call run_manager "
    "with live=true unless the user asked for changes to be made."
)

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
try:
    from mcp.types import ToolAnnotations
except ImportError:
    ToolAnnotations = None

try:
    from importlib.metadata import version as _version
    VERSION = _version("sleeper-gm")
except Exception:
    VERSION = "0"
try:
    server = _Server("sleeper-gm", instructions=INSTRUCTIONS, version=VERSION)
except TypeError:  # mcp 1.x FastMCP has no version argument
    server = _Server("sleeper-gm", instructions=INSTRUCTIONS)


def _ann(read_only):
    if ToolAnnotations is None:
        return {}
    return {"annotations": ToolAnnotations(readOnlyHint=read_only, destructiveHint=not read_only, openWorldHint=True)}


def _ctx(league_id, live=False):
    from .cli import discover, ensure_history, store
    from .cycle import Ctx
    cfg = config.load()
    found = discover(cfg)
    if not found:
        raise RuntimeError("no in-season Lock-In league found for this Sleeper login")
    pick = next((x for x in found if not league_id or str(x[0]) == str(league_id)), None)
    if pick is None:
        raise RuntimeError(f"league {league_id} is not one of yours: {[x[0] for x in found]}")
    st = store()
    ensure_history(st, str(api.rest("/v1/state/nba")["season"]))
    return Ctx(cfg, pick[0], pick[1], live, st), cfg


@server.tool(**_ann(True))
def list_leagues() -> list:
    """Your Sleeper NBA leagues this season and whether the manager handles them."""
    from .cli import discover
    return [{"league_id": lid, "roster_id": rid, "name": name} for lid, rid, name in discover(config.load())]


@server.tool(**_ann(True))
def matchup(league_id: str = "") -> dict:
    """This week's matchup: win chance, both teams' points and projections, every starting slot
    (locked, finished and lockable with its open value and the call, playing, open) and what the
    manager would do right now (locks, passes, today's lineup, IR fix, best roster move)."""
    from .cycle import preview
    ctx, _ = _ctx(league_id)
    snap, _wk, _me = preview(ctx)
    return snap


@server.tool(**_ann(True))
def player_outlook(name: str, league_id: str = "") -> dict:
    """Projection for a player's remaining games this week (chance he plays, mean, 10th/90th
    percentile in your league's scoring), injury status and expected return, rest-of-season value."""
    from . import moves
    from .cycle import load
    ctx, _ = _ctx(league_id)
    wk, _me, _opp = load(ctx)
    q = name.lower().strip()
    hits = [p for p, x in wk.players.items() if x.get("team") and q in f"{x.get('first_name', '')} {x.get('last_name', '')}".lower()]
    if not hits:
        return {"error": f"no active player matching {name!r}"}
    p = hits[0]
    games = []
    for g in wk.player_games(p):
        if g.start_ms <= wk.now_ms:
            continue
        d = wk.model.dist(p, {"game_id": g.game_id, "date": g.date, "start_ms": g.start_ms,
                              "team": wk.team_of(p), "opp": g.team_opp(wk.team_of(p))})
        s = np.asarray(d.samples, dtype=float)
        games.append({"date": g.date, "opp": g.team_opp(wk.team_of(p)), "p_play": round(float(d.p_play), 2),
                      "mean": round(float(s.mean()), 1), "p10": round(float(np.percentile(s, 10)), 1),
                      "p90": round(float(np.percentile(s, 90)), 1)})
    owner = next((rid for rid, r in wk.rosters.items() if p in (r.get("players") or [])), None)
    inj = wk.injuries.get(p) or {}
    return {"player": wk.name(p), "team": wk.team_of(p), "positions": wk.players[p].get("fantasy_positions"),
            "rostered_by": "you" if owner == ctx.rid else ("free agent" if owner is None else f"roster {owner}"),
            "injury": wk.players[p].get("injury_status"), "return_date": inj.get("return_date"),
            "injury_note": inj.get("short"), "rest_of_season_per_game": round(moves.ros(wk, p), 1),
            "games_this_week": games, "also_matched": [wk.name(x) for x in hits[1:5]]}


@server.tool(**_ann(True))
def decision_log(league_id: str = "", week: int = 0) -> list:
    """Every manager run this week (or `week`): win chance at the time, locks, passes and actions."""
    from . import web
    ctx, _ = _ctx(league_id)
    return web.runs(ctx.league_id, week or None) or []


@server.tool(**_ann(True))
def week_review(week: int, league_id: str = "") -> dict:
    """A finished week: result, points vs Sleeper's hindsight maximum, league rank on that, and
    how well the win chances held up."""
    from .review import review
    ctx, _ = _ctx(league_id)
    text, row = review(ctx.league_id, ctx.rid, int(week))
    return {"summary": text, **row}


@server.tool(**_ann(False))
def run_manager(league_id: str = "", live: bool = False) -> dict:
    """Run one full manager cycle (IR fixes, roster moves, locks, lineup). Changes Sleeper only if
    live=true AND writes are switched on with `sleeper-gm on`; otherwise reports what it would do."""
    from .cycle import run_league
    ctx, cfg = _ctx(league_id)
    ctx.live = bool(live and cfg.get("live"))
    ctx.writer.live = ctx.live
    snap = run_league(ctx)
    return {"live": ctx.live, "p_win": snap["p_win"], "acted": snap["acted"]}


def main():
    server.run("stdio")
