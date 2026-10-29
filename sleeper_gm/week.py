"""Live state of the matchup week, turned into policy.Team objects for both sides."""
from __future__ import annotations

import json
import os
import time
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from . import api
from .policy import Draw, Team
from .scoring import fantasy_points

ET = ZoneInfo("America/New_York")
REAL_STATUS = ("pre_game", "in_game", "complete")


def et_date(ms):
    return datetime.fromtimestamp(ms / 1000, ET).strftime("%Y-%m-%d")


@dataclass
class Game:
    game_id: str
    date: str
    start_ms: int
    status: str
    home: str
    away: str

    def team_opp(self, team):
        return self.away if team == self.home else self.home


@dataclass
class Week:
    season: str
    week: int
    now_ms: int
    league: dict
    scoring: dict
    slots: list
    games: list                     # Game, this week
    by_team: dict                   # team -> [Game] sorted by tip
    days: list                      # ET dates still to play (incl. a game in flight)
    box: dict                       # (pid, game_id) -> stats
    players: dict                   # Sleeper player dict (statuses overlaid from the legs)
    injuries: dict
    rosters: dict                   # roster_id -> roster
    legs: dict                      # roster_id -> leg
    my_rid: int
    opp_rid: int
    state_dir: str = "state"
    extra: dict = field(default_factory=dict)

    def name(self, pid):
        p = self.players.get(pid) or {}
        return f"{p.get('first_name', '?')} {p.get('last_name', pid)}".strip()

    def team_of(self, pid):
        return (self.players.get(pid) or {}).get("team")

    def player_games(self, pid):
        """His current team's games this week (what he can still play)."""
        return self.by_team.get(self.team_of(pid), [])

    def finished_games(self, pid):
        """Finished games that were his: any game he has a box row in (whatever team), plus his
        current team's finished games since he joined it (no row there means he sat)."""
        joined = int((self.players.get(pid) or {}).get("team_changed_at") or 0)
        mine = {gid for (p, gid) in self.box if p == pid}
        out = [g for g in self.games if g.status == "complete" and g.start_ms <= self.now_ms
               and (g.game_id in mine or (g in self.player_games(pid) and g.start_ms >= joined))]
        return sorted(out, key=lambda g: g.start_ms)

    def next_tip(self, pid):
        nxt = [g.start_ms for g in self.player_games(pid) if g.start_ms > self.now_ms]
        return min(nxt) if nxt else None

    @property
    def box_games(self):
        return {gid for _p, gid in self.box}

    def score(self, pid, game):
        """Fantasy points of a finished game: 0 when he has no box row (DNP)."""
        st = self.box.get((pid, game.game_id))
        return fantasy_points(st, self.scoring) if st else 0.0

    def in_flight(self, g):
        return g.start_ms <= self.now_ms and g.status != "complete"

    def has_game_today(self, pid):
        return bool(self.days) and any(g.date == self.days[0] and g.start_ms > self.now_ms for g in self.player_games(pid))


def schedule(scores):
    """Parse public scores into the real games and their current team schedules."""
    games = []
    for g in scores:
        if g.get("status") not in REAL_STATUS:
            continue  # postponed/cancelled: not a game, so the previous one stays lockable (Sleeper's rule)
        m = g.get("metadata") or {}
        games.append(Game(g["game_id"], et_date(int(g["start_time"])), int(g["start_time"]), g["status"],
                          (m.get("home_team") or {}).get("team"), (m.get("away_team") or {}).get("team")))
    games.sort(key=lambda g: g.start_ms)
    by_team = {}
    for g in games:
        for t in (g.home, g.away):
            by_team.setdefault(t, []).append(g)
    return games, by_team


def load(store, model_factory, league_id, my_rid, now_ms=None, state_dir="state"):
    now_ms = now_ms or int(time.time() * 1000)
    league = api.rest(f"/v1/league/{league_id}")
    state = api.rest("/v1/state/nba", fresh=True)
    season, week = str(league["season"]), int(state["leg"])
    slots = [p for p in league["roster_positions"] if p not in ("BN", "IR", "TAXI")]
    games, by_team = schedule(api.scores(season, week))
    days = sorted({g.date for g in games if g.start_ms > now_ms or g.status != "complete"})
    box = {}
    for r in api.rest(f"/stats/nba/{season}/{week}?season_type=regular", fresh=True) or []:
        if not str(r.get("player_id", "")).startswith("TEAM_"):
            box[(r["player_id"], r["game_id"])] = r.get("stats") or {}
    rosters = {r["roster_id"]: r for r in api.league_rosters(league_id)}
    raw_legs = api.legs_with_players(league_id, week)
    seen_ms = int(time.time() * 1000)  # the lineup below was in place at least at this moment
    legs = {l["roster_id"]: l for l in raw_legs}
    players = dict(store.players())
    move_path = Path(state_dir) / "team_moves.json"
    try:
        team_moves = json.loads(move_path.read_text())
    except (FileNotFoundError, ValueError):
        team_moves = {}
    for pid, move in list(team_moves.items()):
        observed_this_week = move.get("week") == week and move.get("season") == season
        if not move.get("t") or (not observed_this_week and seen_ms - move["t"] > 8 * 24 * 3600 * 1000):
            del team_moves[pid]
        elif pid in players and players[pid].get("team") == move.get("team"):
            players[pid] = {**players[pid], "team_changed_at": max(players[pid].get("team_changed_at") or 0, move["t"])}
    for l in raw_legs:
        for pid, info in (l.get("player_map") or {}).items():
            if pid in players and isinstance(info, dict) and "injury_status" in info:
                old = players[pid]
                team = info.get("team", old.get("team"))
                changed = max(info.get("team_changed_at") or 0, old.get("team_changed_at") or 0)
                if team != old.get("team"):
                    if (team_moves.get(pid) or {}).get("team") != team:
                        team_moves[pid] = {"team": team, "t": info.get("team_changed_at") or seen_ms,
                                           "week": week, "season": season}
                move = team_moves.get(pid)
                if move and move.get("t") and move.get("team") == team:  # a teamless player must not match "no record"
                    changed = max(changed, move["t"])
                players[pid] = {**old, "injury_status": info.get("injury_status"), "team": team, "team_changed_at": changed,
                                "fantasy_positions": info.get("fantasy_positions") or old.get("fantasy_positions")}
        l.pop("player_map", None)
    move_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=move_path.parent, prefix="team_moves-", delete=False) as f:
        json.dump(team_moves, f)
        temporary = Path(f.name)
    temporary.replace(move_path)
    mine = legs[my_rid]
    opp_rid = next(r for r, l in legs.items() if l.get("matchup_id") == mine.get("matchup_id") and r != my_rid)
    wk = Week(season, week, now_ms, league, league["scoring_settings"], slots, games, by_team, days, box,
              players, store.injuries(), rosters, legs, my_rid, opp_rid, state_dir)
    record_snapshot(wk, list(mine.get("starters") or []), seen_ms, "observed")
    wk.model = model_factory(wk)
    return wk


def _path(wk, name):
    os.makedirs(wk.state_dir, exist_ok=True)
    return os.path.join(wk.state_dir, name)


def snapshots(wk):
    try:
        with open(_path(wk, "snapshots.jsonl")) as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []


def record_snapshot(wk, starters, t_ms, source):
    """Only lineups that were really in place: what a read showed, or a write's read-back."""
    with open(_path(wk, "snapshots.jsonl"), "a") as f:
        f.write(json.dumps({"t": t_ms, "week": wk.week, "starters": starters, "source": source}) + "\n")


def unlockable(wk):
    try:
        with open(_path(wk, "unlockable.json")) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def mark_unlockable(wk, pid, game_id, why):
    seen = unlockable(wk)
    seen[f"{pid}:{game_id}"] = why
    with open(_path(wk, "unlockable.json"), "w") as f:
        json.dump(seen, f)


def started_through(pid, tip_ms, week, snaps):
    """Was pid starting at tip and in every lineup seen since? True / False, or None when we
    never saw the lineup before that tip (the lock is then tried on its own, and a refusal
    is remembered)."""
    snaps = [s for s in snaps if s["week"] == week]
    before = [s for s in snaps if s["t"] <= tip_ms]
    if any(pid not in s["starters"] for s in snaps if s["t"] > tip_ms):
        return False
    if not before:
        return None
    return pid in before[-1]["starters"]


def build_team(wk, rid, snaps=None):
    """policy.Team for roster `rid` from its matchup leg. Our own side checks lock eligibility
    against observed lineups; the opponent's pending games are assumed eligible."""
    roster, leg = wk.rosters[rid], wk.legs[rid]
    starters = list(leg.get("starters") or [])
    starters += ["0"] * (len(wk.slots) - len(starters))
    locks = leg.get("starters_games") or {}
    reserve = set(roster.get("reserve") or [])
    active = [p for p in roster.get("players") or [] if p not in reserve]
    positions = {p: (wk.players.get(p) or {}).get("fantasy_positions") or [] for p in active}
    t = Team(list(wk.slots), starters, positions, now_ms=wk.now_ms)
    t.lock_games, t.uncertain = {}, set()
    refused = unlockable(wk) if snaps is not None else {}
    for p in active:
        draws = {}
        for g in wk.player_games(p):
            if g.status == "complete" or g.date not in wk.days:
                continue
            gd = wk.model.dist(p, {"game_id": g.game_id, "date": g.date, "start_ms": g.start_ms,
                                   "team": wk.team_of(p), "opp": g.team_opp(wk.team_of(p))})
            draws[wk.days.index(g.date)] = Draw(float(gd.p_play), np.asarray(gd.samples, dtype=float), g.start_ms)
            if wk.in_flight(g):
                t.frozen.add(p)
        t.games[p] = draws
        if not any(g.start_ms > wk.now_ms or wk.in_flight(g) for g in wk.player_games(p)):
            t.final_done.add(p)
    for i, p in enumerate(starters):
        if p == "0":
            continue
        if p in locks:
            g = next((g for g in wk.games if g.game_id == locks[p]), None)
            t.locked[i] = wk.score(p, g) if g else 0.0
            t.lock_games[p] = locks[p]
            continue
        if p in t.frozen:
            t.inflight[i] = p
            continue
        done = wk.finished_games(p)
        if not done:
            continue
        g = done[-1]
        if g.game_id not in wk.box_games:
            t.frozen.add(p)  # finished but its box score is not in the (cached) feed yet: hold, decide next run
            t.inflight[i] = p
            continue
        if f"{p}:{g.game_id}" in refused:
            continue
        ok = True if snaps is None else started_through(p, g.start_ms, wk.week, snaps)
        if ok is False:
            continue
        t.pending[i] = (p, wk.score(p, g), g.game_id)
        if ok is None:
            t.uncertain.add(i)
    return t
