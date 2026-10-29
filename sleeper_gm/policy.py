"""Lock-In decisions: which finished games to lock, who sits in each open slot today.

The week is a stopping problem per open starting slot. Each tip is an event;
a look holds the slot until a scheduled run after the final and box delay.
After it, lock (keep that score
for the week) or pass (lose it, keep the slot open). The occupant's final game
of the week counts if nothing was locked, so the last draw is forced.

`make_plan()` binds every available player to at most one open slot for the rest
of the week (a player cannot be in two slots, and binding keeps the slots
independent), then solves each slot's stopping problem backward over its bound
players' games. A slot's continuation value C is what keeping it open is worth.
`decide()` locks a finished game when locking beats passing on expected points,
then flips any decision whose flip raises the simulated probability of winning
the matchup, which is the actual objective (ahead you lock safer, behind you hold
out for a ceiling game). Simulations use common random numbers keyed by player and
game day, so two alternatives differ only where their decisions differ.
"""
from __future__ import annotations

import copy
import zlib
from bisect import bisect_left
from dataclasses import dataclass, field

import numpy as np

ELIG = {"PG": {"PG"}, "SG": {"SG"}, "G": {"PG", "SG"}, "SF": {"SF"}, "PF": {"PF"},
        "F": {"SF", "PF"}, "C": {"C"}, "UTIL": None}
SPECIFICITY = {"PG": 0, "SG": 0, "SF": 0, "PF": 0, "C": 0, "G": 1, "F": 1, "UTIL": 2}
TILTS = (-6.0, -3.0, 0.0, 3.0, 6.0)
RISK = (-0.5, 0.0, 0.75, 1.5)   # lineup variants valued at mean + r * standard deviation
MINUTE = 60_000
GAME_SPAN = 150 * MINUTE  # 2h30, allowing 15 minutes beyond a typical 2h15 game
BOX_DELAY = 10 * MINUTE  # final box feed/cache delay
LEAD = 10 * MINUTE       # finish a run before the incoming player's tip
HANDOFF_P = 0.90         # assumption, not fitted: 10% miss the rest of that day's looks


def next_run(ms):
    """First scheduled :10/:40 run at or after ms (also valid across ET DST)."""
    cadence, offset = 30 * MINUTE, 10 * MINUTE
    return ((ms - offset + cadence - 1) // cadence) * cadence + offset


def fits(slot, positions):
    e = ELIG[slot]
    return bool(positions) if e is None else bool(e & set(positions))


@dataclass
class Draw:
    """One future game: chance he plays, and fantasy-point samples if he does."""
    p_play: float
    samples: np.ndarray
    tip_ms: int | None = None  # absent in historical/day-only callers

    @property
    def mean(self):
        return float(self.samples.mean())

    def value(self, nxt, threshold=None):
        """Expected slot value with this draw, given the slot's continuation `nxt`.

        nxt = (c, terminal). terminal: this is the slot's last draw, forced, a DNP scores 0.
        Otherwise lock at the owner's threshold when set, or when the game beats c.
        A DNP leaves the slot at c unless the owner's threshold locks its zero.
        """
        c, terminal = nxt
        if terminal:
            return self.p_play * self.mean
        outcomes = (np.maximum(self.samples, c) if threshold is None
                    else np.where(self.samples >= threshold, self.samples, c))
        dnp = 0.0 if threshold is not None and threshold <= 0 else c
        return self.p_play * float(outcomes.mean()) + (1 - self.p_play) * dnp


@dataclass
class Team:
    slots: list                      # starting slot names, positional
    starters: list                   # player id per slot, "0" empty
    positions: dict                  # player id -> fantasy positions (active roster only, not IR)
    locked: dict = field(default_factory=dict)     # slot index -> locked score
    pending: dict = field(default_factory=dict)    # slot index -> (pid, score, game_id): lockable finished game
    inflight: dict = field(default_factory=dict)   # slot index -> pid held in place now (in a game, or box score not in yet)
    games: dict = field(default_factory=dict)      # pid -> {day index: Draw} for games not yet over (incl. in flight)
    frozen: set = field(default_factory=set)       # pids that cannot be moved now
    final_done: set = field(default_factory=set)   # pids whose last game of the week is already over
    now_ms: int | None = None
    lock_at: dict = field(default_factory=dict)    # pid -> owner threshold for this week only

    def open_slots(self, extra_locks=()):
        return [i for i in range(len(self.slots)) if i not in self.locked and i not in extra_locks]


@dataclass
class Plan:
    bind: dict      # open slot -> [pids bound to it for the week]
    C: dict         # open slot -> [(value, terminal)] entering each event, plus end
    choice: dict    # open slot -> [pid or None] at each tip event
    base: dict      # open slot -> score it keeps if nobody new plays there
    value: float    # expected points still to come from the open slots
    events: dict = field(default_factory=dict)  # slot -> [(tip_ms or None, day)]
    after: dict = field(default_factory=dict)   # slot -> [(normal next index, delayed next index)]

    def looks(self, s):
        """Ordered normal-timing path; later looks require passing earlier ones."""
        i = 0
        while i < len(self.choice[s]):
            p = self.choice[s][i]
            if p is None:
                i += 1
                continue
            tip, day = self.events[s][i]
            yield i, p, day, tip
            i = self.after[s][i][0]


def slot_events(team, members, D, daily=False):
    """A shared event grid and handoff transitions for DP, realization and MC."""
    games = [(d, dr.tip_ms) for p in members for d, dr in team.games.get(p, {}).items() if d < D]
    if daily or any(t is None for _d, t in games) or not games:
        return [(None, d) for d in range(D)], [(d + 1, d + 1) for d in range(D)]
    events = sorted({(t, d) for d, t in games})
    tips = [t for t, _d in events]
    after = []
    for i, (t, d) in enumerate(events):
        ready = next_run(t + GAME_SPAN + BOX_DELAY)
        if team.now_ms is not None and t <= team.now_ms:
            # A held game is still unresolved at this run, even if its modeled
            # deadline passed. It cannot free a slot before a subsequent run.
            ready = max(ready, next_run(team.now_ms + 1))
        normal = bisect_left(tips, ready + LEAD)
        tomorrow = next((j for j in range(i + 1, len(events)) if events[j][1] > d), len(events))
        after.append((normal, max(normal, tomorrow)))
    return events, after


def look_value(dr, C, transition, threshold=None):
    normal, delayed = transition
    return HANDOFF_P * dr.value(C[normal], threshold) + (1 - HANDOFF_P) * dr.value(C[delayed], threshold)


def slot_dp(team, members, D, base, fixed0=None, skip0=False, *, events=None, after=None):
    """Backward stopping problem for one slot over its members' games."""
    if events is None:
        events, after = slot_events(team, members, D)
    N = len(events)
    C = [None] * (N + 1)
    ch = [None] * N
    nxt = (base, True)
    C[N] = nxt
    for i in range(N - 1, -1, -1):
        tip, d = events[i]
        best, pick = nxt, None
        if d == 0 and fixed0 is not None and (tip is None or team.games[fixed0][0].tip_ms == tip):
            best, pick = (look_value(team.games[fixed0][0], C, after[i], team.lock_at.get(fixed0)), False), fixed0
        elif not (d == 0 and skip0):
            for p in members:
                dr = team.games.get(p, {}).get(d)
                if dr is None or (tip is not None and dr.tip_ms != tip) or (d == 0 and p in team.frozen):
                    continue
                v = look_value(dr, C, after[i], team.lock_at.get(p))
                # An owner threshold can make a look worse than skipping. With later
                # games a tie still plays him; before a forced last draw require an edge.
                zero_final = nxt[1] and dr.p_play == 0 and v == best[0] == 0 and pick is None
                if (v > best[0] + 1e-9 or zero_final
                        or (not nxt[1] and v >= best[0] - 1e-9 and (pick is None or v > best[0] + 1e-9))):
                    best, pick = (v, False), p
        C[i], ch[i] = best, pick
        nxt = best
    return C, ch


def make_plan(team, D, locks=(), opponent=False):
    """Bind players to open slots for the week and solve each slot.

    `locks`: slots being locked now (their occupant leaves the pool).
    opponent=True: their occupants keep their slots; only a slot worth nothing
    (empty, or an occupant who will not play again) takes their best bench player.
    """
    locks = set(locks)
    open_ = team.open_slots(locks)
    used = {team.starters[s] for s in team.locked} | {team.starters[s] for s in locks}
    pool = [p for p in team.positions if p not in used]
    fixed = {team.starters[s]: s for s in team.inflight if s in open_}
    bind = {s: [] for s in open_}
    for p, s in fixed.items():
        bind[s].append(p)
    bound = set(fixed)
    for s in open_:
        p = team.starters[s]
        if p in pool and p not in bound and fits(team.slots[s], team.positions.get(p, [])):
            bind[s].append(p)
            bound.add(p)

    def base_of(s, members):
        if s in team.pending and s not in locks:
            p, x, _g = team.pending[s]
            if p in team.final_done and p in members:
                return x
        return 0.0

    cache = {}

    def sval(s, members):
        key = (s, tuple(sorted(members)))
        if key not in cache:
            occ = team.starters[s] if s in team.inflight else None
            fixed0 = occ if occ and 0 in team.games.get(occ, {}) else None
            events, after = slot_events(team, members, D, daily=opponent)
            C, ch = slot_dp(team, members, D, base_of(s, members), fixed0,
                            skip0=occ is not None and fixed0 is None, events=events, after=after)
            cache[key] = (C[0][0], C, ch, events, after)
        return cache[key]

    def can(p, s):
        return fits(team.slots[s], team.positions.get(p, []))

    free = [p for p in pool if p not in bound]
    if opponent:
        for s in open_:
            if sval(s, bind[s])[0] >= 1.0 or not free:
                continue
            best = max(((sval(s, bind[s] + [p])[0], p) for p in free if can(p, s)), default=None)
            if best and best[0] >= 1.0:
                bind[s].append(best[1])
                free.remove(best[1])
    else:
        for _ in range(60):
            cur = {s: sval(s, bind[s])[0] for s in open_}
            best_gain, best_move = 1e-6, None
            for p in free:
                for s in open_:
                    if can(p, s):
                        g = sval(s, bind[s] + [p])[0] - cur[s]
                        if g > best_gain:
                            best_gain, best_move = g, ("add", p, None, s)
            for a in open_:
                for p in bind[a]:
                    if p in fixed:
                        continue
                    rest_a = [q for q in bind[a] if q != p]
                    for b in open_:
                        if b == a or not can(p, b):
                            continue
                        g = sval(a, rest_a)[0] + sval(b, bind[b] + [p])[0] - cur[a] - cur[b]
                        if g > best_gain:
                            best_gain, best_move = g, ("move", p, a, b)
                        for q in bind[b]:
                            if q in fixed or not can(q, a):
                                continue
                            g = (sval(a, rest_a + [q])[0] + sval(b, [r for r in bind[b] if r != q] + [p])[0]
                                 - cur[a] - cur[b])
                            if g > best_gain:
                                best_gain, best_move = g, ("swap", p, a, b, q)
            if best_move is None:
                break
            kind = best_move[0]
            if kind == "add":
                _k, p, _a, s = best_move
                bind[s].append(p)
                free.remove(p)
            elif kind == "move":
                _k, p, a, b = best_move
                bind[a].remove(p)
                bind[b].append(p)
            else:
                _k, p, a, b, q = best_move
                bind[a].remove(p)
                bind[b].remove(q)
                bind[a].append(q)
                bind[b].append(p)
    C, choice, base, events, after = {}, {}, {}, {}, {}
    total = 0.0
    for s in open_:
        v, Cs, ch, events[s], after[s] = sval(s, bind[s])
        C[s], choice[s], base[s] = Cs, ch, base_of(s, bind[s])
        total += v
    return Plan(bind, C, choice, base, total, events, after)


def _risk_variant(team, r):
    """Shift conditional samples using the mixture's spread, including DNP probability.
    Only lineup selection uses these variants; simulation uses the real draws.
    """
    if r == 0:
        return team
    t = copy.copy(team)
    def shifted(dr):
        p = dr.p_play
        sd = np.sqrt(max(0.0, p * float(np.square(dr.samples).mean()) - (p * dr.mean) ** 2))
        return Draw(p, dr.samples + r * sd / max(p, 0.05), dr.tip_ms)

    t.games = {p: {d: shifted(dr) for d, dr in g.items()}
               for p, g in team.games.items()}
    return t


def realize(team, plan, D):
    """Keep the event policy, recomputing every branch with the real draws."""
    C = {}
    for s, ch in plan.choice.items():
        nxt = (plan.base[s], True)
        Cs = [None] * (len(ch) + 1)
        Cs[-1] = nxt
        for i in range(len(ch) - 1, -1, -1):
            if ch[i] is not None:
                d = plan.events[s][i][1]
                nxt = (look_value(team.games[ch[i]][d], Cs, plan.after[s][i], team.lock_at.get(ch[i])), False)
            Cs[i] = nxt
        C[s] = Cs
    return Plan(plan.bind, C, plan.choice, plan.base, sum(C[s][0][0] for s in C), plan.events, plan.after)


_CRN = {}


def _draw(pid, d, dr, n, seed):
    """The same random outcome for (player, game day, path) in every alternative evaluated."""
    key = (pid, d, n, seed, len(dr.samples))
    if key not in _CRN:
        if len(_CRN) > 20000:
            _CRN.clear()
        rng = np.random.default_rng([seed, zlib.crc32(str(pid).encode()), d])
        _CRN[key] = (rng.random(n), rng.integers(0, len(dr.samples), n))
    u, idx = _CRN[key]
    return dr.samples[idx], u < dr.p_play


def simulate(team, plan, locks, n, seed, tilt=0.0):
    """Monte Carlo week totals following `plan` after `locks`, chronologically per slot.

    `tilt` shifts model thresholds: positive holds out for bigger games (worth it
    when behind), negative locks earlier (when ahead). Owner thresholds stay fixed.
    """
    total = np.full(n, sum(team.locked.values()) + sum(team.pending[s][1] for s in locks), dtype=float)
    for s, ch in plan.choice.items():
        v = np.full(n, plan.base[s])
        done = np.zeros(n, dtype=bool)
        cursor = np.zeros(n, dtype=int)
        for i, p in enumerate(ch):
            if p is None:
                continue
            d = plan.events[s][i][1]
            x, played = _draw(p, d, team.games[p][d], n, seed)
            normal, delayed = plan.after[s][i]
            # Independent of scoring, shared across alternatives for this player/game.
            rng = np.random.default_rng([seed, zlib.crc32(str(p).encode()), d, 771])
            nxt = np.where(rng.random(n) < HANDOFF_P, normal, delayed) if normal != delayed else np.full(n, normal)
            c = np.array([v[0] for v in plan.C[s]])[nxt]
            terminal = np.array([v[1] for v in plan.C[s]])[nxt]
            active = ~done & (cursor <= i)
            threshold = team.lock_at.get(p)
            score = np.where(played, x, 0.0)
            enough = played & (x >= c + tilt) if threshold is None else score >= threshold
            take = active & (terminal | enough)
            v = np.where(take, score, v)
            done |= take
            cursor = np.where(active, nxt, cursor)
        total += v
    return total


def win_prob(mine, opp):
    return float((mine > opp).mean() + 0.5 * (mine == opp).mean())


def opponent_locks(opp, D):
    """Their pending games: assume they lock when locking beats passing on expected points."""
    locks = set()
    for s in sorted(opp.pending, key=lambda s: -opp.pending[s][1]):
        x = opp.pending[s][1]
        if make_plan(opp, D, locks | {s}, opponent=True).value + x >= make_plan(opp, D, locks, opponent=True).value:
            locks.add(s)
    return sorted(locks)


def opponent_totals(opp, D, n, seed):
    locks = opponent_locks(opp, D)
    return simulate(opp, make_plan(opp, D, locks, opponent=True), locks, n, seed)


@dataclass
class Decision:
    locks: list            # slot indexes to lock now
    passes: list           # slot indexes whose pending game we let go
    plan: Plan
    p_win: float
    p_win_alt: dict        # slot index -> p_win had we flipped that decision
    cont: dict             # slot index -> open value of the slot when the decision was made
    mean_total: float
    opp_mean: float

    @property
    def assign_today(self):
        out = {}
        for s in self.plan.choice:
            first = next(self.plan.looks(s), None)
            if first and first[2] == 0:
                out[s] = first[1]
        return out


def decide(team, opp, D, n=8000, seed=0, flip_margin=0.01, risk=(0.0,), forbidden_locks=()):
    if isinstance(D, (list, tuple)):
        D = len(D)
    theirs = opponent_totals(opp, D, n, seed + 1)
    locks, cont = set(), {}
    lockable = set(team.pending) - set(forbidden_locks)
    controlled = {s for s, (p, _x, _g) in team.pending.items() if p in team.lock_at}
    forced = {s for s in controlled & lockable if team.pending[s][1] >= team.lock_at[team.pending[s][0]]}
    for s in sorted(team.pending, key=lambda s: -team.pending[s][1]):
        x = team.pending[s][1]
        v_pass = make_plan(team, D, locks).value
        v_lock = make_plan(team, D, locks | {s}).value
        cont[s] = v_pass - v_lock
        if s in forced or (s in lockable - controlled and v_lock + x >= v_pass):
            locks.add(s)

    def evaluate(lk):
        """Best P(win) over lineup variants (who plays where) and future lock thresholds."""
        best = None
        for r in risk:
            plan = make_plan(team, D, lk) if r == 0 else realize(team, make_plan(_risk_variant(team, r), D, lk), D)
            for tilt in TILTS:
                mine = simulate(team, plan, sorted(lk), n, seed, tilt=tilt)
                pw = win_prob(mine, theirs)
                if best is None or pw > best[0] + 1e-12:
                    best = (pw, plan, mine)
        return best

    best, plan, mine = evaluate(locks)
    alt = {}
    for _ in range(2):
        changed = False
        for s in sorted(lockable - controlled):
            trial = locks ^ {s}
            pw = evaluate(trial)[0]
            alt[s] = pw
            if pw > best + flip_margin:
                locks = trial
                best, plan, mine = evaluate(locks)
                changed = True
        if not changed:
            break
    return Decision(sorted(locks), sorted(set(team.pending) - locks), plan, best, alt, cont,
                    float(mine.mean()), float(theirs.mean()))


def lineup_today(team, dec, today_has_game):
    """Positional starters list for the next tip. Locked, held and newly locked players stay;
    today's planned players take their slots; any other slot keeps a player with no game
    today (harmless, and a passed final game stays countable) or goes empty, so nobody
    plays an unplanned game in a starting slot."""
    starters = list(team.starters)
    keep = set(team.locked) | set(team.inflight) | set(dec.locks)
    placed = {starters[s] for s in keep}
    today = {s: p for s, p in dec.assign_today.items() if s not in keep}
    for s, p in today.items():
        starters[s] = p
        placed.add(p)
    for s in range(len(starters)):
        if s in keep or s in today:
            continue
        p = starters[s]
        ok = p != "0" and p in team.positions and p not in placed and not today_has_game(p)
        if not ok:
            spare = [q for q in dec.plan.bind.get(s, []) if q not in placed and q not in team.frozen and not today_has_game(q)
                     and fits(team.slots[s], team.positions.get(q, []))]
            p = spare[0] if spare else "0"
        starters[s] = p
        if p != "0":
            placed.add(p)
    return starters


def respecify_locks(team, starters, locks):
    """Move each player being locked into the most specific open slot he fits, swapping
    the occupant out to the slot he leaves when that occupant fits it and is not frozen."""
    starters = list(starters)
    locks = list(locks)
    fixed = set(team.locked) | set(team.inflight)
    out = []
    for s in locks:
        p = starters[s]
        best = s
        for t in sorted(range(len(starters)), key=lambda t: SPECIFICITY[team.slots[t]]):
            if SPECIFICITY[team.slots[t]] >= SPECIFICITY[team.slots[s]]:
                break
            if t in fixed or t in locks or t in out or not fits(team.slots[t], team.positions[p]):
                continue
            q = starters[t]
            if q != "0" and (q in team.frozen or not fits(team.slots[s], team.positions.get(q, []))):
                continue
            best = t
            break
        if best != s:
            starters[s], starters[best] = starters[best], starters[s]
        out.append(best)
    return starters, out
