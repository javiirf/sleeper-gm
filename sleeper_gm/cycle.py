"""One manager cycle for one league: IR hygiene, roster moves, locks and the lineup.

Safety rules, each from a reproduced failure:
- one run per league at a time (a cross-process lock), so caps and decisions cannot race;
- locks whose window closes soon go first, before any other work;
- one roster change at a time, then a fresh read of the real roster before the next;
- writes are never retried (a timed-out write may have committed); state is re-read instead,
  and what counts as done is what an authenticated read-back shows;
- lineup history records only lineups actually seen or read back, never proposals;
- a lock we cannot prove is legal goes out on its own, and only an eligibility refusal is remembered;
- each lock is re-checked against its tip right before it is sent; moving players has a wider guard;
- IR problems are fixed with roster/reserve operations before any lineup save;
- IR-forced drops count against the daily transaction cap.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime

from . import api, claims, config, crowd, moves, notify, week
from .policy import HANDOFF_P, RISK, decide, fits, lineup_today, respecify_locks

URGENT_MS = 40 * 60 * 1000
MOVE_GUARD_MS = 60 * 1000     # do not move anyone whose game tips within a minute
LOCK_GUARD_MS = 5 * 1000      # a lock is legal until his next tip; leave a few seconds for the request
INELIGIBLE = ("not in starting lineup", "moved to bench", "ineligible", "not eligible")


class Busy(RuntimeError):
    pass


class _StoreView:
    """The store, but with the player dictionary the matchup legs just refreshed (fresher injury
    statuses than the once-a-day download), so the model and the roster logic agree."""

    def __init__(self, store, players):
        self._store, self._players = store, players

    def players(self, *a, **kw):
        return self._players

    def __getattr__(self, name):
        return getattr(self._store, name)


class Ctx:
    def __init__(self, cfg, league_id, roster_id, live, store):
        self.cfg, self.league_id, self.rid, self.live, self.store = cfg, str(league_id), int(roster_id), live, store
        self.dir = config.league_state(league_id)
        self.acted = []
        self.writer = api.Writer(live, lambda e: self.append("writes", e))

    def log_file(self, kind):
        return self.dir / "logs" / f"{kind}-{datetime.fromtimestamp(time.time(), week.ET).strftime('%Y-%m-%d')}.jsonl"

    def append(self, kind, entry):
        entry = {k: v for k, v in entry.items() if k != "query"} if kind == "writes" else entry
        with open(self.log_file(kind), "a") as f:
            f.write(api.redact(json.dumps(entry, default=str)) + "\n")

    def transactions_today(self):
        try:
            rows = [json.loads(l) for l in open(self.log_file("writes")) if l.strip()]
        except FileNotFoundError:
            return 0
        # only transactions Sleeper accepted count against the cap; a refused add costs nothing
        return sum(1 for r in rows if r.get("live") and r.get("kind") == "free_agent" and str(r.get("result", "")).startswith("ok"))

    def claim_writes_today(self):
        """Count live attempts, including refusals/timeouts, once per write id."""
        try:
            rows = [json.loads(line) for line in self.log_file("writes").read_text().splitlines() if line.strip()]
        except FileNotFoundError:
            return 0
        today = datetime.fromtimestamp(time.time(), week.ET).date()
        return len({r.get("write_id") or f"legacy-{i}" for i, r in enumerate(rows)
                    if r.get("live") and r.get("kind") in ("submit_claim", "update_claim", "cancel_claim")
                    and datetime.fromtimestamp(r.get("t", time.time()), week.ET).date() == today})

    def on_waivers(self):
        """Known waiver refusals last until the next estimated processing time."""
        try:
            data = json.loads((self.dir / "on_waivers.json").read_text())
        except (FileNotFoundError, ValueError):
            return set()
        now = time.time() * 1000
        return {p for p, value in data.items() if isinstance(value, dict) and value.get("expires", 0) > now}

    def mark_on_waivers(self, pid):
        path = self.dir / "on_waivers.json"
        expires = claims.processing_bounds(int(time.time() * 1000), claims.read_state(self.dir))[1]
        data = {p: {"expires": expires} for p in self.on_waivers()}
        data[pid] = {"expires": expires}
        path.write_text(json.dumps(data))
        claims.fa_result(self.dir, waiver_failure=True)

    @contextlib.contextmanager
    def exclusive(self):
        """One run per league at a time, across processes (schedule, CLI, MCP)."""
        try:
            import fcntl
        except ImportError:
            with self._exclusive_file():
                yield
            return
        with open(self.dir / "run.lock", "w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Busy("another sleeper-gm run is working on this league right now") from None
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    @contextlib.contextmanager
    def _lockfile_guard(self):
        """Serialize lockfile creation/deletion, including stale takeovers. SQLite releases this
        short-lived mutex on process exit, so recovery never needs another stale lockfile."""
        db = sqlite3.connect(self.dir / "run-lock.sqlite", timeout=5)
        try:
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                raise Busy("another sleeper-gm run is working on this league right now") from None
            yield
        finally:
            db.close()

    @contextlib.contextmanager
    def _exclusive_file(self):
        """O_EXCL fallback: reclaim dead owners or locks older than two hours."""
        path = self.dir / "run.lock"
        with self._lockfile_guard():
            while True:
                try:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    break
                except FileExistsError:
                    with open(path) as fh:
                        stat = os.fstat(fh.fileno())
                        try:
                            owner = json.load(fh)
                            created = float(owner["t"])
                            pid = int(owner["pid"])
                        except (ValueError, KeyError, TypeError):
                            created = stat.st_mtime  # a crashed creator may leave an empty file
                            pid = None
                    alive = True
                    if pid is not None and pid > 0:
                        try:
                            os.kill(pid, 0)
                        except OSError:
                            alive = False
                    if alive and time.time() - created <= 2 * 3600:
                        raise Busy("another sleeper-gm run is working on this league right now")
                    path.unlink()
            token = uuid.uuid4().hex  # ownership is the token, not the inode (filesystems reuse inodes)
            with os.fdopen(fd, "w") as fh:
                json.dump({"pid": os.getpid(), "t": time.time(), "token": token}, fh)
        try:
            yield
        finally:
            with self._lockfile_guard():
                try:
                    with open(path) as fh:
                        mine = json.load(fh).get("token") == token
                    if mine:
                        path.unlink()
                except (FileNotFoundError, ValueError, AttributeError):
                    pass


def load(ctx):
    from .model import Model

    def factory(wk):
        wk.store = ctx.store
        return Model(_StoreView(ctx.store, wk.players), wk.scoring, wk.now_ms)

    wk = week.load(ctx.store, factory, ctx.league_id, ctx.rid, state_dir=str(ctx.dir))
    me = week.build_team(wk, wk.my_rid, week.snapshots(wk))
    opp = week.build_team(wk, wk.opp_rid)
    cfg = ctx.cfg
    by_ros = sorted(set(wk.rosters[ctx.rid].get("players") or []), key=lambda p: -moves.ros(wk, p))
    wk.protected = set(by_ros[:cfg["protect_top"]]) | {p for p in by_ros if moves.ros(wk, p) >= cfg["protect_ros"]}
    return wk, me, opp


def _ir(ctx, wk, me, unfreeze_only=False):
    if getattr(ctx, "week_rolled_over", False):
        return wk, me
    budget = ctx.cfg["max_transactions_per_day"] - ctx.transactions_today()
    for _ in range(3):
        roster = wk.rosters[ctx.rid]
        if unfreeze_only and not any(not moves.ir_eligible(wk.league, (wk.players.get(p) or {}).get("injury_status"))
                                     for p in roster.get("reserve") or []):
            return wk, me
        m = moves.ir_fix(wk, me, roster)
        if m is None:
            return wk, me
        if m.kind == "needs_you":
            ctx.acted.append(f"NEEDS YOU: {m.why}")
            return wk, me
        if not ctx.live:
            ctx.acted.append(f"Would: {m.why}")
            return wk, me
        try:
            if m.kind == "drop_then_reserve":
                if budget <= 0:
                    ctx.acted.append(f"NEEDS YOU (daily move cap reached): {m.why}")
                    return wk, me
                budget -= 1
                ctx.writer.free_agent(ctx.league_id, ctx.rid, drop=m.drop, summary=m.why)
            parked = [p for p in m.reserve if p not in (roster.get("reserve") or [])]
            frozen_ir = any(not moves.ir_eligible(wk.league, (wk.players.get(p) or {}).get("injury_status"))
                            for p in roster.get("reserve") or [])
            if any(p in me.starters for p in parked):
                if not _refresh_write_guards(ctx, wk):
                    return wk, me
                now_ms = int(time.time() * 1000)
                if any(g.status == "in_game" or (g.status == "pre_game" and g.start_ms <= now_ms + MOVE_GUARD_MS)
                       for p in parked if p in me.starters for g in wk.player_games(p)):
                    ctx.acted.append("Skipped bench before IR: the player's team is playing or tips within 60 seconds")
                    return wk, me
                # The reserve swap unfreezes healthy IR first. Benching first can be refused.
                if not frozen_ir:
                    ctx.writer.set_leg(ctx.league_id, ctx.rid, wk.week, ["0" if p in parked else p for p in me.starters],
                                       summary="bench before IR")
            ctx.writer.set_reserve(ctx.league_id, ctx.rid, m.reserve, summary=m.why)
            ctx.acted.append(m.why)
        except api.SleeperError as e:
            ctx.acted.append(f"IR move failed ({e}); re-reading the roster")
        wk, me, _opp = load(ctx)
    return wk, me


def _moves(ctx, wk, me, opp):
    if getattr(ctx, "week_rolled_over", False):
        return wk, me, opp
    if getattr(ctx, "claims_available", False):
        try:
            claims.observe(ctx, wk)
        except api.SleeperError as e:
            ctx.claims_available = False
            ctx.acted.append(f"Could not refresh claims ({e}); leaving claims unchanged")
    if not ctx.cfg.get("moves", True):
        return wk, me, opp
    budget = ctx.cfg["max_transactions_per_day"] - ctx.transactions_today()
    attempts = 0
    while budget > 0 and attempts < 4 and not claims.fa_disabled(ctx.dir):
        attempts += 1
        pending_rows = getattr(ctx, "pending_claims", [])
        pending_adds = {p for r in pending_rows for p in claims.owned_players(r, "adds", ctx.rid)}
        # Do not invalidate an existing owner or bot claim by dropping its required player.
        protected = wk.protected
        wk.protected = protected | {p for r in pending_rows for p in claims.owned_players(r, "drops", ctx.rid)}
        try:
            m = moves.best_move(wk, me, opp, wk.rosters[ctx.rid], exclude=ctx.on_waivers() | pending_adds)
        finally:
            wk.protected = protected
        if m is None:
            break
        if not ctx.live:
            ctx.acted.append(("Would add: " if m.kind == "add" else "Would swap: ") + m.why)
            break
        err = None
        try:
            ctx.writer.free_agent(ctx.league_id, ctx.rid, add=m.add, drop=m.drop, summary=m.why)
        except api.SleeperError as e:
            err = e
        wk, me, opp = load(ctx)
        players = set(wk.rosters[ctx.rid].get("players") or [])
        if m.add in players and (not m.drop or m.drop not in players):
            budget -= 1
            claims.fa_result(ctx.dir, success=True)
            if err:  # it went through despite the error: count it like an accepted write
                ctx.append("writes", {"t": time.time(), "kind": "free_agent", "live": True, "result": "ok (confirmed after error)",
                                      "summary": m.why})
            ctx.acted.append(("Added " if m.kind == "add" else "Swap: ") + m.why
                             + (" (Sleeper errored but the move went through)" if err else ""))
        elif err and "waiver" in str(err).lower():
            ctx.mark_on_waivers(m.add)
            ctx.append("errors", {"t": time.time(), "note": f"{wk.name(m.add)} is on waivers; considering a claim"})
        else:
            ctx.acted.append(f"Could not add {wk.name(m.add)}: {err or 'not on the roster after the write'}")
            break
    if getattr(ctx, "claims_available", False) and not getattr(ctx, "week_rolled_over", False):
        claims.reconcile(ctx, wk, me, opp)
    return wk, me, opp


def _my_leg(ctx, wk):
    return next(l for l in api.legs(ctx.league_id, wk.week) if l["roster_id"] == ctx.rid)


def _refresh_write_guards(ctx, wk):
    """Refresh the public schedule and week immediately before a lineup save."""
    if getattr(ctx, "week_rolled_over", False):
        return False
    scores = api.scores(wk.season, wk.week)
    state = api.rest("/v1/state/nba", fresh=True)
    if int(state["leg"]) != wk.week:
        ctx.week_rolled_over = True
        ctx.acted.append("Skipped writes: week rolled over")
        return False
    wk.games, wk.by_team = week.schedule(scores)
    return True


def _locks(ctx, wk, me, opp, pending_drops=()):
    """Decide, then write the lineup and locks, each lock re-checked against its tip just before
    it is sent. Returns (decision, lineup, confirmed locks {pid: game})."""
    if getattr(ctx, "week_rolled_over", False):
        return None, list(me.starters), {}
    excluded = {s for s, (p, _x, _g) in me.pending.items() if p in pending_drops}
    for s in sorted(excluded):
        message = f"NEEDS YOU: not locking {wk.name(me.pending[s][0])} because your pending waiver claim drops him"
        if message not in ctx.acted:
            ctx.acted.append(message)
    dec = decide(me, opp, len(wk.days), risk=RISK, forbidden_locks=excluded) if wk.days else None
    if dec:
        new = lineup_today(me, dec, wk.has_game_today)
        new, _ = respecify_locks(me, new, dec.locks)
        lock_slots = dec.locks
    else:
        new, lock_slots = list(me.starters), sorted(set(me.pending) - excluded)
    if ctx.live and not _refresh_write_guards(ctx, wk):
        return dec, list(me.starters), {}
    now = lambda: int(time.time() * 1000)

    def open_until_tip(p):
        game = next((g for g in wk.games if g.game_id == wanted[p][0]), None)
        if game is None or game.status != "complete":
            return False
        return all(g.status == "pre_game" and g.start_ms > now() + LOCK_GUARD_MS
                   for g in wk.player_games(p) if g.start_ms > game.start_ms)

    def moving_soon(p):
        return p != "0" and any(g.status == "in_game" or (g.status != "complete" and g.start_ms <= now() + MOVE_GUARD_MS)
                                for g in wk.player_games(p))

    wanted = {me.pending[s][0]: (me.pending[s][2], s in me.uncertain, me.pending[s][1]) for s in lock_slots}
    changed = [s for s in range(len(new)) if new[s] != me.starters[s]]
    if any(moving_soon(me.starters[s]) or moving_soon(new[s]) for s in changed):
        new = list(me.starters)  # a tip is close or past since the read: lock in place, move nobody
    for s in list(me.locked) + list(me.inflight):
        assert new[s] == me.starters[s], "refusing to move a locked or held player"
    real = [p for p in new if p != "0"]
    assert all(p in me.positions for p in real) and len(real) == len(set(real)), "invalid lineup"

    if not ctx.live:
        for p, (_g, _u, x) in wanted.items():
            ctx.acted.append(f"Would lock {wk.name(p)} {x:.1f}")
        _describe_lineup(ctx, wk, me.starters, new, would=True)
        return dec, new, {}

    games = dict(me.lock_games)
    sent = {}
    attempted = {}
    certain = {p: g for p, (g, unsure, _x) in wanted.items() if not unsure and open_until_tip(p)}
    stop = False
    if new != me.starters or certain:
        attempted.update(certain)
        try:
            names = ", ".join(wk.name(p) for p in certain)
            r = ctx.writer.set_leg(ctx.league_id, ctx.rid, wk.week, new, {**games, **certain} if certain else None,
                                   summary=("lock " + names + "; " if names else "") + "lineup")
            games = dict(((r or {}).get("update_matchup_leg") or {}).get("starters_games") or {**games, **certain})
            sent.update(certain)
        except api.SleeperError as e:
            ctx.acted.append(f"Lineup write failed ({e}); re-reading before anything else")
            games = dict(_my_leg(ctx, wk).get("starters_games") or {})
            sent.update({p: g for p, g in certain.items() if games.get(p) == g})
            stop = True
    for p, (g, unsure, _x) in wanted.items():
        if stop or not unsure:
            continue
        if not open_until_tip(p):
            continue
        attempted[p] = g
        try:
            r = ctx.writer.set_leg(ctx.league_id, ctx.rid, wk.week, new, {**games, p: g},
                                   summary=f"lock {wk.name(p)} (lineup at tip not seen)")
            games = dict(((r or {}).get("update_matchup_leg") or {}).get("starters_games") or {**games, p: g})
            sent[p] = g
        except api.SleeperError as e:
            games = dict(_my_leg(ctx, wk).get("starters_games") or {})  # did it commit anyway?
            if games.get(p) == g:
                sent[p] = g
            elif any(k in str(e).lower() for k in INELIGIBLE):
                week.mark_unlockable(wk, p, g, str(e))
                ctx.acted.append(f"Sleeper refused locking {wk.name(p)}: he was not starting at tip")
            else:
                ctx.acted.append(f"Lock of {wk.name(p)} failed ({e}); will retry next run")
                stop = True
    confirmed = {}
    got = list(me.starters)
    if attempted or new != me.starters:
        leg = _my_leg(ctx, wk)
        got = list(leg.get("starters") or [])
        week.record_snapshot(wk, got, now(), "read-back")
        confirmed = {p: g for p, g in attempted.items() if (leg.get("starters_games") or {}).get(p) == g}
        if got != new or len(confirmed) < len(sent):
            ctx.acted.append("WRITE DID NOT STICK: Sleeper's lineup reads back different; check the app")
            ctx.append("errors", {"t": time.time(), "wanted": new, "got": got, "locks": sent, "confirmed": confirmed})
    for p, (_g, _u, x) in wanted.items():
        if p in confirmed:
            ctx.acted.append(f"Locked {wk.name(p)} {x:.1f}")
        elif not open_until_tip(p) and p not in sent:
            ctx.acted.append(f"Missed the lock window for {wk.name(p)} {x:.1f} (his next game tipped)")
    _describe_lineup(ctx, wk, me.starters, got)
    return dec, got, confirmed


def _describe_lineup(ctx, wk, old, new, would=False):
    ins = [p for p in new if p not in old and p != "0"]
    outs = [p for p in old if p not in new and p != "0"]
    if ins or outs:
        ctx.acted.append(("Would change lineup: in " if would else "Lineup: in ") + (", ".join(wk.name(p) for p in ins) or "nobody")
                         + (" / out " + ", ".join(wk.name(p) for p in outs) if outs else ""))


def _claims_check(ctx, wk, me):
    """Read pending claims once; warn about risky drops and keep them out of locks."""
    ctx.claims_available = False
    try:
        rows = claims.pending(ctx)
        ctx.pending_claims = rows
        ctx.claims_available = True
    except (api.SleeperError, KeyError, TypeError):
        ctx.pending_claims = []
        return set()
    drops = set()
    for r in rows:
        if r.get("type") not in (None, "waiver"):
            continue
        for p, rid in (r.get("drops") or {}).items():
            if str(rid) != str(ctx.rid):
                continue
            drops.add(p)
            if p in me.starters or p in me.lock_games or p in wk.protected:
                ctx.acted.append(f"NEEDS YOU: your pending waiver claim drops {wk.name(p)}, who is starting, locked or one of your best players")
    return drops


def plan_details(wk, team, dec, starters=None):
    """Serializable nominal looks. Same-day branches are conditional on a timely final.

    Plans belong to slots, not occupants. When a new lock moves to a more specific
    slot, that slot's plan follows its displaced occupant into the broader slot.
    """
    if not dec:
        return {}
    plan, out = dec.plan, {}
    for s in plan.choice:
        looks = []
        for i, p, d, tip in plan.looks(s):
            normal, delayed = plan.after[s][i]
            c, terminal = plan.C[s][normal]
            looks.append({"id": p, "name": wk.name(p), "date": wk.days[d], "tip_ms": tip,
                          "lock_above": round(c, 1), "terminal": terminal,
                          "handoff_p": HANDOFF_P if normal != delayed else 1.0})
        out[s] = {"looks": looks, "expected_value": round(plan.C[s][0][0], 1)}
    if starters is not None:
        for s in dec.locks:
            p = team.starters[s]
            if p in starters and starters.index(p) != s:
                target = starters.index(p)
                if target in out:
                    out[s] = out.pop(target)
    return out


def bench_role(wk, team, plans, p):
    uses = []
    for s, plan in plans.items():
        days = list(dict.fromkeys(datetime.fromisoformat(x["date"]).strftime("%a")
                                  for x in plan["looks"] if x["id"] == p))
        if days:
            uses.append(f"{team.slots[s]} {', '.join(days)}")
    if uses:
        return "plays " + "; ".join(uses) + " (if open)"
    available = list(dict.fromkeys(team.slots[s] for s in plans if fits(team.slots[s], team.positions.get(p, []))))
    if not available:
        return "no open slot this week"
    if not team.games.get(p):
        return "no games left this week"
    return "reserve for " + "/".join(available) + " (not in current plan)"


def snapshot(ctx, wk, me, opp, dec, starters, done, write=True):
    """Everything the web app shows about right now (written to now.json)."""
    plans = plan_details(wk, me, dec, starters)
    users = {}
    try:
        users = {u["user_id"]: u for u in api.rest(f"/v1/league/{ctx.league_id}/users")}
    except api.SleeperError:
        pass

    def team_name(rid):
        u = users.get(wk.rosters[rid].get("owner_id")) or {}
        return (u.get("metadata") or {}).get("team_name") or u.get("display_name") or f"Team {rid}"

    def nxt(p):
        g = next((g for g in wk.player_games(p) if g.start_ms > wk.now_ms), None)
        if not g:
            return None
        t = wk.team_of(p)
        return {"date": g.date, "tip_ms": g.start_ms, "opp": g.team_opp(t), "home": g.home == t}

    def live_score(p):
        g = next((g for g in wk.player_games(p) if wk.in_flight(g)), None)
        return round(wk.score(p, g), 1) if g and (p, g.game_id) in wk.box else None

    def pinfo(p, team):
        x = wk.players.get(p) or {}
        draws = team.games.get(p, {})
        first = draws[min(draws)] if draws else None
        return {"id": p, "name": wk.name(p), "team": x.get("team"), "pos": x.get("fantasy_positions") or [],
                "injury": x.get("injury_status"), "return": (wk.injuries.get(p) or {}).get("return_date"),
                "games_left": len(draws), "p_play": round(first.p_play, 2) if first else None,
                "proj": round(first.mean, 1) if first else None, "next": nxt(p)}

    def slots(team, mine):
        """Rows for the lineup as it now stands. State follows the PLAYER, because a lock can
        move him into a more specific slot (and the occupant he swaps with to his old one)."""
        by = {}
        for s, p in enumerate(team.starters):
            if s in team.locked:
                by[p] = {"state": "locked", "score": round(team.locked[s], 1)}
            elif s in team.inflight:
                by[p] = {"state": "playing", "score": live_score(p)}
            elif s in team.pending:
                row = {"state": "pending", "score": round(team.pending[s][1], 1)}
                if mine and dec:
                    row.update(open_value=round(dec.cont.get(s, 0), 1), decision="lock" if s in dec.locks else "pass")
                by[p] = row
        out = []
        for s, slot in enumerate(team.slots):
            p = starters[s] if mine else team.starters[s]
            row = {"slot": slot, "player": pinfo(p, team) if p != "0" else None}
            info = dict(by.get(p, {}))
            if mine and p in done:
                info.update(state="locked", just_locked=True)
            info.setdefault("state", "open" if p != "0" else "empty")
            row.update(info)
            if mine and row["state"] != "locked":
                if s in plans:
                    row["plan"] = plans[s]
                    row["slot_value"] = plans[s]["expected_value"]
                elif row.get("decision") == "lock":
                    row["plan"] = {"looks": [], "expected_value": row["score"], "locking": True}
            out.append(row)
        return out

    try:  # the legs carry no points until a week is scored; this is Sleeper's running total
        running = {m["roster_id"]: m.get("points") for m in api.rest(f"/v1/league/{ctx.league_id}/matchups/{wk.week}", fresh=True) or []}
    except api.SleeperError:
        running = {}

    def points(team):
        rid = ctx.rid if team is me else wk.opp_rid
        return wk.legs[rid].get("points") or running.get(rid) or 0

    roster = wk.rosters[ctx.rid]
    bench = [p for p in roster.get("players") or [] if p not in starters and p not in (roster.get("reserve") or [])]
    snap = {
        "t": int(time.time() * 1000), "live": ctx.live, "league": {"id": ctx.league_id, "name": wk.league.get("name"),
                                                                   "season": wk.season, "week": wk.week},
        "days_left": wk.days, "p_win": dec.p_win if dec else None,
        "me": {"name": team_name(ctx.rid), "record": _record(wk.rosters[ctx.rid]), "points": points(me),
               "proj": round(dec.mean_total, 1) if dec else None, "slots": slots(me, True),
               "bench": [{**pinfo(p, me), "role": bench_role(wk, me, plans, p)} for p in bench],
               "ir": [pinfo(p, me) for p in roster.get("reserve") or []]},
        "opp": {"name": team_name(wk.opp_rid), "record": _record(wk.rosters[wk.opp_rid]), "points": points(opp),
                "proj": round(dec.opp_mean, 1) if dec else None, "slots": slots(opp, False)},
        "acted": ctx.acted,
        "claims": claims.view(ctx, wk),
    }
    pending = {p for p, _x, _g in me.pending.values()}
    rows = [r for r in snap["me"]["slots"] if r["state"] == "pending" and r["player"]["id"] in pending]
    if rows:
        if not hasattr(ctx, "_crowd_signals"):
            ctx._crowd_signals = {}  # a failed burst is never retried during this run
            try:
                pids = [r["player"]["id"] for r in rows]
                gids = {g for p, _x, g in me.pending.values() if p in pids}
                since = min((g.start_ms for g in wk.games if g.game_id in gids), default=wk.now_ms)
                ctx._crowd_signals = crowd.signals(wk.players, pids, since, gql=api.gql)
            except Exception:
                pass  # crowd data is optional and never affects a decision
        for row in rows:
            signal = ctx._crowd_signals.get(row["player"]["id"])
            if signal is not None:
                row["crowd"] = {k: signal[k] for k in ("n", "lock", "pass", "lock_share", "news")}
    if write:
        (ctx.dir / "now.json").write_text(api.redact(json.dumps(snap, default=str)))
    return snap


def preview(ctx, with_moves=True):
    """Read-only: what a run would do now. Writes nothing to Sleeper or to now.json."""
    wk, me, opp = load(ctx)
    ir = moves.ir_fix(wk, me, wk.rosters[ctx.rid])
    dec = decide(me, opp, len(wk.days), risk=RISK) if wk.days else None
    starters = list(me.starters)
    if dec:
        starters, _ = respecify_locks(me, lineup_today(me, dec, wk.has_game_today), dec.locks)
    _claims_check(ctx, wk, me)
    snap = snapshot(ctx, wk, me, opp, dec, starters, {}, write=False)
    snap["would"] = {
        "lock": [wk.name(me.pending[s][0]) for s in (dec.locks if dec else [])],
        "pass": [wk.name(me.pending[s][0]) for s in (dec.passes if dec else [])],
        "start_today": {me.slots[s]: wk.name(p) for s, p in (dec.assign_today.items() if dec else [])},
        "ir": ir.why if ir else None,
        "roster_move": None,
    }
    if with_moves and ctx.cfg.get("moves", True):
        m = moves.best_move(wk, me, opp, wk.rosters[ctx.rid])
        snap["would"]["roster_move"] = m.why if m else None
    return snap, wk, me


def _record(r):
    s = r.get("settings") or {}
    return f"{s.get('wins', 0)}-{s.get('losses', 0)}" + (f"-{s['ties']}" if s.get("ties") else "")


def run_league(ctx, brief=False):
    """Return the snapshot plus `notified`: delivered, or printed with no channel configured."""
    with ctx.exclusive():
        return _run(ctx, brief)


def _run(ctx, brief):
    t0 = time.time()
    ctx.__dict__.pop("_crowd_signals", None)
    ctx.week_rolled_over = False
    wk, me, opp = load(ctx)
    pending_drops = _claims_check(ctx, wk, me)
    wk, me = _ir(ctx, wk, me, unfreeze_only=True)
    urgent = any((wk.next_tip(p) or 1 << 62) - wk.now_ms < URGENT_MS for p, _x, _g in me.pending.values())
    if urgent:
        _locks(ctx, wk, me, opp, pending_drops)  # after unfreezing, a closing lock window beats optional moves
        if ctx.live and not getattr(ctx, "week_rolled_over", False):
            wk, me, opp = load(ctx)
    wk, me = _ir(ctx, wk, me)
    wk, me, opp = _moves(ctx, wk, me, opp)
    if ctx.live and any(a.startswith(("Added", "Swap")) for a in ctx.acted):
        wk, me, opp = load(ctx)
    if getattr(ctx, "claims_available", False):
        pending_drops = {p for r in ctx.pending_claims for p in claims.owned_players(r, "drops", ctx.rid)}
    dec, starters, done = _locks(ctx, wk, me, opp, pending_drops)
    # Keep the actually observed roster, not the proposed lineup, for the cheap gate.
    ctx.roster_for_gate = wk.rosters[ctx.rid] if hasattr(wk, "rosters") else None
    snap = snapshot(ctx, wk, me, opp, dec, starters, done)
    entry = {"t": time.time(), "secs": round(time.time() - t0, 1), "live": ctx.live, "week": wk.week,
             "p_win": snap["p_win"], "mean": snap["me"]["proj"], "opp_mean": snap["opp"]["proj"],
             "points": snap["me"]["points"], "opp_points": snap["opp"]["points"],
             "decisions": [{"player": r["player"]["name"], "slot": r["slot"], "score": r.get("score"),
                            "open_value": r.get("open_value"), "decision": r.get("decision")}
                           for r in snap["me"]["slots"] if r.get("decision")],
             "acted": ctx.acted}
    ctx.append("decisions", entry)
    head = (f"{snap['league']['name']}, week {wk.week}: {snap['p_win']:.0%} to win "
            f"(you ~{snap['me']['proj']:.0f}, {snap['opp']['name']} ~{snap['opp']['proj']:.0f})"
            if dec else f"{snap['league']['name']}, week {wk.week}: no games left")
    if not ctx.live:
        head += " [paused: nothing was sent]"
    say = bool(ctx.acted) or brief
    if not ctx.live and ctx.acted and not brief:
        # paused: the same "would" list every half hour is noise; only say it when it changes
        seen = ctx.dir / "last_would.json"
        prev = json.loads(seen.read_text()) if seen.exists() else []
        say = sorted(ctx.acted) != sorted(prev)
        seen.write_text(json.dumps(ctx.acted))
    snap["notified"] = False
    if say:
        lines = [head] + [f"- {a}" for a in ctx.acted]
        if brief and dec:
            lines.append("")
            for r in snap["me"]["slots"]:
                p = r["player"]["name"] if r["player"] else "(empty)"
                tag = {"locked": f"locked {r.get('score')}", "playing": "playing now",
                       "pending": f"{r.get('score')} vs open {r.get('open_value')}: {r.get('decision')}"}.get(r["state"], "")
                lines.append(notify.plan_line(r) or f"{r['slot']:<4} {p} {tag}".rstrip())
                if r["state"] == "pending":
                    chat = notify.crowd_line(r.get("crowd"))
                    if chat:
                        lines.append(chat)
        url = ctx.cfg.get("web_url")
        if url:
            lines.append(url)
        delivered = notify.send(ctx.cfg, "\n".join(lines), log=lambda e: ctx.append("errors", e))
        channels = ctx.cfg.get("notify") or {}
        snap["notified"] = delivered or not any(channels.get(k) for k in ("command", "telegram_chat_id", "ntfy_topic"))
    return snap
