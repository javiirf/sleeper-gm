import numpy as np
import pytest

from sleeper_gm.week import Game, Week, build_team, started_through

H = 3600 * 1000
NOW = 100 * H


class FakeModel:
    def dist(self, pid, game):
        class D:
            p_play = 0.9
            samples = np.full(50, 20.0)
            mean = 20.0
        return D()


def make_week(games, box, starters, locks=None, now=NOW):
    players = {p: {"first_name": p, "last_name": "", "team": "AAA", "fantasy_positions": ["SF"]} for p in ("a", "b")}
    by_team = {"AAA": sorted(games, key=lambda g: g.start_ms)}
    days = sorted({g.date for g in games if g.start_ms > now or g.status != "complete"})
    wk = Week("2026", 2, now, {}, {"pts": 1.0}, ["SF", "UTIL"], games, by_team, days, box, players, {},
              {9: {"players": ["a", "b"], "reserve": []}}, {9: {"starters": starters, "starters_games": locks or {}, "matchup_id": 1}},
              9, 1)
    wk.model = FakeModel()
    return wk


def g(gid, date, start, status):
    return Game(gid, date, start, status, "AAA", "BBB")


@pytest.mark.parametrize("snaps,expect", [
    ([], None),                                                                   # never saw it: unknown, tried alone
    ([{"t": NOW - 10 * H, "week": 2, "starters": ["a"]}], True),                  # starting at tip
    ([{"t": NOW - 10 * H, "week": 2, "starters": ["b"]}], False),                 # on the bench at tip
    ([{"t": NOW - 10 * H, "week": 2, "starters": ["a"]}, {"t": NOW - 2 * H, "week": 2, "starters": ["b"]}], False),  # benched after
    ([{"t": NOW - 10 * H, "week": 1, "starters": ["b"]}], None),                  # last week's lineup does not count
])
def test_started_through(snaps, expect):
    assert started_through("a", NOW - 5 * H, 2, snaps) is expect


def test_traded_player_keeps_his_old_teams_game_and_skips_his_new_teams_earlier_one():
    # Review finding: he scored 45 for AAA Monday, joined BBB Wednesday; BBB played Tuesday without him.
    a1 = Game("g1", "2026-10-26", NOW - 50 * H, "complete", "AAA", "CCC")
    b1 = Game("g2", "2026-10-27", NOW - 30 * H, "complete", "BBB", "DDD")
    b2 = Game("g3", "2026-10-30", NOW + 40 * H, "pre_game", "BBB", "EEE")
    wk = make_week([a1, b1, b2], {("a", "g1"): {"pts": 45.0}, ("x", "g2"): {"pts": 3.0}}, ["a", "0"])
    wk.players["a"]["team"] = "BBB"
    wk.players["a"]["team_changed_at"] = NOW - 20 * H
    wk.by_team = {"AAA": [a1], "BBB": [b1, b2]}
    t = build_team(wk, 9)
    assert t.pending[0][1] == 45.0 and t.pending[0][2] == "g1"


def test_locked_slot_scores_its_locked_game():
    games = [g("g1", "2026-10-26", NOW - 50 * H, "complete"), g("g2", "2026-10-28", NOW - 5 * H, "complete"),
             g("g3", "2026-10-30", NOW + 40 * H, "pre_game")]
    wk = make_week(games, {("a", "g1"): {"pts": 31.0}, ("a", "g2"): {"pts": 12.0}}, ["a", "0"], locks={"a": "g1"})
    t = build_team(wk, 9)
    assert t.locked == {0: 31.0} and not t.pending


@pytest.mark.parametrize("box,expect", [
    ({("a", "g2"): {"pts": 27.0}, ("b", "g2"): {"pts": 3.0}}, ("pending", 27.0)),   # box landed: lockable 27
    ({("b", "g2"): {"pts": 3.0}}, ("pending", 0.0)),                                  # teammates have rows, he does not: DNP
    ({}, ("held", None)),                                                            # no rows for the game yet: hold
])
def test_finished_game_is_lockable_only_once_its_box_score_exists(box, expect):
    games = [g("g2", "2026-10-28", NOW - 5 * H, "complete"), g("g3", "2026-10-30", NOW + 40 * H, "pre_game")]
    t = build_team(make_week(games, box, ["a", "0"]), 9)
    kind, x = expect
    if kind == "pending":
        assert t.pending[0][1] == x and 0 not in t.inflight
    else:
        assert 0 in t.inflight and 0 not in t.pending and "a" in t.frozen


def test_in_game_player_is_frozen_and_drawing_today():
    games = [g("g2", "2026-10-28", NOW - 1 * H, "in_game"), g("g3", "2026-10-30", NOW + 40 * H, "pre_game")]
    t = build_team(make_week(games, {}, ["a", "0"]), 9)
    assert t.inflight == {0: "a"} and "a" in t.frozen and 0 in t.games["a"]


@pytest.mark.parametrize("next_status,final", [("pre_game", False), (None, True)])
def test_final_done_when_no_games_left(next_status, final):
    games = [g("g2", "2026-10-28", NOW - 5 * H, "complete")]
    if next_status:
        games.append(g("g3", "2026-10-30", NOW + 40 * H, next_status))
    t = build_team(make_week(games, {("a", "g2"): {"pts": 20.0}}, ["a", "0"]), 9)
    assert ("a" in t.final_done) is final


@pytest.mark.parametrize("dictionary_catches_up,authoritative_time", [(False, None), (True, None), (False, NOW - 2 * H)])
def test_team_transition_time_survives_reloads(tmp_path, monkeypatch, dictionary_catches_up, authoritative_time):
    import copy
    import json
    from sleeper_gm import api, week
    from types import SimpleNamespace
    current = {"t": NOW, "team": "AAA"}
    players = {"a": {"first_name": "a", "last_name": "", "team": "AAA", "fantasy_positions": ["SF"]}}
    league = {"season": "2026", "roster_positions": ["UTIL"], "scoring_settings": {"pts": 1.}}
    games = [
        {"game_id": "old", "start_time": NOW - 50 * H, "status": "complete", "metadata": {"home_team": {"team": "AAA"}, "away_team": {"team": "CCC"}}},
        {"game_id": "before_trade", "start_time": NOW - 30 * H, "status": "complete", "metadata": {"home_team": {"team": "BBB"}, "away_team": {"team": "CCC"}}},
        {"game_id": "dnp", "start_time": NOW + H, "status": "pre_game", "metadata": {"home_team": {"team": "BBB"}, "away_team": {"team": "CCC"}}},
        {"game_id": "next", "start_time": NOW + 40 * H, "status": "pre_game", "metadata": {"home_team": {"team": "BBB"}, "away_team": {"team": "CCC"}}},
    ]
    boxes = [{"player_id": "a", "game_id": "old", "stats": {"pts": 45}}, {"player_id": "x", "game_id": "before_trade", "stats": {"pts": 10}}]
    info = {"team": "BBB", "injury_status": None}
    if authoritative_time is not None: info["team_changed_at"] = authoritative_time
    legs = [{"roster_id": 9, "matchup_id": 1, "starters": ["a"], "player_map": {"a": info}}, {"roster_id": 1, "matchup_id": 1, "starters": []}]
    monkeypatch.setattr(api, "rest", lambda path, **kw: {"leg": 2} if path == "/v1/state/nba" else boxes if path.startswith("/stats") else league)
    monkeypatch.setattr(api, "scores", lambda *a: games)
    monkeypatch.setattr(api, "league_rosters", lambda *a: [{"roster_id": 9, "players": ["a"], "reserve": []}, {"roster_id": 1, "players": []}])
    monkeypatch.setattr(api, "legs_with_players", lambda *a: copy.deepcopy(legs))
    monkeypatch.setattr(week.time, "time", lambda: current["t"] / 1000)
    store = SimpleNamespace(players=lambda: players, injuries=lambda: {})
    first = week.load(store, lambda wk: FakeModel(), "L", 9, now_ms=NOW, state_dir=str(tmp_path))
    assert build_team(first, 9).pending[0] == ("a", 45., "old")
    expected_time = authoritative_time or NOW
    persisted = {"a": {"team": "BBB", "t": expected_time, "week": 2, "season": "2026"}}
    assert json.loads((tmp_path / "team_moves.json").read_text()) == persisted
    current["t"] = NOW + 3 * H
    games[2]["status"] = "complete"
    boxes.append({"player_id": "x", "game_id": "dnp", "stats": {"pts": 10}})
    second = week.load(store, lambda wk: FakeModel(), "L", 9, now_ms=current["t"], state_dir=str(tmp_path))
    assert second.players["a"]["team_changed_at"] == expected_time
    assert build_team(second, 9).pending[0] == ("a", 0., "dnp")
    if dictionary_catches_up: players["a"]["team"] = "BBB"
    third = week.load(store, lambda wk: FakeModel(), "L", 9, now_ms=current["t"] + 1, state_dir=str(tmp_path))
    assert third.players["a"]["team_changed_at"] == expected_time
    assert json.loads((tmp_path / "team_moves.json").read_text()) == persisted
