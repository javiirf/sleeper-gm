"""Offline claim workflows. Writers below are fakes; no Sleeper mutations are sent."""
import copy
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sleeper_gm import api, claims, cli, config, cycle, moves, week
from sleeper_gm.policy import Draw, Team
from test_public import home, _node  # noqa: F401


def ms(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=week.ET).timestamp() * 1000)


NOW = ms("2026-10-28T18:00")
PROCESS = ms("2026-10-29T03:10")


def fixture(monkeypatch, full=True, rank=4, values=None):
    values = values or {"core": 40, "cut": 12, "add": 22, "alt": 21, "owner_add": 24}
    players = ["core", "cut"] if full else ["core"]
    roster = {"roster_id": 1, "players": players, "starters": ["core"], "reserve": [], "settings": {"waiver_position": rank}}
    games = [week.Game("before", "2026-10-28", NOW + 3600_000, "pre_game", "AAA", "BBB"),
             week.Game("at", "2026-10-29", PROCESS, "pre_game", "AAA", "BBB"),
             week.Game("after", "2026-10-30", PROCESS + 40 * 3600_000, "pre_game", "AAA", "BBB")]
    model = SimpleNamespace(ros_value=lambda p, t: values[p], availability_weeks=lambda *a, **k: 1,
                            dist=lambda p, g: SimpleNamespace(p_play=1., samples=np.full(20, values[p])))
    wk = SimpleNamespace(now_ms=NOW, my_rid=1, week=2, days=["2026-10-28", "2026-10-29", "2026-10-30"],
                         league={"roster_positions": ["UTIL", "BN"], "settings": {"waiver_type": 0}},
                         rosters={1: roster, 2: {}, 3: {}, 4: {}}, players={p: {"fantasy_positions": ["SF"]} for p in values},
                         model=model, name=lambda p: p, team_of=lambda p: "AAA", player_games=lambda p: games,
                         protected={"core"}, games=games)
    team = Team(["UTIL"], ["core"], {p: ["SF"] for p in players},
                games={p: {i: Draw(1., np.full(20, values[p])) for i in range(3)} for p in players})
    team.lock_games = {}
    monkeypatch.setattr(moves, "free_agents", lambda wk, limit=60: ["add", "alt"])
    # Equal weekly probability isolates the ROS delta and the priority opportunity cost.
    monkeypatch.setattr(claims, "decide", lambda *a, **kw: SimpleNamespace(p_win=.5))
    monkeypatch.setattr(claims.time, "time", lambda: NOW / 1000)
    return wk, team, Team(["UTIL"], ["opp"], {}), roster


def row(tid="owner", add="owner_add", drop=None, seq=4, leg=2, **kw):
    return {"transaction_id": tid, "type": "waiver", "status": "pending", "leg": leg,
            "adds": {add: 1}, "drops": {drop: 1} if drop else {}, "settings": {"seq": seq}, **kw}


@pytest.mark.parametrize("rank,placed", [(1, False), (2, False), (4, True)])
def test_marginal_claim_cost_depends_on_priority(monkeypatch, rank, placed):
    wk, me, opp, r = fixture(monkeypatch, rank=rank)
    planned = claims.plan(wk, me, opp, r, [], PROCESS, 3)
    assert bool(planned) is placed
    if placed:
        assert planned[0]["score"] == pytest.approx(.04)


@pytest.mark.parametrize("full,drop", [(False, None), (True, "cut")])
def test_claim_has_drop_only_when_roster_full(monkeypatch, full, drop):
    wk, me, opp, r = fixture(monkeypatch, full=full)
    plan = claims.plan(wk, me, opp, r, [], PROCESS, 3)
    assert plan and plan[0]["drop"] == drop
    assert len(plan) <= 3
    if full:
        assert [p["drop"] for p in plan] == ["cut", "cut"]


@pytest.mark.parametrize("owner_present", [False, True])
def test_owner_claim_consumes_spot_and_bot_follows_sequence(monkeypatch, owner_present):
    wk, me, opp, r = fixture(monkeypatch)
    wk.league["roster_positions"].append("BN")  # one open spot until owner's add processes
    owner = [row(seq=7)] if owner_present else []
    original = copy.deepcopy(owner)
    plan = claims.plan(wk, me, opp, r, owner, PROCESS, 3)
    assert plan[0]["drop"] == ("cut" if owner_present else None)
    assert owner == original


def test_owner_drop_cannot_be_reused_even_when_protected_roster_is_full(monkeypatch):
    wk, me, opp, r = fixture(monkeypatch)
    assert claims.plan(wk, me, opp, r, [row(drop="cut")], PROCESS, 3) == []


@pytest.mark.parametrize("after,add_days", [(None, {0, 1, 2}), (NOW + 3600_000, {1, 2}),
                                           (PROCESS, {2}), (PROCESS + 40 * 3600_000, set())])
def test_claim_removes_drop_and_limits_add_games_after_processing(monkeypatch, after, add_days):
    wk, me, _opp, _r = fixture(monkeypatch)
    t = moves.with_player(wk, me, "add", "cut", after_ms=after)
    assert set(t.games["add"]) == add_days
    assert "cut" not in t.games and "cut" not in t.positions
    assert set(me.games["cut"]) == {0, 1, 2}
    assert "cut" in me.positions


@pytest.mark.parametrize("samples,expected", [([], "03:10"), (["03:08", "03:09", "06:00"], "03:09")])
def test_processing_clock_is_median_in_et(monkeypatch, samples, expected):
    state = {}
    rows = [row(status="complete", status_updated=ms("2026-10-28T" + s)) for s in samples]
    rows.append(row(status="pending", status_updated=ms("2026-10-28T12:00")))
    claims.learn_time(state, rows)
    previous, following = claims.processing_bounds(NOW, state)
    assert previous == ms("2026-10-28T" + expected)
    assert following == ms("2026-10-29T" + expected)


@pytest.mark.parametrize("date,hours", [("2026-11-01T00:00", 25), ("2026-03-08T00:00", 23)])
def test_processing_clock_crosses_dst(date, hours):
    a, b = claims.processing_bounds(ms(date), {})
    assert b - a == hours * 3600_000


class FakeWriter:
    def __init__(self, pending, commit=True, error=False):
        self.pending, self.calls = pending, []
        self.commit, self.error = commit, error
        self.log = lambda kind: None
        self.next_seq = max([claims.seq(r) for r in pending] + [0]) + 1

    def submit_claim(self, league, rid, add, drop, **kw):
        assert set(kw) <= {"summary", "bid"}
        self.log("submit_claim")
        self.calls.append(("submit", add))
        r = row("bot-new", add, drop, self.next_seq)
        self.next_seq += 1
        if kw.get("bid") is not None:
            r["settings"]["waiver_bid"] = kw["bid"]
        if self.commit:
            self.pending.append(r)
        if self.error:
            raise api.SleeperError("timeout")
        return {"submit_waiver_claim": r}

    def cancel_claim(self, league, leg, tid, **kw):
        self.log("cancel_claim")
        self.calls.append(("cancel", tid, leg))
        if self.commit:
            self.pending[:] = [r for r in self.pending if r["transaction_id"] != tid]
        if self.error:
            raise api.SleeperError("timeout")

    def update_claim(self, league, leg, tid, priority, **kw):
        self.log("update_claim")
        self.calls.append(("update", tid, priority))
        if self.commit:
            next(r for r in self.pending if r["transaction_id"] == tid)["settings"]["priority"] = priority
        if self.error:
            raise api.SleeperError("timeout")


def context(home, monkeypatch, rows=None, commit=True, error=False):
    ctx = cycle.Ctx({**config.load(), "max_claims": 1}, "fictional-league", 1, False, None)
    ctx.live = True
    rows = rows if rows is not None else []
    ctx.pending_claims = copy.deepcopy(rows)
    ctx.claims_available = True
    ctx.writer = FakeWriter(rows, commit, error)
    ctx.writer.log = lambda kind: ctx.append("writes", {"kind": kind, "t": claims.time.time(), "live": True,
                                                       "result": "error: timeout" if error else "ok"})
    monkeypatch.setattr(claims, "pending", lambda ctx: copy.deepcopy(rows))
    return ctx, rows


@pytest.mark.parametrize("stale", [False, True])
def test_replan_only_bot_claims_owner_untouched(home, monkeypatch, stale):
    wk, me, opp, _ = fixture(monkeypatch, values={"core": 40, "cut": 12, "add": 25, "alt": 21, "owner_add": 24})
    rows = [row(), row("bot-old", "alt" if stale else "add", "cut", seq=5, leg=1,
                       settings={"seq": 5, "priority": 5})]
    owner = copy.deepcopy(rows[0])
    # Owner add is an alternative on another player's drop, leaving one droppable player.
    rows[0]["drops"] = {"other": 1}
    owner = copy.deepcopy(rows[0])
    ctx, rows = context(home, monkeypatch, rows)
    claims.save_state(ctx.dir, {"bot": {"bot-old": {"why": "old", "leg": 1}}})
    claims.reconcile(ctx, wk, me, opp)
    assert rows[0] == owner
    assert ctx.writer.calls == ([("cancel", "bot-old", 1), ("submit", "add")] if stale else [])
    assert claims.view(ctx, wk)["owner"] == [{"add": "owner_add", "drop": "other"}]
    assert claims.view(ctx, wk)["bot"][0]["seq"] == (6 if stale else 5)


@pytest.mark.parametrize("minutes,changes", [(16, True), (15, False), (5, False)])
def test_freeze_window_stops_all_claim_changes(home, monkeypatch, minutes, changes):
    wk, me, opp, _ = fixture(monkeypatch, values={"core": 40, "cut": 12, "add": 25, "alt": 21, "owner_add": 24})
    ctx, rows = context(home, monkeypatch, [row("bot-old", "alt", "cut")])
    claims.save_state(ctx.dir, {"bot": {"bot-old": {"leg": 2}}})
    monkeypatch.setattr(claims.time, "time", lambda: (PROCESS - minutes * 60_000) / 1000)
    claims.reconcile(ctx, wk, me, opp)
    assert bool(ctx.writer.calls) is changes


def test_freeze_is_rechecked_after_slow_planning(home, monkeypatch):
    wk, me, opp, _ = fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch)
    real = claims.plan
    def slow(*args):
        result = real(*args)
        monkeypatch.setattr(claims.time, "time", lambda: (PROCESS - 60_000) / 1000)
        return result
    monkeypatch.setattr(claims, "plan", slow)
    claims.reconcile(ctx, wk, me, opp)
    assert ctx.writer.calls == []


@pytest.mark.parametrize("committed,error", [(True, False), (True, True), (False, False), (False, True)])
def test_submission_requires_pending_readback_and_never_retries(home, monkeypatch, committed, error):
    wk, me, opp, _ = fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch, commit=committed, error=error)
    claims.reconcile(ctx, wk, me, opp)
    claims.reconcile(ctx, wk, me, opp)
    assert ctx.writer.calls == [("submit", "add")]
    assert any(a.startswith("Claim placed:") for a in ctx.acted) is committed
    assert bool(claims.read_state(ctx.dir).get("intents")) is (not committed)


@pytest.mark.parametrize("committed", [False, True])
def test_cancellation_requires_readback_before_replacement(home, monkeypatch, committed):
    wk, me, opp, _ = fixture(monkeypatch, values={"core": 40, "cut": 12, "add": 25, "alt": 21, "owner_add": 24})
    ctx, _ = context(home, monkeypatch, [row("old", "alt", "cut")], committed, True)
    claims.save_state(ctx.dir, {"bot": {"old": {"leg": 2}}})
    claims.reconcile(ctx, wk, me, opp)
    assert [c[0] for c in ctx.writer.calls] == (["cancel", "submit"] if committed else ["cancel"])


@pytest.mark.parametrize("waiver_type,bid_min,submitted", [(0, 0, True), (2, 0, True), (2, 1, False)])
def test_faab_only_uses_zero_bid(home, monkeypatch, waiver_type, bid_min, submitted):
    wk, me, opp, _ = fixture(monkeypatch)
    wk.league["settings"].update(waiver_type=waiver_type, waiver_bid_min=bid_min)
    ctx, rows = context(home, monkeypatch)
    claims.reconcile(ctx, wk, me, opp)
    assert bool(rows) is submitted
    if submitted:
        assert rows[0]["settings"].get("waiver_bid") == (0 if waiver_type == 2 else None)


@pytest.mark.parametrize("status,notes,expected", [
    ("complete", "success", "Got add (dropped cut)"),
    ("failed", "This player was claimed by another owner.", "Lost add to another team"),
    ("failed", "Unfortunately, your roster will have too many players after this transaction.", "Failed: roster full (add)"),
    ("cancelled", "", None),
])
def test_processing_results_reported_once_across_contexts(home, monkeypatch, status, notes, expected):
    wk, _me, _opp, _ = fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch)
    claims.save_state(ctx.dir, {"bot": {"done": {"leg": 1}}})
    processed = row("done", "add", "cut", status=status, metadata={"notes": notes}, status_updated=PROCESS)
    monkeypatch.setattr(api, "league_transactions", lambda *a, **kw: [processed])
    claims.observe(ctx, wk)
    assert ctx.acted == ([expected] if expected else [])
    fresh, _ = context(home, monkeypatch)
    claims.observe(fresh, wk)
    assert fresh.acted == []
    assert claims.view(fresh, wk)["results"] == claims.view(ctx, wk)["results"]


def test_timeout_intent_recovers_ownership_without_adopting_owner_claim(home, monkeypatch):
    wk, _me, _opp, _ = fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch)
    claims.save_state(ctx.dir, {"intents": {"local": {"add": "owner_add", "drop": None, "why": "our request",
                                                      "leg": 2, "pending_before": ["owner"]}}})
    ctx.pending_claims = [row("ours"), row("owner")]
    monkeypatch.setattr(api, "league_transactions", lambda *a, **kw: [])
    claims.observe(ctx, wk)
    state = claims.read_state(ctx.dir)
    assert set(state["bot"]) == {"ours"} and state["intents"] == {}


@pytest.mark.parametrize("failures,success,disabled", [(2, False, False), (3, False, True), (4, False, True), (3, True, False)])
def test_repeated_waiver_errors_persist_until_success(home, monkeypatch, failures, success, disabled):
    fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch)
    for i in range(failures):
        ctx.mark_on_waivers(str(i))
    if success:
        claims.fa_result(ctx.dir, success=True)
    new, _ = context(home, monkeypatch)
    assert claims.fa_disabled(new.dir) is disabled


@pytest.mark.parametrize("fails,attempts", [(0, 3), (2, 1), (3, 0)])
def test_cycle_stops_futile_free_agent_attempts_and_routes_to_claims(home, monkeypatch, fails, attempts):
    wk, me, opp, _ = fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch)
    for _ in range(fails):
        claims.fa_result(ctx.dir, waiver_failure=True)
    monkeypatch.setattr(claims, "observe", lambda *a: None)
    monkeypatch.setattr(cycle, "load", lambda ctx: (wk, me, opp))
    calls, excluded, routed = [], [], []
    def free(*args, **kw):
        calls.append(kw["add"])
        raise api.SleeperError("At least one of the players being added in is on waivers.")
    def best(*args, exclude=(), reserved=0):
        excluded.append(set(exclude))
        return moves.Move("add", add="candidate" + str(len(calls)))
    ctx.writer.free_agent = free
    monkeypatch.setattr(moves, "best_move", best)
    monkeypatch.setattr(claims, "reconcile", lambda *a: routed.append(True))
    cycle._moves(ctx, wk, me, opp)
    assert len(calls) == attempts and routed == [True]
    if len(calls) > 1:
        assert "candidate0" in excluded[1]


@pytest.mark.parametrize("success", [True, False])
def test_external_success_reenables_free_agents(home, monkeypatch, success):
    wk, _me, _opp, _ = fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch)
    claims.save_state(ctx.dir, {"fa_waiver_failures": 3})
    rows = [row(type="free_agent", status="complete" if success else "failed", status_updated=NOW)]
    monkeypatch.setattr(api, "league_transactions", lambda *a, **kw: rows)
    claims.observe(ctx, wk)
    assert claims.fa_disabled(ctx.dir) is (not success)


@pytest.mark.parametrize("now,last,changed,expected", [
    ("02:10", "01:40", None, False), ("02:40", "02:10", None, True),
    ("02:40", "02:40", None, False), ("03:10", "02:40", None, True),
    ("03:10", "03:09", None, False), ("05:40", "05:10", "players", True),
    ("05:40", "05:10", "starters", True), ("05:40", "05:10", "reserve", True),
    ("05:40", "05:10", None, False),
])
def test_gate_due_at_processing_final_tick_or_roster_change(home, monkeypatch, now, last, changed, expected):
    cfg = {**config.load(), "brief_at_et": "23:59"}
    original = {"roster_id": 1, "players": ["a", "b"], "starters": ["a"], "reserve": []}
    roster = copy.deepcopy(original)
    if changed:
        roster[changed] = ["b"]
    saved = {"leagues": ["fake"], "brief": {"fake": cli._today()},
             "rosters": {"fake": {"rid": 1, "hash": cli.roster_hash(original), "full_ms": ms("2026-10-29T" + last)}}}
    config.STATE_DIR.mkdir(exist_ok=True)
    (config.STATE_DIR / "run.json").write_text(json.dumps(saved))
    claims.save_state(config.league_state("fake"), {"process_second": 3 * 3600 + 8 * 60})
    reads = []
    def rest(path, **kw):
        reads.append(path)
        return [roster] if path.endswith("/rosters") else {"season": "2026", "leg": 2}
    monkeypatch.setattr(api, "rest", rest)
    monkeypatch.setattr(api, "scores", lambda *a: [])
    assert cli.due(cfg, ms("2026-10-29T" + now)) == (expected, False)
    assert reads.count("/v1/league/fake/rosters") <= 1


def test_roster_hash_ignores_player_list_order_but_not_slot_order():
    a = {"players": ["a", "b"], "starters": ["a", "b"], "reserve": ["x", "y"]}
    b = {**a, "players": ["b", "a"], "reserve": ["y", "x"]}
    assert cli.roster_hash(a) == cli.roster_hash(b)
    b["starters"] = ["b", "a"]
    assert cli.roster_hash(a) != cli.roster_hash(b)


def test_writer_claim_shapes_are_dry_run_only(monkeypatch):
    logs = []
    writer = api.Writer(False, logs.append)
    monkeypatch.setattr(api, "gql", lambda *a, **kw: pytest.fail("dry run must never call GraphQL"))
    writer.submit_claim("fake", 1, "add", "cut", bid=0)
    writer.update_claim("fake", 2, "fake-tx", 7, settings={"existing": 5, "waiver_bid": 0, "seq": 12})
    writer.cancel_claim("fake", 2, "fake-tx")
    assert [r["kind"] for r in logs] == ["submit_claim", "update_claim", "cancel_claim"]
    assert all(r["result"] == "dry-run" for r in logs)
    assert logs[0]["variables"]["ks"] == ["waiver_bid"]
    assert logs[0]["variables"]["vs"] == [0]
    assert logs[0]["variables"]["ka"] == ["add"] and logs[0]["variables"]["va"] == [1]
    assert "$ks: [String]" in logs[0]["query"] and "$vs: [Int]" in logs[0]["query"]
    assert logs[1]["variables"]["ks"] == ["existing", "waiver_bid", "priority"]
    assert logs[2]["variables"]["leg"] == 2


def test_claim_graphql_write_is_never_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "token", lambda: "fake-token")
    def http(*args, **kw):
        calls.append(kw["tries"])
        raise api.SleeperError("timeout")
    monkeypatch.setattr(api, "_http", http)
    with pytest.raises(api.SleeperError):
        api.gql("offline-placeholder", auth=True, write=True)
    assert calls == [1]


def test_ir_unfreeze_starter_swap_never_benches_while_frozen(home, monkeypatch):
    from test_moves import WK, team
    wk = WK({"star": "Out", "cut": None, "core": None, "back": None},
            {"star": 40, "cut": 5, "core": 30, "back": 25}, protected={"star", "core"})
    wk.rosters = {1: {"players": ["star", "cut", "core", "back"], "reserve": ["back"]}}
    me = team(["star", "core"])
    wk.player_games = lambda p: []
    ctx, _ = context(home, monkeypatch)
    calls = []
    def reserve(lid, rid, new, **kw):
        calls.append(new)
        wk.rosters[1]["reserve"] = new
    ctx.writer.set_reserve = reserve
    ctx.writer.set_leg = lambda *a, **kw: pytest.fail("lineup is frozen before reserve swap")
    ctx.writer.free_agent = lambda *a, **kw: pytest.fail("swap must not drop anyone")
    monkeypatch.setattr(cycle, "load", lambda ctx: (wk, me, None))
    monkeypatch.setattr(cycle, "_refresh_write_guards", lambda *a: True)
    cycle._ir(ctx, wk, me, unfreeze_only=True)
    assert calls == [["star"]]


def test_claims_moves_tab_renders_demo_and_escapes_text():
    source = Path("sleeper_gm/web/app.js").read_text()
    render = source[source.index("  function renderMoves()"):source.index("  /* ---------- season")]
    demo = json.loads(Path("sleeper_gm/web/demo/now.json").read_text())
    demo["claims"]["bot"][0]["why"] = '<script>bad()</script>'
    helpers = source[source.index("  function esc("):source.index("  var fTime")]
    script = 'var S = {now: ' + json.dumps(demo) + ', runs: []};\n' + helpers + '''
const el = {}, document = {querySelector: () => null};
const $ = () => el, buildFeed = () => [], tidy = () => ({kind: 'note'}), glyph = () => '';
const rowWrap = (key, cls, inner, why) => inner + why;
''' + render + '\nrenderMoves(); console.log(JSON.stringify(el.innerHTML));'
    html = _node(script)
    assert '<h2>Claims</h2>' in html and 'Taylor Example' in html and '#2' in html
    assert '&lt;script&gt;' in html and '<script>' not in html
    assert 'Got Morgan Sample' in html


@pytest.mark.parametrize("commit", [True, False])
def test_owner_reorder_never_triggers_bot_update(home, monkeypatch, commit):
    wk, me, opp, _ = fixture(monkeypatch)
    ctx, rows = context(home, monkeypatch, [row("owner", drop="missing", seq=8, settings={"seq": 8, "priority": 8}), row("bot", "add", "cut", seq=1)], commit)
    original = copy.deepcopy(rows[0])
    claims.save_state(ctx.dir, {"bot": {"bot": {"leg": 2}}})
    claims.reconcile(ctx, wk, me, opp)
    assert ctx.writer.calls == []
    assert rows[0] == original
    assert "priority" not in rows[1]["settings"]
    assert rows[1]["settings"]["seq"] == 1
    assert not any("update not confirmed" in a for a in ctx.acted)


@pytest.mark.parametrize("limit", [0, 1, 2, 3])
def test_pending_cap_and_same_drop_alternatives(home, monkeypatch, limit):
    wk, me, opp, _ = fixture(monkeypatch)
    ctx, rows = context(home, monkeypatch)
    ctx.cfg["max_claims"] = limit
    original = ctx.writer.submit_claim
    def unique(*args, **kw):
        response = original(*args, **kw)
        response["submit_waiver_claim"]["transaction_id"] = "bot-" + str(len(rows))
        return response
    ctx.writer.submit_claim = unique
    claims.reconcile(ctx, wk, me, opp)
    assert len(rows) == min(limit, 2)
    assert all(r["drops"] == {"cut": 1} for r in rows)
    assert [claims.seq(r) for r in rows] == list(range(1, len(rows) + 1))


def test_readback_network_failure_keeps_intent_and_recovers_by_returned_id(home, monkeypatch):
    wk, me, opp, _ = fixture(monkeypatch)
    ctx, rows = context(home, monkeypatch)
    def pending(ctx):
        if rows:
            raise api.SleeperError("read failed")
        return []
    monkeypatch.setattr(claims, "pending", pending)
    with pytest.raises(api.SleeperError, match="read failed"):
        claims.reconcile(ctx, wk, me, opp)
    assert claims.read_state(ctx.dir)["intents"]
    # The returned transaction id proves ownership independently of identity matching.
    ctx.pending_claims = copy.deepcopy(rows)
    monkeypatch.setattr(api, "league_transactions", lambda *a, **kw: [])
    claims.observe(ctx, wk)
    assert not claims.read_state(ctx.dir)["intents"]
    claims.reconcile(ctx, wk, me, opp)
    assert ctx.writer.calls == [("submit", "add")]


def test_owner_alternative_missing_drop_and_full_roster_are_not_double_counted(monkeypatch):
    wk, me, opp, r = fixture(monkeypatch)
    wk.league["roster_positions"].append("BN")
    owner = [row("first"), row("second", "alt", seq=5)]
    planned = claims.plan(wk, me, opp, r, owner, PROCESS, 3)
    assert planned[0]["drop"] == "cut"  # second no-drop claim cannot consume a nonexistent spot


def test_our_success_resets_failure_streak_in_cycle(home, monkeypatch):
    wk, me, opp, _ = fixture(monkeypatch, full=False)
    ctx, _ = context(home, monkeypatch)
    ctx.cfg["max_transactions_per_day"] = 1
    claims.save_state(ctx.dir, {"fa_waiver_failures": 2})
    monkeypatch.setattr(claims, "observe", lambda *a: None)
    monkeypatch.setattr(claims, "reconcile", lambda *a: None)
    monkeypatch.setattr(moves, "best_move", lambda *a, **kw: moves.Move("add", add="add"))
    monkeypatch.setattr(cycle, "load", lambda ctx: (wk, me, opp))
    def add(*args, **kw):
        wk.rosters[1]["players"].append(kw["add"])
    ctx.writer.free_agent = add
    cycle._moves(ctx, wk, me, opp)
    assert claims.read_state(ctx.dir)["fa_waiver_failures"] == 0
    assert any(a.startswith("Added") for a in ctx.acted)


def test_writer_redacts_token_in_claim_summary(monkeypatch):
    from sleeper_gm import auth
    logs = []
    monkeypatch.setattr(auth, "_cached", lambda: "private-sentinel")
    api.Writer(False, logs.append).submit_claim("fake", 1, "add", summary="private-sentinel")
    assert "private-sentinel" not in json.dumps(logs) and "<token>" in json.dumps(logs)


@pytest.mark.parametrize("add,week_gain,placed", [
    (13, .012, True),    # better this week and rest of season: .016 clears the 1.5% bar
    (11, .022, False),   # better this week, worse rest of season: .018 is under the 3% bar
    (11, .036, True),    # same trade-off with a big enough week gain
])
def test_claim_bar_is_lower_only_when_add_wins_both_ways(monkeypatch, add, week_gain, placed):
    wk, me, opp, r = fixture(monkeypatch, values={"core": 40, "cut": 12, "add": add, "alt": 5, "owner_add": 24})
    monkeypatch.setattr(claims, "decide", lambda t, *a, **kw: SimpleNamespace(p_win=.5 if t is me else .5 + week_gain))
    planned = claims.plan(wk, me, opp, r, [], PROCESS, 3)
    assert [p["add"] for p in planned] == (["add"] if placed else [])


@pytest.mark.parametrize("week_gain,ros_gain,edge", [(.01, 1, moves.DOMINANT_EDGE), (.01, -1, moves.SWAP_EDGE),
                                                     (-.01, 1, moves.SWAP_EDGE), (0, 1, moves.SWAP_EDGE)])
def test_swap_edge(week_gain, ros_gain, edge):
    assert moves.swap_edge(week_gain, ros_gain) == edge
