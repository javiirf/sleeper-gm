"""Owner instructions must reach real decisions, including alternate drop paths."""
import copy
import json
from types import SimpleNamespace

import numpy as np
import pytest

from sleeper_gm import api, claims, cli, config, cycle, moves, policy, week
from sleeper_gm.policy import Draw, Team
from test_claims import fixture, ms, row, NOW, PROCESS
from test_moves import WK, team
from test_public import home  # noqa: F401
from test_safety import FakeWk, one_lock_team
from test_sameday import live_week


def instruction(kind, player="cut", **extra):
    return {"id": "rule", "kind": kind, "player_id": player, "name": player,
            "label": f"{kind} {player}", **extra}


@pytest.mark.parametrize("kind,present,expected", [
    ("keep", False, "add"), ("keep", True, None),
    ("avoid", False, "add"), ("avoid", True, "alt"),
])
@pytest.mark.parametrize("planner", ["immediate", "claim", "incumbent"])
def test_keep_and_avoid_reach_each_roster_planner(monkeypatch, kind, present, expected, planner):
    wk, me, opp, roster = fixture(monkeypatch)
    wk.directives = [instruction(kind, "cut" if kind == "keep" else "add")] if present else []
    if planner == "immediate":
        result = moves.best_move(wk, me, opp, roster)
        actual = result.add if result else None
    else:
        existing = [row("bot", "add", "cut")] if planner == "incumbent" else []
        result = claims.plan(wk, me, opp, roster, [], PROCESS, 1, existing=existing)
        actual = result[0]["add"] if result else None
    assert actual == expected


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("avoided", [False, True])
def test_avoid_all_candidates_blocks_adds_and_swaps(monkeypatch, full, avoided):
    wk, me, opp, roster = fixture(monkeypatch, full=full)
    wk.directives = [instruction("avoid", p) for p in ("add", "alt")] if avoided else []
    assert bool(moves.best_move(wk, me, opp, roster)) is (not avoided)
    assert bool(claims.plan(wk, me, opp, roster, [], PROCESS, 1)) is (not avoided)


@pytest.mark.parametrize("protect,values,expected", [
    ([], {"b": 5, "back": 22}, "b"),
    (["b"], {"b": 5, "back": 22}, "back"),
    (["b", "back"], {"b": 5, "back": 22}, None),
    ([], {"b": 22, "back": 5}, "back"),
    (["back"], {"b": 22, "back": 5}, None),
])
def test_keep_protects_bench_and_healthy_ir_drop_branches(protect, values, expected):
    wk = WK({p: None for p in ("a", "b", "c", "back")}, {"a": 30, "c": 30, **values})
    wk.directives = [instruction("keep", p) for p in protect]
    result = moves.ir_fix(wk, team(["a", "c"]),
                          {"players": ["a", "b", "c", "back"], "reserve": ["back"]})
    assert result.drop == expected
    assert result.kind == ("needs_you" if expected is None else "drop_then_reserve")


@pytest.mark.parametrize("future", [20, 60])
@pytest.mark.parametrize("threshold,forbidden,expected", [(29, False, [0]), (30, False, [0]),
                                                        (31, False, []), (29, True, [])])
def test_lock_threshold_overrides_model_but_never_forbidden_lock(future, threshold, forbidden, expected):
    me = Team(["UTIL"], ["p"], {"p": ["SF"]}, pending={0: ("p", 30., "finished")},
              games={"p": {0: Draw(1., np.full(20, future))}})
    me.lock_at = {"p": threshold}
    opp = Team(["UTIL"], ["o"], {"o": ["SF"]}, locked={0: 40})
    decision = policy.decide(me, opp, 1, n=100, risk=policy.RISK,
                             forbidden_locks={0} if forbidden else ())
    assert decision.locks == expected


@pytest.mark.parametrize("threshold,forbidden,expected", [(44, False, True), (45, False, True),
                                                        (46, False, False), (44, True, False)])
def test_last_finished_game_respects_threshold_and_pending_claims(home, threshold, forbidden, expected):
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    wk = FakeWk(3600_000, str(ctx.dir))
    wk.days = []
    me = one_lock_team(False)
    me.lock_at = {"p": threshold}
    cycle._locks(ctx, wk, me, Team([], [], {}), {"p"} if forbidden else ())
    assert any(a.startswith("Would lock") for a in ctx.acted) is expected


@pytest.mark.parametrize("change,expected", [("none", False), ("replace", True), ("delete", True)])
def test_cheap_gate_detects_directive_changes_and_deletion(home, monkeypatch, change, expected):
    from sleeper_gm import directives
    directory = config.league_state("fake")
    path = directory / "directives.json"
    path.write_text(json.dumps([instruction("keep")]))
    original = {"roster_id": 1, "players": ["cut"], "starters": ["cut"], "reserve": []}
    saved = {"leagues": ["fake"], "brief": {"fake": cli._today()}, "rosters": {"fake": {
        "rid": 1, "hash": cli.roster_hash(original), "full_ms": ms("2026-10-29T12:00"),
        "directives": directives.fingerprint(directory)}}}
    (config.STATE_DIR / "run.json").write_text(json.dumps(saved))
    if change == "replace":
        path.write_text(json.dumps([instruction("avoid", "add")]))
    elif change == "delete":
        path.unlink()
    monkeypatch.setattr(api, "rest", lambda path, **kw: [original] if path.endswith("/rosters")
                        else {"season": "2026", "leg": 2})
    monkeypatch.setattr(api, "scores", lambda *a: [])
    assert cli.due({**config.load(), "brief_at_et": "23:59"}, ms("2026-10-29T13:00")) == (expected, False)


@pytest.mark.parametrize("checkpoint", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_gate_checks_known_leagues_without_checkpoint_and_ignores_old_directories(home, monkeypatch, checkpoint, changed):
    directory = config.league_state("fake")
    old_directory = config.league_state("old-unmanaged")
    old_directory.joinpath("directives.json").write_text(json.dumps([instruction("keep")]))
    roster = {"roster_id": 1, "players": ["cut"], "starters": ["cut"], "reserve": []}
    saved = {"leagues": ["fake"], "brief": {"fake": cli._today()}}
    if checkpoint:
        saved["rosters"] = {"fake": {"rid": 1, "hash": cli.roster_hash(roster),
                            "full_ms": ms("2026-10-29T12:00"), "directives": ""}}
    (config.STATE_DIR / "run.json").write_text(json.dumps(saved))
    if changed:
        directory.joinpath("directives.json").write_text(json.dumps([instruction("keep")]))
    monkeypatch.setattr(api, "rest", lambda path, **kw: [roster] if path.endswith("/rosters")
                        else {"season": "2026", "leg": 2})
    monkeypatch.setattr(api, "scores", lambda *a: [])
    assert cli.due({**config.load(), "brief_at_et": "23:59"}, ms("2026-10-29T13:00")) == (changed, False)


@pytest.mark.parametrize("season,number,now,active", [
    ("2026", 2, "2026-10-30T21:40", True), ("2026", 3, "2026-10-30T21:40", False),
    ("2027", 2, "2026-10-30T21:40", False), ("2026", 2, "2026-11-02T00:00", False),
])
def test_cycle_load_filters_expired_locks_and_publishes_player_catalog(home, monkeypatch, season, number, now, active):
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    wk = live_week()
    wk.season, wk.week, wk.now_ms, wk.state_dir = season, number, ms(now), str(ctx.dir)
    wk.model.ros_value = lambda *a: 20
    rows = [instruction("keep", "a"), instruction("lock_at", "a", points=3,
            season="2026", week=2, expires_at=ms("2026-11-02T00:00"))]
    (ctx.dir / "directives.json").write_text(json.dumps(rows))
    monkeypatch.setattr(week, "load", lambda *a, **kw: wk)
    loaded, me, _opp = cycle.load(ctx)
    assert len(loaded.directives) == (2 if active else 1)
    assert me.lock_at == ({"a": 3} if active else {})
    assert json.loads((ctx.dir / "players.json").read_text()) == {
        "a": {"name": "Alex Example"}, "b": {"name": "Blair Example"}}


@pytest.mark.parametrize("active", [False, True])
def test_snapshot_and_status_show_active_instructions_even_without_decision(home, monkeypatch, capsys, active):
    wk = live_week()
    wk.days = []
    wk.protected = set()
    wk.directives = [instruction("keep", "a")] if active else []
    me, opp = week.build_team(wk, 1), week.build_team(wk, 2)
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    monkeypatch.setattr(api, "rest", lambda path, **kw: {"season": "2026"} if "state/nba" in path else [])
    monkeypatch.setattr(claims, "pending", lambda ctx: [])
    monkeypatch.setattr(cycle.crowd, "signals", lambda *a, **kw: {})
    snapshot = cycle.snapshot(ctx, wk, me, opp, None, me.starters, {})
    assert snapshot["directives"] == wk.directives
    assert json.loads((ctx.dir / "now.json").read_text())["directives"] == wk.directives
    monkeypatch.setattr(cli, "store", lambda: None)
    monkeypatch.setattr(cli, "ensure_history", lambda *a: None)
    monkeypatch.setattr(cli, "discover", lambda cfg: [("77", 1, "Demo")])
    monkeypatch.setattr(cycle, "load", lambda ctx: (wk, me, opp))
    cli.cmd_status(SimpleNamespace(league=None))
    assert ("keep a" in capsys.readouterr().out) is active


@pytest.mark.parametrize("pending_drop", [False, True])
@pytest.mark.parametrize("caller", ["preview", "status"])
def test_read_only_decisions_honor_claim_safety_over_lock_instruction(home, monkeypatch, capsys, pending_drop, caller):
    wk = live_week()
    wk.protected = set()
    wk.league.update(roster_positions=["UTIL", "BN"], settings={})
    me, opp = week.build_team(wk, 1), week.build_team(wk, 2)
    me.lock_at = {"a": 3}
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    monkeypatch.setattr(cycle, "load", lambda ctx: (wk, me, opp))
    monkeypatch.setattr(claims, "pending", lambda ctx: [row(drop="a")] if pending_drop else [])
    monkeypatch.setattr(api, "rest", lambda path, **kw: {"season": "2026"} if "state/nba" in path else [])
    monkeypatch.setattr(cycle.crowd, "signals", lambda *a, **kw: {})
    if caller == "preview":
        snapshot, _wk, _me = cycle.preview(ctx, with_moves=False)
        assert bool(snapshot["would"]["lock"]) is (not pending_drop)
    else:
        monkeypatch.setattr(cli, "store", lambda: None)
        monkeypatch.setattr(cli, "ensure_history", lambda *a: None)
        monkeypatch.setattr(cli, "discover", lambda cfg: [("77", 1, "Demo")])
        cli.cmd_status(SimpleNamespace(league=None))
        assert ("LOCK" in capsys.readouterr().out) is (not pending_drop)


@pytest.mark.parametrize("kind", [None, "keep", "avoid"])
def test_owner_claim_conflicts_are_reported_without_editing_owner_queue(home, monkeypatch, kind):
    wk, me, _opp, _roster = fixture(monkeypatch)
    wk.directives = [instruction(kind, "cut" if kind == "keep" else "add")] if kind else []
    owner = [row(add="add", drop="cut")]
    before = copy.deepcopy(owner)
    monkeypatch.setattr(claims, "pending", lambda ctx: owner)
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    cycle._claims_check(ctx, wk, me)
    assert any("instruction" in message for message in ctx.acted) is bool(kind)
    assert owner == before


@pytest.mark.parametrize("starting_at_tip,benched_since,expected", [
    (True, False, [0]), (False, False, []), (True, True, []),
])
def test_lock_instruction_cannot_restore_ineligible_game(home, monkeypatch, starting_at_tip, benched_since, expected):
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    wk = live_week()
    wk.state_dir = str(ctx.dir)
    wk.model.ros_value = lambda *a: 20
    (ctx.dir / "directives.json").write_text(json.dumps([instruction("lock_at", "a", points=3,
        season="2026", week=2, expires_at=ms("2026-11-02T00:00"))]))
    snapshots = [{"week": 2, "t": wk.games[0].start_ms - 1000,
                  "starters": ["a"] if starting_at_tip else ["b"]}]
    if benched_since:
        snapshots.append({"week": 2, "t": wk.games[0].start_ms + 1000, "starters": ["b"]})
    (ctx.dir / "snapshots.jsonl").write_text("\n".join(json.dumps(r) for r in snapshots))
    monkeypatch.setattr(week, "load", lambda *a, **kw: wk)
    loaded, me, opp = cycle.load(ctx)
    assert policy.decide(me, opp, len(loaded.days), n=100).locks == expected


@pytest.mark.parametrize("threshold", [None, 3, 7])
def test_snapshot_distinguishes_owner_lock_threshold_from_model_value(home, monkeypatch, threshold):
    wk = live_week()
    me, opp = week.build_team(wk, 1), week.build_team(wk, 2)
    me.lock_at = {"a": threshold} if threshold is not None else {}
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    monkeypatch.setattr(api, "rest", lambda *a, **kw: [])
    monkeypatch.setattr(cycle.crowd, "signals", lambda *a, **kw: {})
    dec = policy.decide(me, opp, len(wk.days), n=100)
    snapshot = cycle.snapshot(ctx, wk, me, opp, dec, me.starters, {})
    slot = snapshot["me"]["slots"][0]
    assert slot.get("owner_lock_at") == threshold
    assert ("owner_lock_at" in slot) is (threshold is not None)
    assert slot["open_value"] == 20


@pytest.mark.parametrize("sensitive", [False, True])
def test_player_catalog_redacts_saved_names(home, monkeypatch, sensitive):
    from sleeper_gm import auth
    ctx = cycle.Ctx(config.load(), "77", 1, False, None)
    wk = live_week()
    wk.state_dir = str(ctx.dir)
    wk.model.ros_value = lambda *a: 20
    wk.players["a"]["first_name"] = "catalog-private-sentinel" if sensitive else "Alex"
    monkeypatch.setattr(auth, "_cached", lambda: "catalog-private-sentinel")
    monkeypatch.setattr(week, "load", lambda *a, **kw: wk)
    cycle.load(ctx)
    catalog = json.loads((ctx.dir / "players.json").read_text())
    assert catalog["a"]["name"] == ("<token> Example" if sensitive else "Alex Example")


@pytest.mark.parametrize("changed", [False, True])
def test_full_run_saves_start_fingerprint_so_mid_run_edits_stay_due(home, monkeypatch, changed):
    from sleeper_gm import directives, review
    from test_safety import _snapshot_for_cycle
    directory = config.league_state("fake")
    path = directory / "directives.json"
    path.write_text(json.dumps([instruction("keep")]))
    before = directives.fingerprint(directory)
    wk, me, opp, roster = fixture(monkeypatch)
    wk.days = []
    monkeypatch.setattr(api, "rest", lambda path, **kw: [roster] if path.endswith("/rosters")
                        else {"season": "2026", "leg": 2})
    monkeypatch.setattr(api, "scores", lambda *a: [])
    monkeypatch.setattr(api, "league_transactions", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "store", lambda: SimpleNamespace(db=SimpleNamespace(
        execute=lambda *a: SimpleNamespace(fetchone=lambda: (1,)))))
    monkeypatch.setattr(cli, "ensure_history", lambda *a: None)
    monkeypatch.setattr(cli, "discover", lambda cfg: [("fake", 1, "Demo")])
    monkeypatch.setattr(cli, "brief_due", lambda *a: False)
    monkeypatch.setattr(review, "backfill", lambda *a: [])
    monkeypatch.setattr(cycle, "snapshot", _snapshot_for_cycle)
    def load(ctx):
        if changed:
            path.write_text(json.dumps([instruction("avoid", "add")]))
        return wk, me, opp
    monkeypatch.setattr(cycle, "load", load)
    monkeypatch.setattr(cli.time, "time", lambda: ms("2026-10-29T13:00") / 1000)
    cli.cmd_run(SimpleNamespace(live=False, force=True, brief=False, league=None))
    saved, run_path = cli._run_state()
    assert saved["rosters"]["fake"]["directives"] == before
    saved["brief"] = {"fake": cli._today()}
    run_path.write_text(json.dumps(saved))
    assert cli.due({**config.load(), "brief_at_et": "23:59"}, ms("2026-10-29T13:30")) == (changed, False)


def future_threshold_team(threshold, inflight=True, p_play=1., samples=(30.,)):
    return Team(["UTIL"], ["p"], {"p": ["SF"]},
                inflight={0: "p"} if inflight else {}, frozen={"p"} if inflight else set(),
                games={"p": {0: Draw(p_play, np.array(samples)), 1: Draw(1., np.array([60.]))}},
                lock_at={"p": threshold} if threshold is not None else {})


@pytest.mark.parametrize("threshold,inflight,total,p_win", [
    (25, True, 30, 0), (30, True, 30, 0), (35, True, 60, 1),
    (25, False, 60, 1), (35, False, 60, 1), (None, True, 60, 1),
])
def test_future_owner_threshold_changes_projected_total_and_win_chance(threshold, inflight, total, p_win):
    me = future_threshold_team(threshold, inflight)
    opp = Team(["UTIL"], ["o"], {"o": ["SF"]}, locked={0: 45})
    dec = policy.decide(me, opp, 2, n=100, risk=policy.RISK)
    assert dec.plan.value == pytest.approx(total)
    assert dec.mean_total == pytest.approx(total)
    assert dec.p_win == p_win
    if threshold == 25 and not inflight:
        assert dec.plan.choice[0][0] is None


@pytest.mark.parametrize("threshold,p_play,value", [(25, 1., 140 / 3), (35, 1., 170 / 3),
                                                   (25, .5, 160 / 3), (35, .5, 175 / 3)])
def test_future_threshold_dp_realization_and_simulation_agree(threshold, p_play, value):
    me = future_threshold_team(threshold, p_play=p_play, samples=(10., 30., 50.))
    plan = policy.make_plan(me, 2)
    realized = policy.realize(me, plan, 2)
    assert plan.value == pytest.approx(value)
    assert realized.value == pytest.approx(value)
    assert policy.simulate(me, plan, [], 80_000, 42).mean() == pytest.approx(value, abs=.2)


@pytest.mark.parametrize("threshold,total", [(25, 30), (30, 30), (35, 60)])
@pytest.mark.parametrize("tilt", [-100., 100.])
def test_owner_future_threshold_is_not_shifted_by_matchup_tilt(threshold, total, tilt):
    me = future_threshold_team(threshold)
    plan = policy.make_plan(me, 2)
    assert policy.simulate(me, plan, [], 100, 42, tilt=tilt).tolist() == [total] * 100


@pytest.mark.parametrize("threshold,p_play,value", [(25, 1., 30), (35, 1., 30), (35, .5, 15), (35, 0., 0)])
def test_owner_threshold_cannot_skip_terminal_score_or_dnp(threshold, p_play, value):
    me = future_threshold_team(threshold, p_play=p_play)
    me.games["p"].pop(1)
    plan = policy.make_plan(me, 1)
    assert plan.value == pytest.approx(value)
    assert policy.simulate(me, plan, [], 80_000, 42, tilt=100).mean() == pytest.approx(value, abs=.2)


@pytest.mark.parametrize("threshold,value", [(25, 30), (35, 57)])
def test_owner_threshold_respects_same_day_handoff_branches(threshold, value):
    from test_sameday import pair
    me = pair()
    me.inflight, me.frozen, me.lock_at = {0: "a"}, {"a"}, {"a": threshold}
    me.games["a"][0].samples = np.array([30.])
    me.games["b"][0].samples = np.array([60.])
    plan = policy.make_plan(me, 1)
    assert plan.value == pytest.approx(value)
    assert policy.realize(me, plan, 1).value == pytest.approx(value)
    assert policy.simulate(me, plan, [], 80_000, 42).mean() == pytest.approx(value, abs=.2)


@pytest.mark.parametrize("threshold,bar", [(None, 60), (25, 25), (35, 35)])
def test_future_plan_details_publish_effective_owner_threshold(threshold, bar):
    me = future_threshold_team(threshold)
    dec = policy.decide(me, Team([], [], {}), 2, n=100)
    wk = SimpleNamespace(name=lambda p: p, days=["2026-10-29", "2026-10-30"])
    looks = cycle.plan_details(wk, me, dec)[0]["looks"]
    assert looks[0]["lock_above"] == bar
    assert looks[0]["terminal"] is False
    assert looks[1]["lock_above"] == 0
    assert looks[1]["terminal"] is True


@pytest.mark.parametrize("threshold,p_play,value,dnp_locks", [
    (0, 0., 0, [0]), (0, .5, 15, [0]), (0, 1., 30, [0]),
    (25, 0., 60, []), (25, .5, 45, []), (25, 1., 30, []),
])
def test_zero_threshold_dnp_matches_pending_and_future_policy(threshold, p_play, value, dnp_locks):
    me = future_threshold_team(threshold, p_play=p_play)
    plan = policy.make_plan(me, 2)
    assert plan.value == pytest.approx(value)
    assert policy.realize(me, plan, 2).value == pytest.approx(value)
    assert policy.simulate(me, plan, [], 80_000, 42).mean() == pytest.approx(value, abs=.2)
    me.inflight, me.frozen = {}, set()
    me.pending = {0: ("p", 0., "dnp")}
    me.games["p"].pop(0)
    assert policy.decide(me, Team([], [], {}), 2, n=100).locks == dnp_locks
