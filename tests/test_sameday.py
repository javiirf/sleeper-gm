"""Same-day policy fixtures use fictional players and actual scheduler minutes."""
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sleeper_gm import cli, cycle, policy, week
from sleeper_gm.policy import Decision, Draw, Team, lineup_today, make_plan, realize, simulate

MIN = 60_000


def tip(hour, minute=0, date="2026-10-30"):
    return int(datetime.fromisoformat(f"{date}T{hour:02}:{minute:02}").replace(tzinfo=week.ET).timestamp() * 1000)


def timed(values, when, p=1.0):
    return Draw(p, np.asarray(values, dtype=float), tip_ms=when)


def pair(gap=3, p_b=1.0):
    return Team(["UTIL"], ["a"], {"a": ["SF"], "b": ["PG"]}, games={
        "a": {0: timed([10, 40], tip(19))},
        "b": {0: timed([20, 20], tip(19 + gap), p_b)},
    })


@pytest.mark.parametrize("gap,expected", [(0, ["a"]), (1, ["a"]), (3, ["a", "b"])])
def test_same_day_looks_need_time_for_a_scheduled_swap(gap, expected):
    team = pair(gap)
    plan = make_plan(team, 1)
    assert [p for _i, p, _d, _t in plan.looks(0)] == expected
    if gap == 3:
        # 90% gets max(A, 20) = 30; delayed 10% keeps A = 25.
        assert plan.value == pytest.approx(29.5)
        daily = pair(gap)
        for games in daily.games.values():
            for dr in games.values():
                dr.tip_ms = None
        assert plan.value > make_plan(daily, 1).value
    else:
        assert plan.value == 25


@pytest.mark.parametrize("hour,minute,fits", [(21, 49, False), (21, 50, True), (22, 0, True)])
def test_cadence_and_lead_boundary(hour, minute, fits):
    team = pair()
    team.games["b"][0].tip_ms = tip(hour, minute)
    assert (len(list(make_plan(team, 1).looks(0))) == 2) is fits


@pytest.mark.parametrize("p_b", [1.0, 0.5, 0.0])
def test_event_simulation_and_realization_agree_with_dp(p_b):
    team = pair(p_b=p_b)
    if p_b == 0.5:
        team.games["a"][0].samples[0] = 5  # below B's 10-point continuation, not a locking tie
    plan = make_plan(team, 1)
    actual = realize(team, plan, 1)
    assert actual.value == pytest.approx(plan.value)
    samples = simulate(team, actual, [], 150_000, 81)
    assert samples.mean() == pytest.approx(plan.value, abs=0.12)
    if p_b == 0.5:
        # Passing A for B loses A, including when B sits in his terminal game.
        assert 0 in samples and 5 in samples and 40 in samples


def live_week(state="complete", score=5, box=True):
    a = week.Game("early", "2026-10-30", tip(19), state, "AAA", "CCC")
    b = week.Game("late", "2026-10-30", tip(22), "pre_game", "BBB", "DDD")
    players = {p: {"first_name": "Alex" if p == "a" else "Blair", "last_name": "Example",
                   "team": t, "fantasy_positions": ["SF"]} for p, t in [("a", "AAA"), ("b", "BBB")]}
    wk = week.Week("2026", 2, tip(21, 40), {"name": "Demo"}, {"pts": 1.0}, ["UTIL"],
                   [a, b], {"AAA": [a], "BBB": [b]}, ["2026-10-30"],
                   {("a", "early"): {"pts": score}} if box else {}, players, {},
                   {1: {"players": ["a", "b"]}, 2: {"players": []}},
                   {1: {"starters": ["a"]}, 2: {"starters": ["0"]}}, 1, 2)
    wk.model = SimpleNamespace(dist=lambda *args: SimpleNamespace(p_play=1.0, samples=np.array([20., 20.])))
    return wk


@pytest.mark.parametrize("state,box,score,expected", [
    ("in_game", True, 5, "a"), ("complete", False, 5, "a"),
    ("complete", True, 5, "b"), ("complete", True, 40, "a"),
])
def test_between_games_handoff_waits_for_final_box_and_pass(state, box, score, expected):
    wk = live_week(state, score, box)
    me = week.build_team(wk, 1)
    opp = Team(["UTIL"], ["o"], {"o": ["SF"]}, locked={0: 0})
    dec = policy.decide(me, opp, 1, flip_margin=1.0)
    assert lineup_today(me, dec, wk.has_game_today) == [expected]
    assert (0 in dec.locks) is (state == "complete" and box and score == 40)
    assert me.games["b"][0].tip_ms == tip(22)

    # Exercise the runner's actual decision/write-selection path in dry mode.
    ctx = SimpleNamespace(live=False, acted=[])
    _dec, starters, confirmed = cycle._locks(ctx, wk, me, opp)
    assert starters == [expected] and confirmed == {}


@pytest.mark.parametrize("later_value,expected", [(10, "a"), (30, "b")])
def test_passed_final_score_only_survives_without_a_replacement(later_value, expected):
    me = pair()
    me.games = {"b": {0: timed([later_value], tip(22))}}
    me.pending = {0: ("a", 18., "early")}
    me.final_done = {"a"}
    plan = make_plan(me, 1)
    dec = Decision([], [0], plan, 0, {}, {}, 0, 0)
    assert lineup_today(me, dec, lambda p: p == "b") == [expected]
    assert simulate(me, plan, [], 500, 1).mean() == (18 if expected == "a" else later_value)


@pytest.mark.parametrize("offset,expected", [(-20, True), (-50, True), (-200, False)])
def test_due_fires_on_real_cadence_before_late_tip(monkeypatch, offset, expected):
    now = tip(22) + offset * MIN  # 21:40, 21:10, 18:40 ET
    assert datetime.fromtimestamp(now / 1000, week.ET).minute in (10, 40)
    monkeypatch.setattr(cli, "_run_state", lambda: ({"brief_date": cli._today()}, None))
    monkeypatch.setattr(cli.api, "rest", lambda *a, **k: {"season": "2026", "leg": 2})
    # Isolate the pre_game gate, rather than letting the completed A trigger it.
    monkeypatch.setattr(cli.api, "scores", lambda *a: [{"start_time": tip(22), "status": "pre_game"}])
    assert cli.due({"brief_at_et": "23:59"}, now) == (expected, False)


@pytest.mark.parametrize("locked", [False, True])
def test_snapshot_keeps_existing_fields_and_explains_bench(tmp_path, monkeypatch, locked):
    wk = live_week()
    wk.now_ms = tip(18)
    wk.games[0].status = "pre_game"
    me = week.build_team(wk, 1)
    me.games["a"][0].samples = np.array([10., 40.])
    if locked:
        me.locked = {0: 40}
    opp = week.build_team(wk, 2)
    plan = make_plan(me, 1)
    dec = Decision([], [], plan, .5, {}, {}, plan.value, 0)
    ctx = SimpleNamespace(league_id="demo", rid=1, live=False, acted=[], dir=tmp_path)
    monkeypatch.setattr(cycle.api, "rest", lambda *a, **k: [])
    snap = cycle.snapshot(ctx, wk, me, opp, dec, me.starters, {})
    row = snap["me"]["slots"][0]
    bench = snap["me"]["bench"][0]
    assert row["player"]["id"] == "a" and bench["games_left"] == 1
    if locked:
        assert "plan" not in row
        assert bench["role"] == "no open slot this week"
    else:
        assert row["plan"]["expected_value"] == 29.5
        assert row["slot_value"] == 29.5
        assert [(x["id"], x["tip_ms"], x["lock_above"]) for x in row["plan"]["looks"]] == [
            ("a", tip(19), 20.), ("b", tip(22), 0.)]
        assert row["plan"]["looks"][-1]["terminal"] is True
        assert bench["role"] == "plays UTIL Fri (if open)"
        assert cycle.notify.plan_line(row) == "UTIL: Example Fri 7pm (lock at 20.0+), then Example Fri 10pm (final)"


@pytest.mark.parametrize("later_game", [False, True])
def test_frozen_bench_game_never_enters_lineup(later_game):
    me = pair()
    me.starters = ["0"]
    me.positions.pop("a")
    me.games.pop("a")
    me.frozen = {"b"}
    if later_game:
        me.games["b"][1] = timed([30.], tip(19, date="2026-10-31"))
    dec = Decision([], [], make_plan(me, 2), 0, {}, {}, 0, 0)
    # The real predicate excludes games that already tipped, including this one.
    assert lineup_today(me, dec, lambda p: False) == ["0"]


@pytest.mark.parametrize("score", [-10., 0., 10.])
def test_known_dnp_final_game_cannot_reuse_an_earlier_unlocked_draw(score):
    me = Team(["UTIL"], ["a"], {"a": ["SF"]}, games={"a": {
        0: timed([score], tip(19)), 1: timed([20.], tip(19, date="2026-10-31"), p=0.)}})
    # With a final DNP ahead, an earlier negative draw can be passed for zero.
    plan = make_plan(me, 2)
    assert plan.value == max(0, score)
    assert simulate(me, plan, [], 500, 4).mean() == max(0, score)
    if score > 0:
        look = next(plan.looks(0))
        assert plan.C[0][plan.after[0][look[0]][0]][1] is False


@pytest.mark.parametrize("before_run", [True, False])
def test_long_game_cannot_plan_a_swap_before_another_run(before_run):
    wk = live_week("in_game")
    wk.now_ms = tip(21, 10) if before_run else tip(21, 40)
    me = week.build_team(wk, 1)
    me.games["a"][0].samples = np.array([10., 40.])
    looks = [p for _i, p, _d, _t in make_plan(me, 1).looks(0)]
    assert looks == (["a", "b"] if before_run else ["a"])


@pytest.mark.parametrize("mode", ["legacy", "empty", "looks"])
def test_board_plan_and_bench_role_render_safely(mode):
    from sleeper_gm import web
    from test_public import _node
    source = (Path(web.__file__).with_suffix("") / "app.js").read_text()
    helpers = source[source.index("  function esc("):source.index("  /* ---------- data")]
    board = source[source.index("  function tag("):source.index("  function oppRow(")]
    row = {"slot": "UTIL", "state": "empty", "player": None}
    if mode != "legacy":
        row["plan"] = {"expected_value": 29.5, "looks": []}
    if mode == "looks":
        row["plan"]["looks"] = [{"id": "a", "name": "Alex <Example>", "tip_ms": tip(19),
                                  "date": "2026-10-30", "lock_above": 20, "terminal": False}]
    player = {"id": "b", "name": "Blair Example", "games_left": 1, "role": "reserve <UTIL>"}
    rendered = _node("var S = {open: {}}, HOUR = 3600000, DAY = 86400000;\n" + helpers + board +
                     "console.log(JSON.stringify(slotRow(" + json.dumps(row) + ", 0, 60, {}) + benchRow(" +
                     json.dumps(player) + ", 'bench', 0)));" )
    assert "reserve &lt;UTIL&gt;" in rendered and "1 game left" in rendered
    assert ("29.5 expected" in rendered) is (mode != "legacy")
    if mode == "looks":
        assert "&lt;Example&gt;" in rendered and "lock at 20.0+" in rendered
        assert "<Example>" not in rendered
    elif mode == "empty":
        assert "No further looks" in rendered


@pytest.mark.parametrize("can_move", [False, True])
def test_plan_follows_open_slot_when_a_lock_is_respecified(can_move):
    wk = live_week(score=40)
    wk.slots = ["SF", "UTIL"]
    me = Team(wk.slots, ["b", "a"], {"a": ["SF"] if can_move else ["PG"], "b": ["SF"]},
              pending={1: ("a", 40., "early")}, final_done={"a"},
              games={"b": {0: timed([20.], tip(22))}})
    dec = Decision([1], [], make_plan(me, 1, locks=[1]), .5, {}, {}, 60, 0)
    starters, locks = policy.respecify_locks(me, me.starters, dec.locks)
    details = cycle.plan_details(wk, me, dec, starters)
    open_slot = 1 if can_move else 0
    assert locks == ([0] if can_move else [1])
    assert list(details) == [open_slot]
    assert details[open_slot]["looks"][0]["id"] == "b"
    assert details[open_slot]["expected_value"] == 20
