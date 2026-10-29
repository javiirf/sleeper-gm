"""Offline regressions for the explicit fourth-review decisions."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sleeper_gm import api, cli, config, crowd, cycle, notify, policy, review, schedule, web, week
from sleeper_gm.data import Store
from sleeper_gm.policy import Draw, Team
from test_public import _cli_run_setup, _node, home  # noqa: F401
from test_safety import FakeWk, FakeWriter, one_lock_team


@pytest.mark.parametrize("rows", ["empty", "previous", "current"])
@pytest.mark.parametrize("last_age", [None, 300, 1201])
def test_run_history_order_depends_on_current_season(home, monkeypatch, rows, last_age):
    refresh = cli.ensure_history
    args = _cli_run_setup(monkeypatch)
    st = Store(config.CACHE_DIR)
    if rows != "empty":
        st.db.execute("INSERT INTO games (season, game_id) VALUES (?, 'g')", ("2026" if rows == "current" else "2025",))
    monkeypatch.setattr(cli, "store", lambda: st)
    monkeypatch.setattr(api, "rest", lambda *a, **kw: {"season": "2026", "leg": 7})
    monkeypatch.setattr(cli.time, "time", lambda: 10000)
    config.STATE_DIR.mkdir()
    path = config.STATE_DIR / "run.json"
    path.write_text(json.dumps({"last_sync": 10000 - last_age} if last_age is not None else {}))
    events, syncs = [], []

    def history(*args):
        events.append("history")
        return refresh(*args)

    def run(ctx, brief):
        events.append(ctx.league_id)
        return {"p_win": 0.5, "acted": [], "notified": True}

    monkeypatch.setattr(cli, "ensure_history", history)
    monkeypatch.setattr(st, "sync", lambda season, weeks: syncs.append((season, list(weeks))))
    monkeypatch.setattr(cycle, "run_league", run)
    cli.cmd_run(args)
    assert events == (["a", "b", "history"] if rows == "current" else ["history", "a", "b"])
    assert bool(syncs) is (last_age != 300)
    if syncs:
        assert syncs[-1] == ("2026", [7])
    assert json.loads(path.read_text())["last_sync"] == (9700 if last_age == 300 else 10000)
    st.db.close()


@pytest.mark.parametrize("location", ["interpreter", "cwd"])
@pytest.mark.parametrize("part,refused", [("plain", False), ("space and 'quote", False), ("bad%Q", True), ("bad\npath", True), ("bad\rpath", True), ("trailing\\", True)])
def test_schedule_install_refuses_unsupported_paths(home, monkeypatch, location, part, refused):
    cwd = home / (part if location == "cwd" else "project")
    interpreter = str(home / part) if location == "interpreter" else "/bin/true"
    monkeypatch.setattr(schedule, "__file__", str(cwd / "sleeper_gm" / "schedule.py"))
    monkeypatch.setattr(schedule, "_cmd", lambda: [interpreter, "-m", "sleeper_gm.cli", "run"])
    monkeypatch.setattr(schedule, "_has_systemd", lambda: True)
    monkeypatch.setattr(schedule, "_systemd_dir", lambda: home / "units")
    calls = []
    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **kw: calls.append(a))
    if refused:
        with pytest.raises(ValueError, match="--cron"):
            schedule.install()
        assert not calls and not (home / "units").exists()
    else:
        assert "installed systemd" in schedule.install()
        assert len(calls) == 2


@pytest.mark.parametrize("part", ["bad%Q", "trailing\\"])
def test_schedule_cron_accepts_native_scheduler_path_exceptions(home, monkeypatch, part):
    monkeypatch.setattr(schedule, "_cmd", lambda: [str(home / part)])
    calls = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="")
    monkeypatch.setattr(schedule.subprocess, "run", run)
    assert "installed crontab" in schedule.install(cron=True)
    assert calls[-1][0] == (["crontab", "-"],)


@pytest.mark.parametrize("opponent,choice,pwin", [(45, "gamble", 0.5), (30, "sure", 1.0)])
def test_lineup_variants_include_participation_risk(opponent, choice, pwin):
    me = Team(["UTIL"], ["0"], {"sure": ["SF"], "gamble": ["SF"]}, games={
        "sure": {0: Draw(1., np.full(50, 40.))}, "gamble": {0: Draw(0.5, np.full(50, 60.))}})
    opp = Team(["UTIL"], ["o"], {"o": ["SF"]}, locked={0: opponent})
    dec = policy.decide(me, opp, 1, n=8000, risk=policy.RISK)
    assert dec.assign_today == {0: choice}
    assert dec.p_win == pytest.approx(pwin, abs=0.03)
    assert dec.plan.value == (30 if choice == "gamble" else 40)


@pytest.mark.parametrize("p", [0., 0.01, 0.05, 0.5, 1.])
@pytest.mark.parametrize("r", [-0.5, 0., 1.5])
def test_participation_variant_uses_mixture_sd(p, r):
    samples = np.array([20., 60.])
    me = Team(["UTIL"], ["p"], {"p": ["SF"]}, games={"p": {0: Draw(p, samples)}})
    variant = policy._risk_variant(me, r).games["p"][0]
    sd = np.sqrt(p * 2000 - (p * 40) ** 2)
    assert variant.samples == pytest.approx(samples + r * sd / max(p, 0.05))
    assert me.games["p"][0].samples.tolist() == [20., 60.]
    assert variant.value((0, True)) - p * 40 == pytest.approx(r * sd * p / max(p, 0.05))


@pytest.mark.parametrize("channel,delivered", [(None, False), ("command", False), ("command", True), ("telegram_chat_id", False), ("ntfy_topic", True)])
@pytest.mark.parametrize("runs", [0, 2])
def test_run_persists_reviews_only_after_delivery(home, monkeypatch, capsys, channel, delivered, runs):
    backfill = review.backfill
    args = _cli_run_setup(monkeypatch)
    args.brief = False
    monkeypatch.setattr(cli, "brief_due", lambda *a: False)
    monkeypatch.setattr(cli, "discover", lambda cfg: [("a", 1, "A")])
    monkeypatch.setattr(review, "backfill", backfill)
    monkeypatch.setattr(api, "rest", lambda path, **kw: {"season": "2026", "leg": 2} if path == "/v1/state/nba" else {"settings": {"start_week": 1}})
    monkeypatch.setattr(api, "legs", lambda *a: [{"roster_id": 1, "matchup_id": 1, "points": 50}, {"roster_id": 2, "matchup_id": 1, "points": 40}])
    monkeypatch.setattr(review, "predictions", lambda *a: [{"p_win": 0.6}] * runs)
    monkeypatch.setattr(cycle, "run_league", lambda *a, **kw: {"p_win": 0.5, "acted": [], "notified": True})
    cfg = config.load()
    if channel:
        cfg["notify"][channel] = "configured"
    monkeypatch.setattr(config, "load", lambda: cfg)
    sends = []
    real_send = notify.send

    def send(cfg, text):
        assert review.recorded("a") == set(), "persisted before notification"
        sends.append(text)
        if channel is None:
            return real_send(cfg, text)
        return delivered if len(sends) == 1 else True

    monkeypatch.setattr(notify, "send", send)
    cli.cmd_run(args)
    persisted = delivered or channel is None
    assert review.recorded("a") == ({1} if persisted else set())
    cli.cmd_run(args)
    assert len(sends) == (1 if persisted else 2)
    assert all("week 1 review: WON" in text for text in sends)
    assert review.recorded("a") == {1}
    if channel is None:
        assert "week 1 review: WON" in capsys.readouterr().out


@pytest.mark.parametrize("error,skipped", [(ValueError("no opponent"), True), (api.SleeperError("HTTP 503"), False), (ConnectionError("offline"), False), (TimeoutError("timeout"), False)])
def test_backfill_retries_transient_errors(home, monkeypatch, caplog, error, skipped):
    monkeypatch.setattr(api, "rest", lambda path, **kw: {"leg": 2} if path == "/v1/state/nba" else {"settings": {}})
    calls = []

    def fake_review(lid, rid, w, persist=False):
        assert not persist
        calls.append(w)
        if len(calls) == 1:
            raise error
        return "finished", {"week": w, "runs": 0}

    monkeypatch.setattr(review, "review", fake_review)
    first = review.backfill("77", 1)
    assert not review.recorded("77")
    assert bool(first) is skipped
    for w, text, row in first:
        assert row["skipped"]
        review.record_review("77", row)
    second = review.backfill("77", 1)
    assert calls == ([1] if skipped else [1, 1])
    assert second == ([] if skipped else [(1, "finished", {"week": 1, "runs": 0})])
    assert ("Skipping" if skipped else "Retrying") in caplog.text


def cycle_summary(ctx, wk, me, opp, dec, starters, done):
    return {"p_win": dec.p_win if dec else None, "acted": ctx.acted, "league": {"name": "L"},
            "me": {"proj": 45, "points": 0, "slots": []}, "opp": {"name": "O", "proj": 10, "points": 0}}


@pytest.mark.parametrize("urgent", [False, True])
@pytest.mark.parametrize("claim,excluded", [("ours", True), ("theirs", False), ("trade", False), ("error", False)])
def test_pending_waiver_drops_are_excluded_from_all_lock_passes(home, monkeypatch, urgent, claim, excluded):
    from test_safety import mock_write_reads
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    ctx.live, ctx.writer = True, FakeWriter()
    wk = FakeWk(30000 if urgent else 7200_000, str(ctx.dir))
    wk.protected = set()
    me = one_lock_team(False)
    opp = Team(["UTIL"], ["o"], {"o": ["SF"]}, locked={0: 10})
    mock_write_reads(monkeypatch, wk)
    queries = []

    def gql(q, auth):
        queries.append(q)
        if claim == "error": raise api.SleeperError("offline")
        return {"league_transactions": [{"type": "trade" if claim == "trade" else "waiver", "drops": {"p": 2 if claim == "theirs" else 1}}]}

    monkeypatch.setattr(api, "gql", gql)
    monkeypatch.setattr(api, "legs", lambda *a: [{"roster_id": 1, "starters": ["p"], "starters_games": {"p": "g1"} if ctx.writer.calls else {}}])
    monkeypatch.setattr(cycle, "load", lambda ctx: (wk, me, opp))
    monkeypatch.setattr(cycle, "_ir", lambda ctx, wk, me, **kw: (wk, me))
    monkeypatch.setattr(cycle, "_moves", lambda ctx, wk, me, opp: (wk, me, opp))
    monkeypatch.setattr(cycle, "snapshot", cycle_summary)
    monkeypatch.setattr(notify, "send", lambda *a, **kw: True)
    cycle.run_league(ctx)
    assert len(queries) == 1
    assert bool(ctx.writer.calls) is (not excluded)
    warning = "NEEDS YOU: not locking p because your pending waiver claim drops him"
    assert (warning in ctx.acted) is excluded


@pytest.mark.parametrize("kind", ["certain", "uncertain", "lineup"])
@pytest.mark.parametrize("leg,status,seconds", [(2, "pre_game", 3600), (3, "pre_game", 3600), (2, "pre_game", 2), (2, "in_game", -1), (2, "in_game", 3600), (2, "postponed", 2), (2, "complete", -1)])
def test_lineup_and_locks_refresh_week_and_schedule(home, monkeypatch, kind, leg, status, seconds):
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    ctx.live = True
    wk = FakeWk(3600_000, str(ctx.dir))
    me = one_lock_team(kind == "uncertain")
    if kind == "lineup":
        me.starters, me.pending = ["0"], {}
    opp = Team(["UTIL"], ["o"], {"o": ["SF"]}, locked={0: 1})
    fresh = copy.deepcopy(wk.games)
    fresh[1].status, fresh[1].start_ms = status, wk.now_ms + seconds * 1000
    rows = [{"game_id": g.game_id, "start_time": g.start_ms, "status": g.status,
             "metadata": {"home_team": {"team": g.home}, "away_team": {"team": g.away}}} for g in fresh]
    events = []

    def scores(season, w):
        assert (season, w) == ("2026", 2)
        events.append("scores")
        return rows

    def state(path, fresh=False):
        assert path == "/v1/state/nba" and fresh
        events.append("state")
        return {"leg": leg}

    class Writer(FakeWriter):
        def set_leg(self, *args, **kwargs):
            assert events[:2] == ["scores", "state"]
            events.append("write")
            return super().set_leg(*args, **kwargs)

    ctx.writer = Writer()
    monkeypatch.setattr(api, "scores", scores)
    monkeypatch.setattr(api, "rest", state)
    monkeypatch.setattr(api, "legs", lambda *a: [{"roster_id": 1, "starters": ["p"], "starters_games": {"p": "g1"}}])
    cycle._locks(ctx, wk, me, opp)
    allowed = leg == 2 and status != "in_game" and (status in ("postponed", "complete") or seconds > (60 if kind == "lineup" else 5))
    if status == "complete" and kind != "lineup": allowed = False
    assert bool(ctx.writer.calls) is allowed
    assert events[:2] == ["scores", "state"]
    assert any("week rolled over" in a for a in ctx.acted) is (leg != 2)
    if leg != 2:
        cycle._locks(ctx, wk, me, opp)
        assert events == ["scores", "state"]


@pytest.mark.parametrize("leg,status,seconds,allowed", [(2, "pre_game", 61, True), (2, "pre_game", 59, False), (2, "in_game", -1, False), (2, "postponed", 1, True), (3, "pre_game", 3600, False)])
def test_ir_bench_refreshes_week_and_schedule(home, monkeypatch, leg, status, seconds, allowed):
    from test_moves import WK, team
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    ctx.live = True
    wk = WK({"p": "Out", "q": None}, {"p": 20, "q": 20})
    wk.season = "2026"
    wk.rosters = {1: {"players": ["p", "q"], "reserve": []}}
    wk.games = [week.Game("g", "today", 1000_000 + 3600_000, "pre_game", "AAA", "BBB")]
    wk.by_team = {"AAA": wk.games}
    wk.player_games = lambda p: wk.by_team.get("AAA", [])
    me = team(["p", "q"])
    events = []
    def scores(*args):
        events.append("scores")
        return [{"game_id": "g", "start_time": 1000_000 + seconds * 1000, "status": status,
                 "metadata": {"home_team": {"team": "AAA"}, "away_team": {"team": "BBB"}}}]
    def state(*args, **kwargs):
        events.append("state")
        return {"leg": leg}
    class Writer:
        def set_leg(self, *args, **kwargs): events.append("bench")
        def set_reserve(self, lid, rid, reserve, **kwargs):
            events.append("reserve")
            wk.rosters[1]["reserve"] = reserve
    ctx.writer = Writer()
    monkeypatch.setattr(cycle.time, "time", lambda: 1000)
    monkeypatch.setattr(api, "scores", scores)
    monkeypatch.setattr(api, "rest", state)
    monkeypatch.setattr(cycle, "load", lambda ctx: (wk, me, None))
    cycle._ir(ctx, wk, me)
    assert events == ["scores", "state"] + (["bench", "reserve"] if allowed else [])
    if leg != 2:
        cycle._ir(ctx, wk, me)
        cycle._moves(ctx, wk, me, None)
        assert events == ["scores", "state"]


@pytest.mark.parametrize("dictionary_offset", [None, -3600_000, 3600_000])
@pytest.mark.parametrize("age,observed_week,kept", [(2, 2, True), (8, 1, True), (8.1, 1, False), (9, 2, True)])
def test_team_transition_cutoff_survives_dictionary_and_prunes(tmp_path, monkeypatch, dictionary_offset, age, observed_week, kept):
    from test_week import FakeModel
    now = 1793289600000
    cutoff = now - int(age * 86400_000)
    dictionary_t = cutoff + dictionary_offset if dictionary_offset is not None else 0
    players = {"p": {"team": "BBB", "team_changed_at": dictionary_t, "fantasy_positions": ["SF"]}}
    path = tmp_path / "team_moves.json"
    move = {"team": "BBB", "t": cutoff, "week": observed_week, "season": "2026"}
    path.write_text(json.dumps({"p": move}))
    league = {"season": "2026", "roster_positions": ["UTIL"], "scoring_settings": {"pts": 1.}}
    boxes = [{"player_id": "p", "game_id": "old", "stats": {"pts": 45}},
             {"player_id": "x", "game_id": "pretrade", "stats": {"pts": 10}}]
    games = [{"game_id": gid, "status": status, "start_time": tip,
              "metadata": {"home_team": {"team": team}, "away_team": {"team": "CCC"}}}
             for gid, status, tip, team in [("old", "complete", cutoff - 3 * 3600_000, "AAA"),
                                           ("pretrade", "complete", cutoff - 2 * 3600_000, "BBB"),
                                           ("future", "pre_game", now + 86400_000, "BBB")]]
    legs = [{"roster_id": 1, "matchup_id": 1, "starters": ["p"], "player_map": {"p": {"team": "BBB", "injury_status": None}}},
            {"roster_id": 2, "matchup_id": 1, "starters": []}]
    monkeypatch.setattr(api, "rest", lambda path, **kw: {"leg": 2} if path == "/v1/state/nba" else boxes if path.startswith("/stats") else league)
    monkeypatch.setattr(api, "scores", lambda *a: games)
    monkeypatch.setattr(api, "league_rosters", lambda *a: [{"roster_id": 1, "players": ["p"]}, {"roster_id": 2, "players": []}])
    monkeypatch.setattr(api, "legs_with_players", lambda *a: copy.deepcopy(legs))
    monkeypatch.setattr(week.time, "time", lambda: now / 1000)
    store = SimpleNamespace(players=lambda: players, injuries=lambda: {})
    wk = week.load(store, lambda wk: FakeModel(), "77", 1, now_ms=now, state_dir=str(tmp_path))
    assert json.loads(path.read_text()) == ({"p": move} if kept else {})
    assert wk.players["p"]["team_changed_at"] == (max(dictionary_t, cutoff) if kept else dictionary_t)
    if kept:
        assert week.build_team(wk, 1).pending[0] == ("p", 45., "old")


@pytest.mark.parametrize("age,usable", [(0, True), (6 * 86400_000, True), (7 * 86400_000, True), (7 * 86400_000 + 1, False), (None, False)])
def test_service_worker_expires_cached_demo_json(age, usable):
    source = (Path(web.__file__).with_suffix("") / "sw.js").read_text()
    script = """
const handlers = {}, removed = [];
const now = 1800000000000;
Date.now = () => now;
const age = AGE;
const headers = {'content-type': 'application/json'};
if (age !== null) headers['x-gm-cached-at'] = String(now - age);
const hit = new Response('{"value":42}', {headers});
const self = {location: {origin: 'https://gm.test'}, addEventListener: (name, cb) => handlers[name] = cb};
const caches = {open: async () => ({match: async () => hit, delete: async req => removed.push(req.url)})};
const fetch = async () => { throw new Error('offline'); };
""".replace("AGE", json.dumps(age)) + source + """
handlers.fetch({request: new Request('https://gm.test/demo/now.json'), respondWith: p => p.then(async res => {
  console.log(JSON.stringify({status: res.status, cached: res.headers.get('x-gm-cache'), body: await res.json(), removed}));
})});
"""
    result = _node(script)
    assert result["status"] == (200 if usable else 503)
    assert result["cached"] == ("1" if usable else None)
    assert bool(result["removed"]) is (not usable)
    assert result["body"] == ({"value": 42} if usable else {"error": "offline"})


@pytest.mark.parametrize("cache_fails", [False, True])
def test_service_worker_dates_new_cache_without_losing_online_response(cache_fails):
    source = (Path(web.__file__).with_suffix("") / "sw.js").read_text()
    script = """
const handlers = {}, now = 1800000000000;
Date.now = () => now;
let saved;
const self = {location: {origin: 'https://gm.test'}, addEventListener: (name, cb) => handlers[name] = cb};
const caches = {open: async () => ({put: async (req, res) => {
  saved = {t: res.headers.get('x-gm-cached-at'), body: await res.json()};
  if (CACHE_FAILS) throw new Error('quota exceeded');
}})};
const fetch = async () => new Response('{"value":42}', {headers: {'cache-control': 'no-store'}});
""".replace("CACHE_FAILS", json.dumps(cache_fails)) + source + """
handlers.fetch({request: new Request('https://gm.test/demo/now.json'), respondWith: p => p.then(async res => {
  console.log(JSON.stringify({status: res.status, body: await res.json(), saved}));
})});
"""
    result = _node(script)
    assert result == {"status": 200, "body": {"value": 42}, "saved": {"t": "1800000000000", "body": {"value": 42}}}


@pytest.mark.parametrize("age,usable", [(1, True), (7 * 86400_000, True), (7 * 86400_000 + 1, False), (None, False)])
@pytest.mark.parametrize("layer", ["localStorage", "service_worker"])
def test_app_cache_expiry_preserves_newer_offline_banner(age, usable, layer):
    source = (Path(web.__file__).with_suffix("") / "app.js").read_text()
    helpers = source[source.index("  function esc("):source.index("  /* ---------- data")]
    data = source[source.index("  function cacheGet("):source.index("  function load(")]
    notice = source[source.index("  function renderNotice("):source.index("  /* ---------- the win line")]
    script = """
const now = 1800000000000, age = __AGE__, layer = __LAYER__;
Date.now = () => now;
var HOUR = 3600000, DAY = 86400000, CACHE_MAX_AGE = 7 * DAY;
var S = {demo: false, fallback: false, now: null};
const banner = {}, removed = [];
var $ = () => banner;
const saved = {t: age === null ? null : now - age, data: {t: now - 3600000, value: 42}};
const localStorage = {getItem: () => layer === 'localStorage' ? JSON.stringify(saved) : null,
  removeItem: key => removed.push(key)};
const fetch = async () => {
  if (layer === 'localStorage') throw new Error('offline');
  const headers = {'x-gm-cache': '1'};
  if (age !== null) headers['x-gm-cached-at'] = String(now - age);
  return new Response(JSON.stringify(saved.data), {headers});
};
""".replace("__AGE__", json.dumps(age)).replace("__LAYER__", json.dumps(layer))
    script += helpers + data + notice + """
getJSON('now').then(r => {S.now = r.data; S.staleFrom = r.stale ? r.data.t : null;}, e => {S.error = e.kind;})
  .then(() => {renderNotice(); console.log(JSON.stringify({S, banner, removed}));});
"""
    result = _node(script)
    assert bool(result["S"]["now"]) is usable
    assert result["banner"]["hidden"] is False
    assert ("Showing the last update" in result["banner"]["innerHTML"]) is usable
    if layer == "localStorage":
        assert bool(result["removed"]) is (not usable)


def crowd_fixture(home, monkeypatch):
    from test_week import g, make_week, NOW, H
    wk = make_week([g("g1", "2026-10-28", NOW - 5 * H, "complete"), g("g2", "2026-10-30", NOW + 40 * H, "pre_game")],
                   {("a", "g1"): {"pts": 45}, ("b", "g1"): {"pts": 30}}, ["a", "b"])
    ctx = cycle.Ctx(config.load(), "77", 9, False, None)
    wk.state_dir = str(ctx.dir)
    wk.league["name"] = "Test"
    wk.rosters[1] = {"players": ["o"]}
    wk.legs[1] = {"points": 10}
    me = week.build_team(wk, 9)
    opp = Team(["SF", "UTIL"], ["o", "0"], {"o": ["SF"]}, pending={0: ("o", 25., "g1")})
    monkeypatch.setattr(api, "rest", lambda *a, **kw: [])
    return ctx, wk, me, opp


@pytest.mark.parametrize("state", ["pending", "locked", "playing", "open", "just_locked", "none"])
@pytest.mark.parametrize("fails", [False, True])
def test_crowd_snapshot_only_our_pending_once_and_fail_soft(home, monkeypatch, state, fails):
    ctx, wk, me, opp = crowd_fixture(home, monkeypatch)
    done = {}
    if state in ("locked", "playing", "open", "none"):
        me.pending.pop(0)
    if state == "locked": me.locked[0] = 45
    if state == "playing": me.inflight[0] = "a"
    if state == "just_locked": done["a"] = "g1"
    if state == "none": me.pending.clear()
    dec = policy.decide(me, opp, len(wk.days), risk=policy.RISK)
    before = (dec.locks[:], dec.assign_today, dec.p_win)
    calls = []
    signal = {"n": 8, "lock": 6, "pass": 2, "lock_share": .75, "news": ["Crowd snippet"], "latest_ms": 123}

    def signals(players, pids, since_ms, **kwargs):
        calls.append((pids, since_ms))
        assert players is wk.players
        if fails: raise ConnectionError("offline")
        return {p: signal for p in ["a", "b", "o"]}

    monkeypatch.setattr(crowd, "signals", signals)
    for _ in range(2):
        snap = cycle.snapshot(ctx, wk, me, opp, dec, me.starters, done)
        expected_pids = ["a", "b"] if state == "pending" else [] if state == "none" else ["b"]
        got = [r["player"]["id"] for r in snap["me"]["slots"] if "crowd" in r]
        assert got == ([] if fails else expected_pids)
        assert all("crowd" not in r for r in snap["opp"]["slots"])
        for row in snap["me"]["slots"]:
            if "crowd" in row: assert set(row["crowd"]) == {"n", "lock", "pass", "lock_share", "news"}
    assert calls == ([(expected_pids, wk.games[0].start_ms)] if expected_pids else [])
    after = policy.decide(me, opp, len(wk.days), risk=policy.RISK)
    assert (after.locks, after.assign_today, after.p_win) == before


@pytest.mark.parametrize("n,share,line", [(0, None, ""), (8, .75, "Sleeper chat: 75% lock (8 votes)"), (8, .125, "Sleeper chat: 13% lock (8 votes)"), (1, None, "Sleeper chat: no lock/pass votes")])
def test_crowd_brief_line_is_separate_from_manager(home, monkeypatch, n, share, line):
    ctx, wk, me, opp = crowd_fixture(home, monkeypatch)
    calls = []
    def signals(players, pids, since_ms, **kwargs):
        calls.append(pids)
        return {p: {"n": n, "lock": round(n * share) if share else 0, "pass": n - round(n * share) if share else 0,
                    "lock_share": share, "news": []} for p in pids}
    monkeypatch.setattr(crowd, "signals", signals)
    monkeypatch.setattr(cycle, "load", lambda *a: (wk, me, opp))
    monkeypatch.setattr(cycle, "_ir", lambda ctx, wk, me, **kw: (wk, me))
    monkeypatch.setattr(cycle, "_moves", lambda ctx, wk, me, opp: (wk, me, opp))
    monkeypatch.setattr(cycle, "_claims_check", lambda *a: set())
    messages = []
    monkeypatch.setattr(notify, "send", lambda cfg, text, **kw: messages.append(text) or True)
    cycle.run_league(ctx, brief=True)
    assert len(calls) == 1
    if line:
        assert messages[0].splitlines().count(line) == 2
    else:
        assert "Sleeper chat:" not in messages[0]


@pytest.mark.parametrize("state,n", [("pending", 8), ("pending", 0), ("locked", 8)])
@pytest.mark.parametrize("news", ['<img src=x onerror="alert(1)">', "Fans say 'rest' & <minutes limit>"])
def test_crowd_web_line_and_news_are_escaped(state, n, news):
    import html
    source = (Path(web.__file__).with_suffix("") / "app.js").read_text()
    helpers = source[source.index("  function esc("):source.index("  /* ---------- data")]
    board = source[source.index("  function tag("):source.index("  function benchRow(")]
    signal = {"n": n, "lock": 6, "pass": 2, "lock_share": .75, "news": [news]}
    row = {"slot": "UTIL", "state": state, "player": {"id": "p", "name": "P"}, "score": 45, "open_value": 30, "decision": "lock", "crowd": signal}
    rendered = _node("var S = {open: {}}, HOUR = 3600000, DAY = 86400000;\n" + helpers + board +
                     "console.log(JSON.stringify(slotRow(" + json.dumps(row) + ", 0, 60, {paused: false, pwin: .5})));" )
    assert ("Sleeper chat: 75% lock (8 votes)" in rendered) is (state == "pending" and n > 0)
    assert news not in rendered
    if state == "pending":
        assert "Sleeper chat (crowd)" in rendered
        assert html.escape(news).replace("&#x27;", "&#39;") in rendered
        assert rendered.index("class=\"reason") < rendered.index("Sleeper chat")


@pytest.mark.parametrize("map_team,dict_team", [(None, None), (None, "AAA"), ("AAA", None)])
def test_teamless_player_never_matches_an_empty_transition(tmp_path, monkeypatch, map_team, dict_team):
    # Found on a real league: a rostered player with no NBA team made {}.get("team") == None match.
    from test_week import FakeModel
    now = 1793289600000
    players = {"p": {"team": dict_team, "fantasy_positions": ["SF"]}}
    league = {"season": "2026", "roster_positions": ["UTIL"], "scoring_settings": {"pts": 1.}}
    legs = [{"roster_id": 1, "matchup_id": 1, "starters": ["p"], "player_map": {"p": {"team": map_team, "injury_status": None}}},
            {"roster_id": 2, "matchup_id": 1, "starters": []}]
    monkeypatch.setattr(api, "rest", lambda path, **kw: {"leg": 2} if path == "/v1/state/nba" else [] if path.startswith("/stats") else league)
    monkeypatch.setattr(api, "scores", lambda *a: [])
    monkeypatch.setattr(api, "league_rosters", lambda *a: [{"roster_id": 1, "players": ["p"]}, {"roster_id": 2, "players": []}])
    monkeypatch.setattr(api, "legs_with_players", lambda *a: copy.deepcopy(legs))
    monkeypatch.setattr(week.time, "time", lambda: now / 1000)
    store = SimpleNamespace(players=lambda: players, injuries=lambda: {})
    wk = week.load(store, lambda wk: FakeModel(), "77", 1, now_ms=now, state_dir=str(tmp_path))
    assert wk.players["p"]["team"] == map_team
