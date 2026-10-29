"""The guarantees the runner makes, each from a failure an independent review reproduced."""
import json
import time

import numpy as np
import pytest

from sleeper_gm import api, auth, config, cycle, notify, review, week
from sleeper_gm.policy import Draw, Team

from test_public import fake_jwt, home  # noqa: F401  (fixture)


def test_second_run_on_a_league_is_refused(home):
    a = cycle.Ctx(config.load(), "77", 1, False, None)
    b = cycle.Ctx(config.load(), "77", 1, False, None)
    with a.exclusive():
        with pytest.raises(cycle.Busy):
            with b.exclusive():
                pass
    with b.exclusive():  # free again once the first run ends
        pass


def test_token_never_survives_into_an_error(home):
    tok = fake_jwt()
    auth.save(tok)
    auth.token()
    e = api.SleeperError(f"HTTP 401 https://sleeper.com/graphql: bad header {tok}")
    assert tok not in str(e) and "<token>" in str(e)
    assert tok not in api.redact(json.dumps({"authorization": tok}))


@pytest.mark.parametrize("command,delivered", [("cat > /dev/null", True), ("exit 3", False)])
def test_notify_reports_real_delivery(home, capsys, command, delivered):
    cfg = config.load()
    cfg["notify"]["command"] = command
    errors = []
    assert notify.send(cfg, "hello", log=errors.append) is delivered
    assert ("hello" in capsys.readouterr().out) is (not delivered)  # undelivered messages still show up
    assert bool(errors) is (not delivered)


@pytest.mark.parametrize("current_week,persisted", [(3, True), (2, False)])
def test_review_records_only_finished_weeks_and_only_once(home, monkeypatch, current_week, persisted):
    legs = [{"roster_id": 1, "matchup_id": 1, "points": 200, "max_points": 250, "starters_games": {}},
            {"roster_id": 2, "matchup_id": 1, "points": 190, "max_points": 230, "starters_games": {}}]
    monkeypatch.setattr(api, "legs", lambda league, wk: legs)
    monkeypatch.setattr(api, "rest", lambda path, fresh=False: {"leg": current_week})
    review.review("88", 1, 2, persist=True)
    review.review("88", 1, 2, persist=True)
    f = config.league_state("88") / "review.jsonl"
    rows = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
    assert len(rows) == (1 if persisted else 0)


class FakeWk:
    def __init__(self, tip_in_ms, state_dir):
        now = int(time.time() * 1000)
        self.week, self.days, self.now_ms, self.state_dir = 2, ["d0"], now, state_dir
        self.season = "2026"
        self.games = [week.Game("g1", "yesterday", now - 86400_000, "complete", "AAA", "BBB"),
                      week.Game("g2", "today", now + tip_in_ms, "pre_game", "AAA", "BBB")]
        self.by_team = {"AAA": self.games}
        self._tip = now + tip_in_ms
        self.extra = {}

    def next_tip(self, p):
        return self._tip

    def player_games(self, p):
        return self.by_team["AAA"]

    def has_game_today(self, p):
        return False

    def name(self, p):
        return p


def one_lock_team(uncertain):
    t = Team(["UTIL"], ["p"], {"p": ["SF"]}, pending={0: ("p", 45.0, "g1")},
             games={"p": {0: Draw(1.0, np.full(50, 10.0))}})
    t.lock_games = {}
    t.uncertain = {0} if uncertain else set()
    return t


class FakeWriter:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def set_leg(self, league, rid, wk, starters, games=None, summary=""):
        self.calls.append(games)
        if self.error:
            raise api.SleeperError(self.error)
        return {"update_matchup_leg": {"starters": starters, "starters_games": games}}


def mock_write_reads(monkeypatch, wk):
    wk.season = "2026"
    rows = [{"game_id": g.game_id, "start_time": g.start_ms, "status": g.status,
             "metadata": {"home_team": {"team": g.home}, "away_team": {"team": g.away}}} for g in wk.games]
    monkeypatch.setattr(api, "scores", lambda *a: rows)
    monkeypatch.setattr(api, "rest", lambda path, **kw: {"leg": wk.week})
    wk.by_team = week.schedule(rows)[1]
    wk.player_games = lambda p: wk.by_team.get("AAA", [])


def run_locks(home_dir, monkeypatch, live, tip_in_ms=3600_000, uncertain=False, error=None, leg_after=None):
    ctx = cycle.Ctx(config.load(), "99", 1, False, None)
    ctx.live = live
    ctx.writer = FakeWriter(error)
    wk = FakeWk(tip_in_ms, str(ctx.dir))
    mock_write_reads(monkeypatch, wk)
    leg_after = leg_after if leg_after is not None else {"roster_id": 1, "starters": ["p"], "starters_games": {"p": "g1"}}
    monkeypatch.setattr(api, "legs", lambda league, w: [leg_after])
    opp = Team(["UTIL"], ["o"], {"o": ["SF"]}, locked={0: 10.0})
    dec, lineup, done = cycle._locks(ctx, wk, one_lock_team(uncertain), opp)
    return ctx, done


def test_paused_mode_says_would_and_sends_nothing(home, monkeypatch):
    ctx, done = run_locks(home, monkeypatch, live=False)
    assert done == {} and ctx.writer.calls == []
    assert any(a.startswith("Would lock p 45.0") for a in ctx.acted)
    assert not any(a.startswith("Locked") for a in ctx.acted)


@pytest.mark.parametrize("tip_in_ms,sent", [(30_000, True), (2_000, False)])
def test_a_lock_is_sent_until_just_before_the_tip(home, monkeypatch, tip_in_ms, sent):
    ctx, done = run_locks(home, monkeypatch, live=True, tip_in_ms=tip_in_ms,
                          leg_after={"roster_id": 1, "starters": ["p"], "starters_games": {"p": "g1"} if sent else {}})
    assert (done == {"p": "g1"}) is sent


def test_locked_only_when_the_read_back_shows_it(home, monkeypatch):
    ctx, done = run_locks(home, monkeypatch, live=True, leg_after={"roster_id": 1, "starters": ["p"], "starters_games": {}})
    assert done == {} and any("DID NOT STICK" in a for a in ctx.acted)


@pytest.mark.parametrize("error,remembered", [
    ("Player was not in starting lineup OR was moved to bench since game began", True),
    ("HTTP 502 https://sleeper.com/graphql: bad gateway", False),
])
def test_only_an_eligibility_refusal_is_remembered(home, monkeypatch, error, remembered):
    ctx, done = run_locks(home, monkeypatch, live=True, uncertain=True, error=error,
                          leg_after={"roster_id": 1, "starters": ["p"], "starters_games": {}})
    wk = FakeWk(0, str(ctx.dir))
    assert ("p:g1" in week.unlockable(wk)) is remembered
    assert done == {}


@pytest.mark.parametrize("backend", ["fcntl", "exclusive_file"])
def test_league_exclusion_across_processes(home, monkeypatch, backend):
    import os
    import subprocess
    import sys
    if backend == "exclusive_file": monkeypatch.setitem(sys.modules, "fcntl", None)
    a = cycle.Ctx(config.load(), "77", 1, False, None)
    script = """
import sys
from sleeper_gm import cycle, config
if sys.argv[1] == 'exclusive_file': sys.modules['fcntl'] = None
ctx = cycle.Ctx(config.load(), '77', 1, False, None)
try:
    with ctx.exclusive(): pass
except cycle.Busy:
    sys.exit(23)
"""
    env = {**os.environ, "SLEEPER_GM_STATE_DIR": str(config.STATE_DIR)}
    with a.exclusive():
        result = subprocess.run([sys.executable, "-c", script, backend], env=env, capture_output=True, text=True)
        assert result.returncode == 23, result.stderr
    result = subprocess.run([sys.executable, "-c", script, backend], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("age,alive,stale", [(0, True, False), (1201, True, False), (7200, True, False), (7201, True, True), (0, False, True), (1201, False, True)])
def test_fallback_lock_requires_dead_pid_or_two_hours(home, monkeypatch, age, alive, stale):
    import os
    import sys
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setattr(cycle.time, "time", lambda: 10000)
    def kill(pid, signal):
        assert (pid, signal) == (12345, 0)
        if not alive: raise ProcessLookupError()
    monkeypatch.setattr(cycle.os, "kill", kill)
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    path = ctx.dir / "run.lock"
    path.write_text(json.dumps({"pid": 12345, "t": 10000 - age}))
    if stale:
        with ctx.exclusive():
            data = json.loads(path.read_text())
            assert data == {"pid": os.getpid(), "t": 10000}
        assert not path.exists()
    else:
        with pytest.raises(cycle.Busy):
            with ctx.exclusive(): pass
        assert json.loads(path.read_text()) == {"pid": 12345, "t": 10000 - age}


@pytest.mark.parametrize("state,protected,warn", [("pending", False, True), ("locked", False, True), ("starting", False, True), ("bench", False, False), ("bench", True, True)])
def test_pending_claim_warns_for_starters(home, monkeypatch, state, protected, warn):
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    wk = FakeWk(100000, str(ctx.dir))
    wk.protected = {"p"} if protected else set()
    me = one_lock_team(False)
    if state != "pending": me.pending = {}
    if state == "locked": me.lock_games = {"p": "g1"}
    if state == "bench": me.starters = ["q"]
    queries = []
    def gql(q, auth):
        assert auth
        queries.append(q)
        return {"league_transactions": [{"drops": {"p": 1}}]}
    monkeypatch.setattr(api, "gql", gql)
    assert cycle._claims_check(ctx, wk, me) == {"p"}
    assert bool(ctx.acted) is warn
    assert all("NEEDS YOU" in a for a in ctx.acted)
    assert len(queries) == 1 and "mutation" not in queries[0]


@pytest.mark.parametrize("status,seconds,blocked", [("in_game", -3600, True), ("pre_game", 59, True), ("pre_game", 60, True), ("pre_game", 61, False), ("complete", -3600, False)])
def test_ir_benching_rechecks_tip_before_write(home, monkeypatch, status, seconds, blocked):
    from test_moves import WK, team
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    ctx.live = True
    wk = WK({"p": "Out", "q": None}, {"p": 20, "q": 20})
    wk.rosters = {1: {"players": ["p", "q"], "reserve": []}}
    wk.now_ms = 1  # deliberately stale; the guard must read the clock at the write
    game = week.Game("g", "today", 1_000_000 + seconds * 1000, status, "AAA", "BBB")
    wk.games = [game]
    mock_write_reads(monkeypatch, wk)
    me = team(["p", "q"])
    monkeypatch.setattr(cycle.time, "time", lambda: 1000)
    calls = []
    class Writer:
        def set_leg(self, *args, **kw): calls.append("bench")
        def set_reserve(self, lid, rid, reserve, **kw):
            calls.append("reserve")
            wk.rosters[1]["reserve"] = reserve
    ctx.writer = Writer()
    monkeypatch.setattr(cycle, "load", lambda ctx: (wk, me, None))
    cycle._ir(ctx, wk, me)
    assert calls == ([] if blocked else ["bench", "reserve"])
    assert any("skip" in a.lower() for a in ctx.acted) is blocked


@pytest.mark.parametrize("uncertain,committed", [(False, True), (False, False), (True, True), (True, False)])
def test_lock_timeout_reconciles_all_attempts(home, monkeypatch, uncertain, committed):
    leg = {"roster_id": 1, "starters": ["p"], "starters_games": {"p": "g1"} if committed else {}}
    ctx, confirmed = run_locks(home, monkeypatch, live=True, uncertain=uncertain, error="timeout", leg_after=leg)
    assert confirmed == ({"p": "g1"} if committed else {})
    assert len(ctx.writer.calls) == 1
    assert any(a.startswith("Locked p") for a in ctx.acted) is committed
    rows = [json.loads(l) for l in (ctx.dir / "snapshots.jsonl").read_text().splitlines()]
    assert rows[-1]["starters"] == ["p"] and rows[-1]["source"] == "read-back"


def _snapshot_for_cycle(ctx, wk, me, opp, dec, starters, done):
    return {"p_win": None, "acted": ctx.acted, "league": {"name": "L"},
            "me": {"proj": None, "points": 0, "slots": []}, "opp": {"proj": None, "points": 0}}


@pytest.mark.parametrize("healthy,urgent", [(True, True), (False, True), (True, False), (False, False)])
def test_cycle_unfreezes_before_urgent_locks_then_parks_ir(home, monkeypatch, healthy, urgent):
    from test_moves import WK, team
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    ctx.live = True
    wk = WK({"p": None, "hurt": "Out", "back": None if healthy else "Out"}, {"p": 30, "hurt": 20, "back": 25})
    wk.league = {**wk.league, "settings": {**wk.league["settings"], "reserve_slots": 2}}
    wk.rosters = {1: {"players": ["p", "hurt", "back"], "reserve": ["back"]}}
    wk.next_tip = lambda p: 1000 if urgent else 10_000_000
    wk.player_games = lambda p: []
    mock_write_reads(monkeypatch, wk)
    me = team(["p", "hurt"])
    me.pending = {0: ("p", 45., "g")}
    events = []
    class Writer:
        def set_leg(self, lid, rid, w, starters, **kw):
            events.append("bench")
            me.starters = starters
        def set_reserve(self, lid, rid, reserve, **kw):
            events.append("unfreeze" if "back" not in reserve and "hurt" not in reserve else "park")
            wk.rosters[1]["reserve"] = reserve
    ctx.writer = Writer()
    monkeypatch.setattr(cycle, "load", lambda ctx: (wk, me, None))
    monkeypatch.setattr(cycle, "_claims_check", lambda *a: set())
    def locks(*args):
        assert not (healthy and "back" in wk.rosters[1]["reserve"])
        events.append("locks")
        return None, me.starters, {}
    monkeypatch.setattr(cycle, "_locks", locks)
    def moves(*args):
        events.append("moves")
        return wk, me, None
    monkeypatch.setattr(cycle, "_moves", moves)
    monkeypatch.setattr(cycle, "snapshot", _snapshot_for_cycle)
    monkeypatch.setattr(notify, "send", lambda *a, **kw: True)
    cycle.run_league(ctx)
    assert events == (["unfreeze"] if healthy else []) + (["locks"] if urgent else []) + ["bench", "park", "moves", "locks"]


@pytest.mark.parametrize("channel,success,expected", [(None, False, True), ("command", False, False), ("command", True, True), ("telegram_chat_id", False, False), ("ntfy_topic", True, True)])
def test_run_league_reports_brief_delivery(home, monkeypatch, capsys, channel, success, expected):
    from types import SimpleNamespace
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    if channel: ctx.cfg["notify"][channel] = "configured"
    wk, me = FakeWk(10000, str(ctx.dir)), one_lock_team(False)
    monkeypatch.setattr(cycle, "load", lambda ctx: (wk, me, None))
    monkeypatch.setattr(cycle, "_ir", lambda ctx, wk, me, **kw: (wk, me))
    monkeypatch.setattr(cycle, "_claims_check", lambda *a: set())
    monkeypatch.setattr(cycle, "_moves", lambda ctx, wk, me, opp: (wk, me, opp))
    monkeypatch.setattr(cycle, "_locks", lambda *a: (None, ["p"], {}))
    monkeypatch.setattr(cycle, "snapshot", _snapshot_for_cycle)
    monkeypatch.delenv("SLEEPER_GM_TELEGRAM_TOKEN", raising=False)
    if channel == "command":
        monkeypatch.setattr(notify.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0 if success else 1))
    if channel == "ntfy_topic":
        monkeypatch.setattr(notify.urllib.request, "urlopen", lambda *a, **kw: None)
    snap = cycle.run_league(ctx, brief=True)
    assert snap["notified"] is expected
    assert ("L, week 2" in capsys.readouterr().out) is (not success)


@pytest.mark.parametrize("start_week,bad_kind", [(None, "missing"), (2, "bye"), (3, "error"), (2, "null_matchup")])
def test_backfill_skips_bad_weeks_and_continues(home, monkeypatch, caplog, start_week, bad_kind):
    first = start_week or 1
    bad_week, good_week = first, first + 1
    settings = {} if start_week is None else {"start_week": start_week}
    def rest(path, **kw):
        return {"leg": good_week + 1} if path == "/v1/state/nba" else {"settings": settings}
    monkeypatch.setattr(api, "rest", rest)
    calls = []
    def legs(lid, w):
        calls.append(w)
        if w == bad_week:
            if bad_kind == "error": raise api.SleeperError("bad week")
            if bad_kind == "missing": return [{"roster_id": 2, "matchup_id": 4}]
            if bad_kind == "null_matchup": return [{"roster_id": 1, "matchup_id": None}, {"roster_id": 2, "matchup_id": None}]
            return [{"roster_id": 1, "matchup_id": 4}]
        return [{"roster_id": 1, "matchup_id": 4, "points": 50, "max_points": 60}, {"roster_id": 2, "matchup_id": 4, "points": 40, "max_points": 55}]
    monkeypatch.setattr(api, "legs", legs)
    path = config.league_state("77") / "logs" / "decisions-test.jsonl"
    path.write_text(json.dumps({"week": good_week, "p_win": 0.6}) + "\n")
    results = review.backfill("77", 1)
    assert len(results) == (1 if bad_kind == "error" else 2)
    assert "WON 50.0 to 40.0" in results[-1][1]
    assert calls == [bad_week, good_week]
    assert not (config.league_state("77") / "review.jsonl").exists()
    for w, text, row in results:
        assert row["week"] == w
        review.record_review("77", row)
    rows = [json.loads(line) for line in (config.league_state("77") / "review.jsonl").read_text().splitlines()]
    if bad_kind != "error": assert rows[0]["week"] == bad_week and rows[0]["skipped"]
    assert rows[-1]["week"] == good_week and rows[-1]["won"] == 1.0
    assert str(bad_week) in caplog.text
    assert review.backfill("77", 1) == []
    assert calls == [bad_week, good_week] + ([bad_week] if bad_kind == "error" else [])


@pytest.mark.parametrize("contents", ["valid", "empty"])
def test_fallback_stale_recovery_has_one_owner(home, monkeypatch, contents):
    import os
    import sys
    import threading
    from pathlib import Path
    monkeypatch.setitem(sys.modules, "fcntl", None)
    a = cycle.Ctx(config.load(), "77", 1, False, None)
    b = cycle.Ctx(config.load(), "77", 1, False, None)
    path = a.dir / "run.lock"
    old = time.time() - 7201
    path.write_text(json.dumps({"pid": 1, "t": old}) if contents == "valid" else "")
    os.utime(path, (old, old))
    paused, resume, release = threading.Event(), threading.Event(), threading.Event()
    a_done, b_done = threading.Event(), threading.Event()
    original_unlink = Path.unlink
    first = True
    def unlink(p, *args, **kw):
        nonlocal first
        if p == path and threading.current_thread().name == "reclaimer-a" and first:
            first = False
            paused.set()
            assert resume.wait(3)
        return original_unlink(p, *args, **kw)
    monkeypatch.setattr(Path, "unlink", unlink)
    owners, errors = [], []
    def run(ctx, done):
        try:
            with ctx.exclusive():
                owners.append(threading.current_thread().name)
                done.set()
                assert release.wait(3)
        except cycle.Busy:
            done.set()
        except Exception as e:
            errors.append(e)
            done.set()
    ta = threading.Thread(target=run, name="reclaimer-a", args=(a, a_done))
    tb = threading.Thread(target=run, name="reclaimer-b", args=(b, b_done))
    ta.start()
    try:
        assert paused.wait(3)
        tb.start()
        b_done.wait(0.2)  # the old code lets B acquire while A is between its stat and unlink
        resume.set()
        assert a_done.wait(3) and b_done.wait(3)
    finally:
        resume.set()
        release.set()
        ta.join(3)
        if tb.ident: tb.join(3)
    assert not errors
    assert owners == ["reclaimer-a"]


@pytest.mark.parametrize("elapsed,reclaimed", [(1201, False), (7200, False), (7201, True)])
def test_expired_lock_owner_cannot_release_successor(home, monkeypatch, elapsed, reclaimed):
    import sys
    monkeypatch.setitem(sys.modules, "fcntl", None)
    clock = {"t": 10000}
    monkeypatch.setattr(cycle.time, "time", lambda: clock["t"])
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    first = ctx.exclusive()
    first.__enter__()
    clock["t"] += elapsed
    second = ctx.exclusive()
    if not reclaimed:
        try:
            with pytest.raises(cycle.Busy): second.__enter__()
        finally:
            first.__exit__(None, None, None)
    else:
        second.__enter__()
        try:
            first.__exit__(None, None, None)
            with pytest.raises(cycle.Busy):
                with ctx.exclusive(): pass
        finally:
            second.__exit__(None, None, None)


@pytest.mark.parametrize("result,counted", [("ok", True), ("ok (confirmed after error)", True),
                                            ("error: At least one of the players being added in is on waivers.", False),
                                            ("dry-run", False)])
def test_only_accepted_transactions_count_against_the_cap(home, result, counted):
    ctx = cycle.Ctx(config.load(), "66", 1, True, None)
    ctx.append("writes", {"t": 1, "kind": "free_agent", "live": True, "result": result})
    assert ctx.transactions_today() == (1 if counted else 0)


def test_waiver_refusals_are_remembered_for_the_day(home):
    ctx = cycle.Ctx(config.load(), "66", 1, True, None)
    assert ctx.on_waivers() == set()
    ctx.mark_on_waivers("a")
    ctx.mark_on_waivers("b")
    assert ctx.on_waivers() == {"a", "b"}


@pytest.mark.parametrize("age_h,skipped", [(1, True), (5.9, True), (6.1, False)])
def test_waiver_skip_expires_so_morning_runs_retry(home, monkeypatch, age_h, skipped):
    ctx = cycle.Ctx(config.load(), "66", 1, True, None)
    now = time.time()
    monkeypatch.setattr(cycle.time, "time", lambda: now - age_h * 3600)
    ctx.mark_on_waivers("a")
    monkeypatch.setattr(cycle.time, "time", lambda: now)
    assert ("a" in ctx.on_waivers()) is skipped
