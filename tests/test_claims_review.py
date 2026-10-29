"""Review regressions for local ownership, web-shaped writes and bounded churn."""
import copy
import json
from types import SimpleNamespace

import pytest

from sleeper_gm import api, auth, claims, cli, config, cycle, moves, notify
from test_claims import context, fixture, ms, row, NOW
from test_public import home  # noqa: F401
from test_safety import _snapshot_for_cycle


@pytest.mark.parametrize("error_type", [None, api.SleeperError, KeyError, TypeError])
@pytest.mark.parametrize("phase", ["pending", "observe", "reconcile"])
def test_claim_refresh_failure_keeps_cycle_running(home, monkeypatch, error_type, phase):
    wk, me, opp, _r = fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch, [row(drop="cut")])
    ctx.cfg["max_transactions_per_day"] = 0
    intents = {"local": {"add": "add", "drop": "core", "leg": 2, "pending_before": ["owner"]}}
    claims.save_state(ctx.dir, {"intents": intents})
    monkeypatch.setattr(auth, "_cached", lambda: "test-private-sentinel")
    monkeypatch.setattr(api, "league_transactions", lambda *a, **kw: [])
    monkeypatch.setattr(cycle, "load", lambda ctx: (wk, me, opp))
    monkeypatch.setattr(cycle, "snapshot", _snapshot_for_cycle)
    monkeypatch.setattr(notify, "send", lambda *a, **kw: True)
    monkeypatch.setattr(claims, "reconcile", lambda *a: None)
    if error_type:
        def fail(*a):
            raise error_type("refresh failed: test-private-sentinel")
        monkeypatch.setattr(claims, phase, fail)
    protected = []
    def locks(ctx, wk, me, opp, pending_drops):
        protected.append(set(pending_drops))
        return None, me.starters, {}
    monkeypatch.setattr(cycle, "_locks", locks)
    cycle._run(ctx, brief=False)
    assert protected == [{"cut", "core"}]
    assert claims.read_state(ctx.dir)["intents"] == intents
    skipped = [a for a in ctx.acted if a.startswith("Claims skipped this run:")]
    assert len(skipped) == int(error_type is not None)
    assert "test-private-sentinel" not in json.dumps(ctx.acted)
    if error_type:
        assert "<token>" in skipped[0]


@pytest.mark.parametrize("response_id", [False, True])
def test_claim_readback_failure_protects_new_intent_drop(home, monkeypatch, response_id):
    wk, me, opp, _r = fixture(monkeypatch)
    ctx, rows = context(home, monkeypatch)
    ctx.cfg["max_transactions_per_day"] = 0
    monkeypatch.setattr(api, "league_transactions", lambda *a, **kw: [])
    monkeypatch.setattr(cycle, "load", lambda ctx: (wk, me, opp))
    monkeypatch.setattr(cycle, "snapshot", _snapshot_for_cycle)
    monkeypatch.setattr(notify, "send", lambda *a, **kw: True)
    original = ctx.writer.submit_claim
    def submit(*a, **kw):
        response = original(*a, **kw)
        return response if response_id else None
    ctx.writer.submit_claim = submit
    def pending(ctx):
        if rows:
            raise api.SleeperError("pending read failed")
        return []
    monkeypatch.setattr(claims, "pending", pending)
    protected = []
    def locks(ctx, wk, me, opp, pending_drops):
        protected.append(set(pending_drops))
        return None, me.starters, {}
    monkeypatch.setattr(cycle, "_locks", locks)
    cycle._run(ctx, brief=False)
    assert protected == [{"cut"}]
    assert ctx.writer.calls == [("submit", "add")]
    assert next(iter(claims.read_state(ctx.dir)["intents"].values()))["drop"] == "cut"
    assert ctx.acted.count("Claims skipped this run: pending read failed") == 1


@pytest.mark.parametrize("phase", ["initial", "readback"])
@pytest.mark.parametrize("priority", [None, []])
def test_malformed_claim_refresh_keeps_last_usable_pending(home, monkeypatch, phase, priority):
    wk, me, opp, _r = fixture(monkeypatch)
    # Use the real pending reader, including its authenticated API boundary.
    pending_reader = claims.pending
    ctx, rows = context(home, monkeypatch, [row(drop="old-drop")])
    monkeypatch.setattr(claims, "pending", pending_reader)
    ctx.cfg["max_transactions_per_day"] = 2 if phase == "initial" else 0
    # No immediate candidates, but still exercise reservation checks before reconciliation.
    monkeypatch.setattr(moves, "free_agents", lambda wk, limit=60: [] if phase == "initial" else ["add", "alt"])
    last_known = copy.deepcopy(ctx.pending_claims)
    def transactions(*a, **kw):
        if kw.get("status") != "pending":
            return []
        refreshed = copy.deepcopy(rows)
        if phase == "initial" or len(rows) > 1:
            refreshed[-1]["settings"]["priority"] = priority
        return refreshed
    monkeypatch.setattr(api, "league_transactions", transactions)
    monkeypatch.setattr(cycle, "load", lambda ctx: (wk, me, opp))
    views = []
    def snapshot(*args):
        views.append(claims.view(ctx, wk))
        return _snapshot_for_cycle(*args)
    monkeypatch.setattr(cycle, "snapshot", snapshot)
    monkeypatch.setattr(notify, "send", lambda *a, **kw: True)
    protected = []
    def locks(ctx, wk, me, opp, pending_drops):
        protected.append(set(pending_drops))
        return None, me.starters, {}
    monkeypatch.setattr(cycle, "_locks", locks)
    cycle._run(ctx, brief=False)
    assert ctx.pending_claims == last_known
    assert protected == [{"old-drop", "cut"} if phase == "readback" else {"old-drop"}]
    assert views[0]["available"] is False
    assert sum(a.startswith("Claims skipped this run:") for a in ctx.acted) == 1
    assert bool(claims.read_state(ctx.dir).get("intents")) is (phase == "readback")


@pytest.mark.parametrize("pending,bot,spaces,kind", [
    ([], {}, 1, "add"),
    ([row()], {}, 1, "swap"),
    ([row()], {"owner": {"leg": 2}}, 1, "add"),
    ([row(drop="cut")], {}, 1, "add"),
    ([row(add="core")], {}, 1, "add"),
    ([row(add="taken")], {}, 1, "add"),
    ([row(adds={"owner_add": 2})], {}, 1, "add"),
    ([row(), row("duplicate")], {}, 2, "add"),
    ([row(), row("second", add="alt")], {}, 2, "swap"),
    ([row()], {}, 2, "add"),
])
def test_immediate_add_reserves_viable_owner_claim_vacancies(home, monkeypatch, pending, bot, spaces, kind):
    wk, me, opp, _r = fixture(monkeypatch, values={"core": 40, "cut": 12, "add": 22,
                                                "alt": 21, "owner_add": 24, "taken": 10})
    wk.league["roster_positions"].extend(["BN"] * spaces)
    wk.rosters[2]["players"] = ["taken"]
    ctx, _ = context(home, monkeypatch, pending)
    ctx.live = False
    claims.save_state(ctx.dir, {"bot": bot})
    monkeypatch.setattr(api, "league_transactions", lambda *a, **kw: [])
    monkeypatch.setattr(moves, "decide", lambda *a, **kw: SimpleNamespace(p_win=.5))
    cycle._moves(ctx, wk, me, opp)
    assert ctx.acted[0].startswith("Would " + kind + ":")
    assert ctx.writer.calls == []


@pytest.mark.parametrize("processing,tick", [("03:08", "02:40"), ("03:20", "02:40"),
                                           ("03:30", "02:40"), ("03:35", "03:10")])
@pytest.mark.parametrize("phase,expected", [("previous", False), ("due", True), ("already_ran", False)])
def test_final_claim_tick_leaves_freeze_and_planning_time(home, monkeypatch, processing, tick, phase, expected):
    cfg = {**config.load(), "brief_at_et": "23:59"}
    roster = {"roster_id": 1, "players": ["core"], "starters": ["core"], "reserve": []}
    tick_ms = ms("2026-10-29T" + tick)
    now = tick_ms - 30 * 60_000 if phase == "previous" else tick_ms
    last = tick_ms if phase == "already_ran" else tick_ms - 60 * 60_000
    saved = {"leagues": ["fake"], "brief": {"fake": cli._today()},
             "rosters": {"fake": {"rid": 1, "hash": cli.roster_hash(roster), "full_ms": last}}}
    config.STATE_DIR.mkdir(exist_ok=True)
    (config.STATE_DIR / "run.json").write_text(json.dumps(saved))
    hour, minute = map(int, processing.split(":"))
    claims.save_state(config.league_state("fake"), {"process_second": hour * 3600 + minute * 60})
    monkeypatch.setattr(api, "rest", lambda path, **kw: [roster] if path.endswith("/rosters")
                        else {"season": "2026", "leg": 2})
    monkeypatch.setattr(api, "scores", lambda *a: [])
    assert cli.due(cfg, now) == (expected, False)


@pytest.mark.parametrize("bid", [None, 0])
def test_submit_wire_has_no_fingerprint_or_assigned_order(bid):
    logs = []
    api.Writer(False, logs.append).submit_claim("fictional", 1, "add", "cut", bid=bid)
    query, variables = logs[0]["query"], logs[0]["variables"]
    assert "k_metadata" not in query and "v_metadata" not in query
    assert "seq" not in json.dumps(variables) and "priority" not in json.dumps(variables)
    if bid is None:
        assert "k_settings" not in query and "v_settings" not in query
        assert "ks" not in variables and "vs" not in variables
    else:
        assert variables["ks"] == ["waiver_bid"] and variables["vs"] == [0]


@pytest.mark.parametrize("settings", [{"seq": 12}, {"seq": 12, "waiver_bid": 0, "existing": 5}])
def test_reorder_preserves_writable_settings_without_sending_seq(settings):
    logs = []
    original = copy.deepcopy(settings)
    api.Writer(False, logs.append).update_claim("fictional", 2, "bot", 7, settings=settings)
    variables = logs[0]["variables"]
    sent = dict(zip(variables["ks"], variables["vs"]))
    assert sent == ({"priority": 7} if len(settings) == 1 else {"priority": 7, "waiver_bid": 0, "existing": 5})
    assert settings == original


@pytest.mark.parametrize("new_ids,drop,expected", [([], "cut", set()), (["new"], "cut", {"new"}),
                                                     (["new"], "other", set()), (["new", "ambiguous"], "cut", set())])
def test_local_intent_matches_only_one_new_pending_identity(home, monkeypatch, new_ids, drop, expected):
    wk, _me, _opp, _r = fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch)
    claims.save_state(ctx.dir, {"intents": {"local": {"add": "add", "drop": "cut", "leg": 2,
                                                       "pending_before": ["owner"]}}})
    ctx.pending_claims = [row("owner", "add", "cut")] + [row(tid, "add", drop) for tid in new_ids]
    monkeypatch.setattr(api, "league_transactions", lambda *a, **kw: [])
    claims.observe(ctx, wk)
    state = claims.read_state(ctx.dir)
    assert set(state["bot"]) == expected
    assert bool(state["intents"]) is (not expected)


@pytest.mark.parametrize("priority", [None, 0, 8])
def test_existing_bot_never_reordered_for_owner_priority(home, monkeypatch, priority):
    wk, me, opp, _r = fixture(monkeypatch)
    owner = row("owner", drop="missing", seq=40)
    if priority is not None:
        owner["settings"]["priority"] = priority
    ctx, rows = context(home, monkeypatch, [owner, row("bot", "add", "cut", seq=50)])
    original = copy.deepcopy(rows)
    claims.save_state(ctx.dir, {"bot": {"bot": {"leg": 2}}})
    claims.reconcile(ctx, wk, me, opp)
    assert ctx.writer.calls == []
    assert rows == original
    assert rows[1]["settings"]["seq"] == 50


@pytest.mark.parametrize("second_alt,replace", [(22.1, False), (24.49, False), (24.5, True), (25, True)])
def test_two_run_hysteresis_avoids_near_tie_cancel_resubmit(home, monkeypatch, second_alt, replace):
    values = {"core": 40, "cut": 12, "add": 22, "alt": 21, "owner_add": 24}
    wk, me, opp, _r = fixture(monkeypatch, values=values)
    ctx, rows = context(home, monkeypatch)
    claims.reconcile(ctx, wk, me, opp)
    first_id = rows[0]["transaction_id"]
    values["alt"] = second_alt
    claims.reconcile(ctx, wk, me, opp)
    assert [r["adds"] for r in rows] == [{"alt" if replace else "add": 1}]
    assert ctx.writer.calls == ([("submit", "add"), ("cancel", first_id, 2), ("submit", "alt")]
                                if replace else [("submit", "add")])


@pytest.mark.parametrize("score_value,keep", [(19, False), (19.5, True), (20, True)])
def test_incumbent_rechecked_against_own_threshold(home, monkeypatch, score_value, keep):
    values = {"core": 40, "cut": 12, "add": score_value, "alt": 18, "owner_add": 24}
    wk, me, opp, _r = fixture(monkeypatch, values=values)
    ctx, rows = context(home, monkeypatch, [row("bot", "add", "cut", seq=1, settings={"seq": 1, "priority": 1})])
    # Deliberately stale persisted score must not decide whether to keep a claim.
    claims.save_state(ctx.dir, {"bot": {"bot": {"leg": 2, "score": .9}}})
    claims.reconcile(ctx, wk, me, opp)
    assert bool(rows) is keep
    assert ctx.writer.calls == ([] if keep else [("cancel", "bot", 2)])


@pytest.mark.parametrize("kind", ["submit_claim", "update_claim", "cancel_claim"])
@pytest.mark.parametrize("result", ["ok", "error: timeout"])
def test_budget_counts_every_claim_attempt_and_reports_once(home, monkeypatch, kind, result):
    wk, me, opp, _r = fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch)
    for _ in range(6):
        ctx.append("writes", {"t": NOW / 1000, "kind": kind, "live": True, "result": result})
    claims.reconcile(ctx, wk, me, opp)
    fresh, _ = context(home, monkeypatch)
    claims.reconcile(fresh, wk, me, opp)
    assert ctx.writer.calls == [] and fresh.writer.calls == []
    assert ctx.acted.count("claim write budget used") == 1
    assert "claim write budget used" not in fresh.acted


@pytest.mark.parametrize("kind,live", [("free_agent", True), ("set_leg", True), ("submit_claim", False)])
def test_budget_does_not_count_unrelated_or_dry_run_writes(home, monkeypatch, kind, live):
    wk, me, opp, _r = fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch)
    for _ in range(6):
        ctx.append("writes", {"t": NOW / 1000, "kind": kind, "live": live, "result": "dry-run" if not live else "ok"})
    claims.reconcile(ctx, wk, me, opp)
    assert ctx.writer.calls == [("submit", "add")]


@pytest.mark.parametrize("used,limit,expected", [(0, 0, []), (0, 1, ["cancel"]),
                                                 (0, 2, ["cancel", "submit"]), (1, 2, ["cancel"])])
def test_budget_stops_between_cancel_and_replacement(home, monkeypatch, used, limit, expected):
    wk, me, opp, _r = fixture(monkeypatch, values={"core": 40, "cut": 12, "add": 25, "alt": 21, "owner_add": 24})
    ctx, _ = context(home, monkeypatch, [row("old", "alt", "cut")])
    ctx.cfg["max_claim_writes_per_day"] = limit
    claims.save_state(ctx.dir, {"bot": {"old": {"leg": 2}}})
    for _ in range(used):
        ctx.append("writes", {"t": NOW / 1000, "kind": "submit_claim", "live": True, "result": "ok"})
    claims.reconcile(ctx, wk, me, opp)
    assert [c[0] for c in ctx.writer.calls] == expected
    assert ctx.claim_writes_today() <= limit
    assert ctx.acted.count("claim write budget used") == 1


@pytest.mark.parametrize("now_et,allowed", [("2026-10-28T23:59:59", False), ("2026-10-29T00:00:00", True)])
def test_daily_claim_budget_resets_at_et_midnight(home, monkeypatch, now_et, allowed):
    wk, me, opp, _r = fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch)
    ctx.cfg["max_claim_writes_per_day"] = 1
    ctx.append("writes", {"t": NOW / 1000, "kind": "update_claim", "live": True, "result": "error: timeout"})
    claims.reconcile(ctx, wk, me, opp)
    assert ctx.acted == ["claim write budget used"]
    monkeypatch.setattr(claims.time, "time", lambda: ms(now_et) / 1000)
    fresh, _ = context(home, monkeypatch)
    fresh.cfg["max_claim_writes_per_day"] = 1
    claims.reconcile(fresh, wk, me, opp)
    assert fresh.writer.calls == ([("submit", "add")] if allowed else [])
    assert fresh.acted.count("claim write budget used") == int(allowed)


@pytest.mark.parametrize("outcome", [None, "ok", "error: timeout"])
def test_attempt_and_outcome_log_rows_charge_one_request(home, monkeypatch, outcome):
    fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch)
    attempt = {"t": NOW / 1000, "write_id": "local-attempt", "kind": "submit_claim", "live": True, "result": "attempt"}
    ctx.append("writes", attempt)
    if outcome:
        ctx.append("writes", {**attempt, "result": outcome})
    assert ctx.claim_writes_today() == 1


def test_late_response_log_does_not_consume_new_day_budget(home, monkeypatch):
    fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch)
    monkeypatch.setattr(claims.time, "time", lambda: ms("2026-10-29T00:00:00") / 1000)
    ctx.append("writes", {"t": ms("2026-10-28T23:59:59") / 1000, "kind": "submit_claim", "live": True, "result": "ok"})
    assert ctx.claim_writes_today() == 0


@pytest.mark.parametrize("priority", [None, 0, 8])
def test_new_claim_uses_only_server_append_order(home, monkeypatch, priority):
    wk, me, opp, _r = fixture(monkeypatch)
    owner = row("owner", drop="missing", seq=17)
    if priority is not None:
        owner["settings"]["priority"] = priority
    ctx, rows = context(home, monkeypatch, [owner])
    claims.reconcile(ctx, wk, me, opp)
    assert ctx.writer.calls == [("submit", "add")]
    assert rows[1]["settings"] == {"seq": 18}
    assert rows[0] == owner


@pytest.mark.parametrize("changed_key", ["seq", "priority"])
def test_submit_readback_ignores_server_order(home, monkeypatch, changed_key):
    wk, me, opp, _r = fixture(monkeypatch)
    ctx, rows = context(home, monkeypatch, [row("owner", drop="missing", settings={"priority": 0})])
    original = ctx.writer.submit_claim
    def submit(*args, **kw):
        response = original(*args, **kw)
        rows[1]["settings"][changed_key] = 99
        return response
    ctx.writer.submit_claim = submit
    claims.reconcile(ctx, wk, me, opp)
    assert ctx.writer.calls == [("submit", "add")]
    assert rows[1]["settings"][changed_key] == 99
    assert any(a.startswith("Claim placed:") for a in ctx.acted)


def test_pending_identity_baseline_is_read_immediately_before_submit(home, monkeypatch):
    wk, me, opp, _r = fixture(monkeypatch)
    owner = row("owner", drop="missing")
    ctx, rows = context(home, monkeypatch, [owner], error=True)
    events = []
    def pending(ctx):
        events.append("read")
        return copy.deepcopy(rows)
    monkeypatch.setattr(claims, "pending", pending)
    original = ctx.writer.submit_claim
    def submit(*args, **kw):
        events.append("submit")
        record = next(iter(claims.read_state(ctx.dir)["intents"].values()))
        assert record["pending_before"] == ["owner"]
        assert events == ["read", "submit"]
        return original(*args, **kw)
    ctx.writer.submit_claim = submit
    claims.reconcile(ctx, wk, me, opp)
    assert events == ["read", "submit", "read"]
    assert set(claims.read_state(ctx.dir)["bot"]) == {"bot-new"}


@pytest.mark.parametrize("visible", [False, True])
def test_response_id_takes_precedence_over_other_new_identity(home, monkeypatch, visible):
    wk, _me, _opp, _r = fixture(monkeypatch)
    ctx, _ = context(home, monkeypatch)
    claims.save_state(ctx.dir, {"intents": {"local": {"transaction_id": "response-id", "add": "add", "drop": "cut",
                                                       "leg": 2, "pending_before": []}}})
    ctx.pending_claims = [row("another", "add", "cut")]
    if visible:
        ctx.pending_claims.append(row("response-id", "add", "cut"))
    monkeypatch.setattr(api, "league_transactions", lambda *a, **kw: [])
    claims.observe(ctx, wk)
    state = claims.read_state(ctx.dir)
    assert set(state["bot"]) == ({"response-id"} if visible else set())
    assert bool(state["intents"]) is (not visible)


def test_hysteresis_revalues_incumbent_outside_candidate_shortlist(home, monkeypatch):
    values = {"core": 40, "cut": 12, "add": 22, **{f"other{i}": 22.1 for i in range(60)}}
    wk, me, opp, _r = fixture(monkeypatch, values=values)
    candidates = [f"other{i}" for i in range(60)] + ["add"]
    monkeypatch.setattr(claims.moves, "free_agents", lambda wk, limit=60: candidates[:limit])
    ctx, rows = context(home, monkeypatch, [row("bot", "add", "cut", seq=1)])
    claims.save_state(ctx.dir, {"bot": {"bot": {"leg": 2}}})
    claims.reconcile(ctx, wk, me, opp)
    assert [r["transaction_id"] for r in rows] == ["bot"]
    assert ctx.writer.calls == []


@pytest.mark.parametrize("initial,latest", [(None, 0), (0, 5), (0, None)])
def test_owner_reorder_during_submit_does_not_reorder_bot(home, monkeypatch, initial, latest):
    wk, me, opp, _r = fixture(monkeypatch)
    owner = row("owner", drop="missing")
    if initial is not None:
        owner["settings"]["priority"] = initial
    ctx, rows = context(home, monkeypatch, [owner])
    original = ctx.writer.submit_claim
    def submit(*args, **kw):
        response = original(*args, **kw)
        if latest is None:
            rows[0]["settings"].pop("priority", None)
        else:
            rows[0]["settings"]["priority"] = latest
        return response
    ctx.writer.submit_claim = submit
    claims.reconcile(ctx, wk, me, opp)
    assert ctx.writer.calls == [("submit", "add")]
    assert "priority" not in rows[1]["settings"]
    assert rows[0]["settings"].get("priority") == latest
