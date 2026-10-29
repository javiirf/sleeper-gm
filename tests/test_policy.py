import numpy as np
import pytest

from sleeper_gm.policy import (Decision, Draw, Team, decide, fits, lineup_today, make_plan, opponent_totals,
                       respecify_locks)

RNG = np.random.default_rng(7)


def draw(mean, sd=6.0, p=1.0, n=600):
    return Draw(p, np.clip(RNG.normal(mean, sd, n), 0, None))


def fixed(v, p=1.0):
    return Draw(p, np.full(50, float(v)))


def one_slot_team(x, future_means, final_done=False, slot="UTIL"):
    games = {"p": {d: draw(m) for d, m in enumerate(future_means)}}
    return Team([slot], ["p"], {"p": ["SF"]}, pending={0: ("p", x, "g0")}, games=games,
                final_done={"p"} if final_done else set())


def opp_fixed(total):
    """An opponent whose week is already locked at `total`."""
    return Team(["UTIL"], ["o"], {"o": ["SF"]}, locked={0: total})


def test_lock_line_matches_optimal_stopping_theory():
    # N(30,10), plays 97%, two games still to come after tonight: textbook lock line 33.4
    rng = np.random.default_rng(1)
    games = {"p": {d: Draw(0.97, rng.normal(30, 10, 200000)) for d in range(2)}}
    team = Team(["UTIL"], ["p"], {"p": ["SF"]}, games=games)
    assert make_plan(team, 2).C[0][0][0] == pytest.approx(33.4, abs=0.3)


@pytest.mark.parametrize("x,expect_lock", [(40.0, True), (12.0, False)])
def test_ev_rule_follows_the_score_against_two_games_left(x, expect_lock):
    team = one_slot_team(x, [20.0, 20.0])
    c = make_plan(team, 2).C[0][0][0]
    assert 20.0 < c < 30.0
    dec = decide(team, Team(["UTIL"], ["o"], {"o": ["SF"]}, locked={0: c}), 2, n=6000)
    assert (0 in dec.locks) == expect_lock


@pytest.mark.parametrize("bench_mean,expect_lock", [(32.0, False), (8.0, True)])
def test_final_game_kept_unless_a_bench_player_beats_it(bench_mean, expect_lock):
    team = Team(["UTIL"], ["p"], {"p": ["SF"], "q": ["PG"]}, pending={0: ("p", 18.0, "g0")},
                games={"q": {0: draw(bench_mean)}}, final_done={"p"})
    dec = decide(team, opp_fixed(0.0), 1, n=4000, flip_margin=1.0)  # EV rule only
    assert (0 in dec.locks) == expect_lock
    if not expect_lock:
        assert dec.assign_today == {0: "q"}


@pytest.mark.parametrize("x,opp_total,expect_lock", [(21.0, 20.0, True), (27.0, 30.0, False)])
def test_matchup_state_overrides_the_ev_rule(x, opp_total, expect_lock):
    # Open value ~22.4 (best of two N(20,6) games, the second forced).
    # 21 is below it (EV passes) but 21 vs a locked 20 is a sure win: lock.
    # 27 is above it (EV locks) but 27 vs 30 is a sure loss: hold out.
    team = one_slot_team(x, [20.0, 20.0])
    dec = decide(team, opp_fixed(opp_total), 2, n=8000)
    assert (dec.cont[0] > x) == (x == 21.0)
    assert (0 in dec.locks) == expect_lock


@pytest.mark.parametrize("slot,positions,ok", [
    ("PG", ["PG"], True), ("PG", ["C"], False), ("G", ["SG"], True), ("G", ["SF"], False),
    ("F", ["PF", "C"], True), ("F", ["PG"], False), ("UTIL", ["C"], True), ("UTIL", [], False)])
def test_slot_eligibility(slot, positions, ok):
    assert fits(slot, positions) is ok


def test_matching_respects_positions_and_one_slot_per_player():
    team = Team(["PG", "C"], ["0", "0"], {"guard": ["PG"], "big": ["C"], "big2": ["C"]},
                games={"guard": {0: draw(20)}, "big": {0: draw(25)}, "big2": {0: draw(30)}})
    plan = make_plan(team, 1)
    assert {s: ch[0] for s, ch in plan.choice.items()} == {0: "guard", 1: "big2"}


def test_one_player_is_never_planned_into_two_slots():
    # Review finding: P plays Sat 40 and Sun 50, Q plays Sat 30, two UTIL slots.
    # Right answer: Q locks 30 in one slot, P keeps the other open for his 50: 80.
    team = Team(["UTIL", "UTIL"], ["0", "0"], {"P": ["SF"], "Q": ["SF"]},
                games={"P": {0: fixed(40), 1: fixed(50)}, "Q": {0: fixed(30)}})
    plan = make_plan(team, 2)
    assert plan.value == pytest.approx(80.0)
    owners = [s for s, members in plan.bind.items() if "P" in members]
    assert len(owners) == 1
    dec = decide(team, opp_fixed(70.0), 2, n=2000)
    assert sorted(lineup_today(team, dec, lambda p: True)) == ["P", "Q"]
    assert dec.p_win == 1.0


def test_dnp_risk_lowers_a_draw():
    sure = draw(30.0, p=1.0)
    shaky = Draw(0.4, sure.samples)
    assert shaky.value((10.0, False)) < sure.value((10.0, False))
    assert shaky.value((0.0, True)) == pytest.approx(0.4 * sure.mean)


def test_frozen_player_is_not_reassigned_today():
    team = Team(["UTIL", "UTIL"], ["a", "0"], {"a": ["SF"], "b": ["SF"]},
                inflight={0: "a"}, frozen={"a"}, games={"a": {0: draw(20)}, "b": {0: draw(40)}})
    plan = make_plan(team, 1)
    assert plan.choice[0][0] == "a" and plan.choice[1][0] == "b"


def test_held_slot_without_a_draw_today_is_left_alone():
    team = Team(["UTIL", "UTIL"], ["a", "0"], {"a": ["SF"], "b": ["SF"]},
                inflight={0: "a"}, frozen={"a"}, games={"a": {1: draw(20)}, "b": {0: draw(30)}})
    plan = make_plan(team, 2)
    assert plan.choice[0][0] is None and plan.choice[1][0] == "b"


@pytest.mark.parametrize("moves", [True, False])
def test_lock_takes_the_most_specific_slot(moves):
    slots = ["SF", "UTIL"] if moves else ["F", "SF"]
    positions = {"p": ["SF"], "q": ["SF"] if moves else ["PF"]}
    team = Team(slots, ["q", "p"], positions)
    starters, out = respecify_locks(team, ["q", "p"], [1])
    if moves:
        assert starters == ["p", "q"] and out == [0]
    else:
        assert starters == ["q", "p"] and out == [1]


def test_lineup_benches_an_unplanned_player_with_a_game_today():
    team = Team(["UTIL", "UTIL"], ["a", "b"], {"a": ["SF"], "b": ["SF"], "c": ["SF"]},
                locked={0: 30.0}, games={"b": {0: fixed(10)}, "c": {0: fixed(35)}})
    plan = make_plan(team, 1)
    dec = Decision([], [], plan, 0, {}, {}, 0, 0)
    assert lineup_today(team, dec, lambda p: p in ("b", "c")) == ["a", "c"]


def test_equal_alternatives_get_identical_simulations():
    # Locking 20 now or taking his forced 20 later are the same week: common random
    # numbers must make them score identically, so noise can never flip the decision.
    team = Team(["UTIL", "UTIL"], ["p", "q"], {"p": ["SF"], "q": ["SF"]},
                pending={0: ("p", 20.0, "g0")}, games={"p": {0: fixed(20)}, "q": {0: draw(20, sd=8)}})
    dec = decide(team, Team(["UTIL"], ["o"], {"o": ["SF"]}, games={"o": {0: draw(40, sd=8)}}), 1, n=4000)
    assert dec.p_win_alt[0] == pytest.approx(dec.p_win, abs=1e-12)


@pytest.mark.parametrize("bench", [True, False])
def test_opponent_replaces_a_starter_who_will_not_play(bench):
    positions = {"out": ["SF"], "sub": ["SF"]} if bench else {"out": ["SF"]}
    games = {"out": {0: Draw(0.0, np.full(50, 25.0))}}
    if bench:
        games["sub"] = {0: fixed(30)}
    opp = Team(["UTIL"], ["out"], positions, games=games)
    mean = opponent_totals(opp, 1, 2000, 0).mean()
    assert mean == pytest.approx(30.0 if bench else 0.0)


@pytest.mark.parametrize("opp_total,expect", [(50.0, "gamble"), (30.0, "sure")])
def test_lineup_choice_follows_win_probability_not_points(opp_total, expect):
    # Review finding: one slot left, a sure 40 or a coin flip between 0 and 70.
    # Against a locked 50 the sure 40 loses every time; against a locked 30 it always wins.
    flip = Draw(1.0, np.array([0.0, 70.0] * 300))
    team = Team(["UTIL"], ["0"], {"sure": ["SF"], "gamble": ["SF"]},
                games={"sure": {0: fixed(40)}, "gamble": {0: flip}})
    from sleeper_gm.policy import RISK
    dec = decide(team, opp_fixed(opp_total), 1, n=4000, risk=RISK)
    assert dec.assign_today == {0: expect}
    assert dec.p_win == pytest.approx(0.5 if expect == "gamble" else 1.0, abs=0.03)


@pytest.mark.parametrize("low,high,opponent,choice,probability", [(0, 70, 50, "gamble", 0.5), (20, 50, 45, "gamble", 0.5), (0, 70, 30, "sure", 1.0), (20, 50, 30, "sure", 1.0)])
def test_lineup_variants_shift_value_for_matchup(low, high, opponent, choice, probability):
    from sleeper_gm.policy import RISK
    team = Team(["UTIL"], ["0"], {"sure": ["SF"], "gamble": ["SF"]}, games={
        "sure": {0: fixed(40)}, "gamble": {0: Draw(1.0, np.array([low, high] * 300, dtype=float))}})
    dec = decide(team, opp_fixed(opponent), 1, n=8000, risk=RISK)
    assert dec.assign_today == {0: choice}
    assert dec.p_win == pytest.approx(probability, abs=0.03)
    assert dec.plan.value == (35 if choice == "gamble" else 40)


@pytest.mark.parametrize("risk", [-0.5, 0.0, 0.75, 1.5])
def test_risk_shift_preserves_spread_and_real_thresholds(risk):
    from sleeper_gm.policy import _risk_variant, realize
    team = Team(["UTIL"], ["p"], {"p": ["SF"]}, games={"p": {0: Draw(1.0, np.array([0., 70.]))}})
    variant = _risk_variant(team, risk)
    assert variant.games["p"][0].samples.tolist() == [risk * 35, 70 + risk * 35]
    assert team.games["p"][0].samples.tolist() == [0., 70.]
    actual = realize(team, make_plan(variant, 1), 1)
    assert actual.value == 35
