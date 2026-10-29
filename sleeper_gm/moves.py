"""Roster moves: IR hygiene, filling empty spots, and add/drop swaps, scored by the week's
simulated win probability plus rest-of-season value. Every function returns ONE move:
the runner executes it, reloads the real roster, and asks again, so no move is ever
judged against a roster that a previous move already changed."""
from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np

from .policy import Draw, decide, make_plan

IR_FLAGS = {"Out": "reserve_allow_out", "Sus": "reserve_allow_sus", "Suspended": "reserve_allow_sus",
            "DNR": "reserve_allow_dnr", "DTD": "reserve_allow_dtd", "D2D": "reserve_allow_dtd",
            "Questionable": "reserve_allow_dtd", "GTD": "reserve_allow_dtd",
            "Doubtful": "reserve_allow_doubtful", "NA": "reserve_allow_na", "COV": "reserve_allow_cov"}
KAPPA = 0.004       # P(win) points per fantasy point per game of rest-of-season value
SWAP_EDGE = 0.03    # a swap must be worth 3 points of win probability (or the ROS equivalent)
DOMINANT_EDGE = 0.015  # ...unless the add is better this week AND rest of season, so less can be wrong


def ir_eligible(league, status):
    if status in ("IR", "PUP"):
        return True
    flag = IR_FLAGS.get(status or "")
    return bool(flag and league["settings"].get(flag))


@dataclass
class Move:
    kind: str            # "reserve" | "drop_then_reserve" | "add" | "swap" | "needs_you"
    add: str = None
    drop: str = None
    reserve: list = None
    score: float = 0.0
    why: str = ""


def roster_limits(league):
    """(active spots: starters + bench, IR spots)."""
    active = sum(1 for p in league["roster_positions"] if p not in ("IR", "TAXI"))
    return active, int(league["settings"].get("reserve_slots") or 0)


def ros(wk, pid):
    """Talent and role per game, ignoring a current injury (ranks who to protect)."""
    return float(wk.model.ros_value(pid, wk.now_ms))


def swap_edge(week_gain, ros_gain):
    """Score an add/drop must clear: lower when the add wins on both counts."""
    return DOMINANT_EDGE if week_gain > 0 and ros_gain > 0 else SWAP_EDGE


def keep_value(wk, pid, horizon=4):
    """What he is worth to keep or add: per-game value, less the share of the rest of the
    regular season he is expected to miss (from his injury's return date)."""
    left = max(horizon, int(wk.league["settings"].get("playoff_week_start") or 22) - int(wk.week))
    avail = float(wk.model.availability_weeks(pid, wk.now_ms, horizon_weeks=horizon))
    return ros(wk, pid) * (1 - (1 - avail) * horizon / left)


def busy(team, pid):
    """Locked this week, mid-game (or box score pending), or holding a lockable game."""
    return (pid in getattr(team, "lock_games", {}) or pid in team.frozen
            or any(p == pid for p, _x, _g in team.pending.values()))


def untouchable(wk, team, pid):
    """Never drop: anyone busy, and the core (top `protect_top` on the roster, anyone at or
    above `protect_ros` per game, injured stars included)."""
    return busy(team, pid) or pid in wk.protected or pid in directed_players(wk, "keep")


def directed_players(wk, kind):
    """Player restrictions already filtered for the current run by cycle.load."""
    return {r["player_id"] for r in getattr(wk, "directives", [])
            if r.get("kind") == kind and r.get("player_id")}


def ir_fix(wk, team, roster):
    """The single most urgent IR correction, or None.

    A healthy player on IR freezes the lineup in Sleeper's apps, so that is fixed first and
    only with roster/reserve operations (never a lineup save, which the freeze can block):
    take him off into an open spot, or swap a non-busy player who is IR-eligible into his IR
    spot, or drop the weakest unprotected BENCH player, or drop him if he is the weakest.
    Otherwise park one long-term Out player on IR to free an active spot."""
    league = wk.league
    max_active, max_ir = roster_limits(league)
    reserve = list(roster.get("reserve") or [])
    players = list(roster.get("players") or [])
    status = lambda p: (wk.players.get(p) or {}).get("injury_status")
    active = [p for p in players if p not in reserve]
    bench = [p for p in active if p not in team.starters]
    for p in reserve:
        if ir_eligible(league, status(p)):
            continue
        if len(active) < max_active:
            return Move("reserve", reserve=[r for r in reserve if r != p],
                        why=f"{wk.name(p)} off IR ({status(p) or 'healthy'}), a healthy player on IR freezes the lineup")
        swap_in = [q for q in active if ir_eligible(league, status(q)) and not busy(team, q)]
        if swap_in:
            q = max(swap_in, key=lambda q: ((wk.injuries.get(q) or {}).get("return_date") or "9999"))
            return Move("reserve", reserve=[q if r == p else r for r in reserve],
                        why=f"{wk.name(p)} off IR ({status(p) or 'healthy'}), {wk.name(q)} ({status(q)}) takes the IR spot")
        cut = [q for q in bench if not untouchable(wk, team, q)]
        d = min(cut, key=lambda q: keep_value(wk, q)) if cut else None
        if d is not None and keep_value(wk, d) < keep_value(wk, p):
            return Move("drop_then_reserve", drop=d, reserve=[r for r in reserve if r != p],
                        why=f"{wk.name(p)} is healthy on IR and the roster is full: dropping {wk.name(d)} ({ros(wk, d):.1f}/g) to activate him")
        if not untouchable(wk, team, p):
            return Move("drop_then_reserve", drop=p, reserve=[r for r in reserve if r != p],
                        why=f"{wk.name(p)} is healthy on IR, the roster is full and he is the weakest option: dropping him")
        return Move("needs_you", why=f"{wk.name(p)} is healthy on IR, the roster is full and nobody is safe to drop")
    if len(reserve) >= max_ir:
        return None
    end_of_week = max((g.date for g in wk.games), default="")
    for p in active:
        if not ir_eligible(league, status(p)) or busy(team, p):
            continue
        ret = (wk.injuries.get(p) or {}).get("return_date")
        if ret and ret <= end_of_week:
            continue
        return Move("reserve", reserve=reserve + [p],
                    why=f"{wk.name(p)} to IR ({status(p)}, back {ret or 'unknown'}), frees an active spot")
    return None


def recent_minutes(wk, n=4):
    """Minutes in each of a player's team's last `n` finished games this season (a DNP or a
    missing box row is 0). Rotation is judged on what the coach is doing now."""
    rows = wk.store.box(season=wk.season)
    games_by_team = {}
    mins = {}
    for r in rows:
        games_by_team.setdefault(r["team"], {})[r["game_id"]] = r["start_ms"]
        mins[(r["player_id"], r["game_id"])] = r["minutes"] or 0.0
    last = {t: sorted(g, key=g.get)[-n:] for t, g in games_by_team.items()}
    return lambda pid: [mins.get((pid, g), 0.0) for g in last.get(wk.team_of(pid), [])]


def in_rotation(minutes):
    """Fail closed: a pickup must be playing real minutes right now."""
    if len(minutes) < 2:
        return False
    return sum(minutes) / len(minutes) >= 18 or (minutes[-1] >= 20 and minutes[-2] >= 20)


def free_agents(wk, limit=60):
    owned = {p for r in wk.rosters.values() for p in (r.get("players") or [])}
    mins = recent_minutes(wk)
    fa = [p for p, x in wk.players.items()
          if x.get("team") and x.get("active") and p not in owned and (x.get("fantasy_positions") or [])
          and in_rotation(mins(p))]
    fa.sort(key=lambda p: -ros(wk, p))
    return fa[:limit]


def with_player(wk, team, add, drop=None, after_ms=None):
    """A copy of `team` with `add` on the bench (and `drop` gone), his remaining games as draws.
    A delayed claim limits only the add's games to tips after processing."""
    t = copy.copy(team)
    t.positions = dict(team.positions)
    t.games = dict(team.games)
    if drop:
        t.positions.pop(drop, None)
        t.games.pop(drop, None)
    t.positions[add] = (wk.players.get(add) or {}).get("fantasy_positions") or []
    draws = {}
    for g in wk.player_games(add):
        if g.start_ms > max(wk.now_ms, after_ms or wk.now_ms) and g.date in wk.days:
            gd = wk.model.dist(add, {"game_id": g.game_id, "date": g.date, "start_ms": g.start_ms,
                                     "team": wk.team_of(add), "opp": g.team_opp(wk.team_of(add))})
            draws[wk.days.index(g.date)] = Draw(float(gd.p_play), np.asarray(gd.samples, dtype=float), g.start_ms)
    t.games[add] = draws
    return t


def best_move(wk, team, opp, roster, exclude=(), reserved=0):
    """The single best roster move now: fill an open active spot, or an add/drop swap that
    clears swap_edge. Owner claims reserve active spots. None if nothing is worth doing."""
    if not wk.days:
        return None
    D = len(wk.days)
    max_active, _ = roster_limits(wk.league)
    reserve = set(roster.get("reserve") or [])
    active = [p for p in roster.get("players") or [] if p not in reserve]
    excluded = set(exclude) | directed_players(wk, "avoid")
    cands = [a for a in free_agents(wk) if a not in excluded]
    if not cands:
        return None
    base_ev = make_plan(team, D).value
    screened = sorted(((make_plan(with_player(wk, team, a), D).value - base_ev, a) for a in cands), reverse=True)
    shortlist = list(dict.fromkeys([a for _e, a in screened[:8]] + cands[:4]))
    base_p = decide(team, opp, D, n=4000).p_win
    if len(active) + reserved < max_active:
        scored = []
        for a in shortlist:
            p = decide(with_player(wk, team, a), opp, D, n=4000).p_win
            scored.append((p - base_p + KAPPA * keep_value(wk, a), a, p))
        sc, a, p = max(scored)
        return Move("add", add=a, score=sc,
                    why=f"open roster spot: {wk.name(a)} ({ros(wk, a):.1f}/g), week win {base_p:.0%}->{p:.0%}")
    droppable = sorted((p for p in active if not untouchable(wk, team, p)), key=lambda p: keep_value(wk, p))
    best = None
    for d in droppable[:3]:
        for a in shortlist:
            p = decide(with_player(wk, team, a, drop=d), opp, D, n=4000).p_win
            ros_gain = keep_value(wk, a) - keep_value(wk, d)
            sc = (p - base_p) + KAPPA * ros_gain
            if sc >= swap_edge(p - base_p, ros_gain) and (best is None or sc > best[0]):
                best = (sc, a, d, p)
    if best:
        sc, a, d, p = best
        return Move("swap", add=a, drop=d, score=sc,
                    why=f"{wk.name(a)} ({ros(wk, a):.1f}/g) for {wk.name(d)} ({ros(wk, d):.1f}/g), week win {base_p:.0%}->{p:.0%}")
    return None
