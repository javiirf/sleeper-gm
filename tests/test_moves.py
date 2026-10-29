import pytest

from sleeper_gm import moves
from sleeper_gm.policy import Team

LEAGUE = {"roster_positions": ["PG", "C", "BN"], "settings": {"reserve_slots": 1, "reserve_allow_out": 1, "playoff_week_start": 22}}


class FakeModel:
    def __init__(self, ros, avail):
        self._ros, self._avail = ros, avail

    def ros_value(self, p, t):
        return self._ros[p]

    def availability_weeks(self, p, t, horizon_weeks=4):
        return self._avail.get(p, 1.0)


class WK:
    week = 2
    now_ms = 0
    games = []

    def __init__(self, statuses, ros, avail=None, injuries=None, protected=()):
        self.league = LEAGUE
        self.players = {p: {"injury_status": s, "first_name": p, "last_name": ""} for p, s in statuses.items()}
        self.model = FakeModel(ros, avail or {})
        self.injuries = injuries or {}
        self.protected = set(protected)

    def name(self, p):
        return p


def team(starters, lock_games=None):
    t = Team(["PG", "C"], starters, {})
    t.lock_games = lock_games or {}
    return t


@pytest.mark.parametrize("status,eligible", [("Out", True), ("IR", True), (None, False), ("DTD", False)])
def test_ir_eligibility_follows_league_flags(status, eligible):
    assert moves.ir_eligible(LEAGUE, status) is eligible


def test_healthy_ir_player_comes_off_when_there_is_room():
    wk = WK({"a": None, "b": None, "hurt": None}, {"a": 20, "b": 18, "hurt": 25})
    m = moves.ir_fix(wk, team(["a", "b"]), {"players": ["a", "b", "hurt"], "reserve": ["hurt"]})
    assert m.kind == "reserve" and m.reserve == []


@pytest.mark.parametrize("bench_status,expect", [("Out", "reserve"), (None, "drop_then_reserve")])
def test_full_roster_swaps_a_bench_injury_in_or_drops(bench_status, expect):
    # 3 active (full), healthy "back" on IR. "b" is on the BENCH: swap him into IR if he is Out, else cut him.
    wk = WK({"a": None, "b": bench_status, "c": None, "back": None}, {"a": 30, "b": 10, "c": 12, "back": 22})
    m = moves.ir_fix(wk, team(["a", "c"]), {"players": ["a", "b", "c", "back"], "reserve": ["back"]})
    assert m.kind == expect
    assert (m.reserve == ["b"]) if expect == "reserve" else (m.drop == "b" and m.reserve == [])


def test_an_injured_starter_is_never_used_to_unfreeze_ir():
    # the only Out player is a STARTER: benching him needs a lineup save, which the freeze can block
    wk = WK({"a": "Out", "c": None, "b": None, "back": None}, {"a": 30, "b": 9, "c": 12, "back": 22})
    m = moves.ir_fix(wk, team(["a", "c"]), {"players": ["a", "b", "c", "back"], "reserve": ["back"]})
    assert m.kind == "drop_then_reserve" and m.drop == "b"


@pytest.mark.parametrize("protected,expect", [((), "b"), (("b",), "back")])
def test_protected_bench_player_is_never_cut(protected, expect):
    wk = WK({"a": None, "b": None, "c": None, "back": None}, {"a": 30, "b": 5, "c": 12, "back": 22}, protected=protected)
    m = moves.ir_fix(wk, team(["a", "c"]), {"players": ["a", "b", "c", "back"], "reserve": ["back"]})
    assert m.drop == expect


def test_injury_lowers_keep_value_by_time_missed():
    wk = WK({"x": None, "y": "Out"}, {"x": 20, "y": 20}, avail={"y": 0.0})
    assert moves.keep_value(wk, "x") == pytest.approx(20)
    assert moves.keep_value(wk, "y") == pytest.approx(20 * (1 - 4 / 20))


def test_long_term_out_bench_player_is_parked_on_ir():
    wk = WK({"a": None, "c": None, "hurt": "Out"}, {"a": 20, "c": 18, "hurt": 25},
            injuries={"hurt": {"return_date": "2099-01-01"}})
    m = moves.ir_fix(wk, team(["a", "c"]), {"players": ["a", "c", "hurt"], "reserve": []})
    assert m.kind == "reserve" and m.reserve == ["hurt"]


@pytest.mark.parametrize("minutes,ok", [([0, 0, 0, 0], False), ([24, 30, 28, 26], True), ([0, 0, 22, 25], True), ([12, 10, 14, 9], False), ([30], False)])
def test_rotation_guard(minutes, ok):
    assert moves.in_rotation(minutes) is ok
