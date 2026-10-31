"""The gap between a week's last game and Sleeper opening the next one."""
import json
from types import SimpleNamespace

import pytest

from sleeper_gm import api, cli, config, cycle, moves, week
from sleeper_gm.policy import Team
from test_public import home  # noqa: F401
from test_week import FakeModel, H, NOW


def row(gid, start, status, home="AAA"):
    return {"game_id": gid, "start_time": start, "status": status,
            "metadata": {"home_team": {"team": home}, "away_team": {"team": "CCC"}}}


def stub(monkeypatch, left_this_week=False, opponent_next=True):
    league = {"season": "2026", "roster_positions": ["UTIL", "BN"], "scoring_settings": {"pts": 1.}}
    players = {p: {"first_name": p, "last_name": "", "team": "AAA", "fantasy_positions": ["SF"]} for p in ("a", "b")}
    sched = {2: [row("w2", NOW - 30 * H, "complete")] + ([row("w2late", NOW + 2 * H, "pre_game")] if left_this_week else []),
             3: [row("w3", NOW + 20 * H, "pre_game")]}
    legs = {2: [{"roster_id": 9, "matchup_id": 1, "starters": ["a"]}, {"roster_id": 1, "matchup_id": 1, "starters": []}],
            3: [{"roster_id": 9, "matchup_id": 2, "starters": ["b"]}]
               + ([{"roster_id": 4, "matchup_id": 2, "starters": []}] if opponent_next else [])}
    monkeypatch.setattr(api, "rest", lambda path, **kw: {"leg": 2} if path == "/v1/state/nba" else [] if path.startswith("/stats") else league)
    monkeypatch.setattr(api, "scores", lambda season, w: sched.get(w, []))
    monkeypatch.setattr(api, "league_rosters", lambda *a: [{"roster_id": 9, "players": ["a", "b"], "reserve": []},
                                                            {"roster_id": 1, "players": []}, {"roster_id": 4, "players": []}])
    monkeypatch.setattr(api, "legs", lambda league_id, w: [dict(l) for l in legs.get(w, [])])
    monkeypatch.setattr(api, "legs_with_players", lambda league_id, w: [dict(l) for l in legs.get(w, [])])
    monkeypatch.setattr(week.time, "time", lambda: NOW / 1000)
    return SimpleNamespace(players=lambda: players, injuries=lambda: {})


@pytest.mark.parametrize("left_this_week,opponent_next,ahead", [(False, True, True), (True, True, False), (False, False, False)])
def test_plans_next_week_once_this_one_is_over(tmp_path, monkeypatch, left_this_week, opponent_next, ahead):
    store = stub(monkeypatch, left_this_week, opponent_next)
    wk = week.load(store, lambda wk: FakeModel(), "L", 9, now_ms=NOW, state_dir=str(tmp_path))
    assert (wk.week, wk.ahead) == ((3, True) if ahead else (2, False))
    assert wk.held == (frozenset({"a"}) if ahead else frozenset())
    team = week.build_team(wk, 9)
    assert team.held == ({"a"} if ahead else set())
    if ahead:
        # Last week's starter stays untouchable until Sleeper opens the new week; the bench guy does not.
        assert moves.busy(team, "a") and not moves.busy(team, "b")
        assert wk.days == [week.et_date(NOW + 20 * H)] and wk.opp_rid == 4
        assert json.loads((tmp_path / "snapshots.jsonl").read_text().splitlines()[-1])["week"] == 3


@pytest.mark.parametrize("ahead,leg,tip_in,message", [
    (True, 2, 20, None),
    (True, 2, 2, "NEEDS YOU: Sleeper has not opened week 3 yet, so no lineup can be saved for it"),
    (False, 4, 20, "Skipped writes: week rolled over"),
])
def test_lineup_saves_wait_for_sleeper_to_open_the_week(monkeypatch, ahead, leg, tip_in, message):
    wk = SimpleNamespace(season="2026", week=3, ahead=ahead, games=[week.Game("w3", "2026-11-02", NOW + tip_in * H, "pre_game", "AAA", "CCC")])
    monkeypatch.setattr(api, "scores", lambda *a: [])
    monkeypatch.setattr(api, "rest", lambda path, **kw: {"leg": leg})
    monkeypatch.setattr(cycle.time, "time", lambda: NOW / 1000)
    ctx = SimpleNamespace(acted=[])
    assert cycle._refresh_write_guards(ctx, wk) is False
    assert ctx.week_rolled_over and ctx.acted == ([message] if message else [])


@pytest.mark.parametrize("next_tip_in,expected", [(2, True), (10, False)])
def test_gate_watches_next_week_after_the_last_game(home, monkeypatch, next_tip_in, expected):
    config.STATE_DIR.mkdir(parents=True)
    (config.STATE_DIR / "run.json").write_text(json.dumps({"last_full_ms": NOW, "brief_date": cli._today()}))
    sched = {2: [row("w2", NOW - 30 * H, "complete")], 3: [row("w3", NOW + next_tip_in * H, "pre_game")]}
    monkeypatch.setattr(api, "rest", lambda path, fresh=False: {"season": "2026", "leg": 2})
    monkeypatch.setattr(api, "scores", lambda season, w: sched[w])
    assert cli.due({**config.load(), "brief_at_et": "23:59"}, NOW) == (expected, False)


def test_held_player_is_never_moved_to_ir(monkeypatch):
    wk = SimpleNamespace(league={"roster_positions": ["UTIL", "BN", "IR"], "settings": {"reserve_slots": 1, "reserve_allow_out": 1}},
                         players={"a": {"injury_status": "Out"}, "b": {"injury_status": "Out"}}, injuries={},
                         games=[], name=lambda p: p, protected=set(), extra={})
    team = Team(["UTIL"], ["0"], {"a": ["SF"], "b": ["SF"]}, held={"a"})
    move = moves.ir_fix(wk, team, {"players": ["a", "b"], "reserve": []})
    assert move.reserve == ["b"]
