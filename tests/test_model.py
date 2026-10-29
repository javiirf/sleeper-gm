from datetime import datetime, timezone
import json

import numpy as np
import pytest

from sleeper_gm.data import Store
from sleeper_gm.model import Model

DAY = 86_400_000
NOW = int(datetime.now(timezone.utc).timestamp() * 1000)
DATE = datetime.fromtimestamp((NOW + DAY) / 1000, timezone.utc).date().isoformat()


def fixture_store(tmp_path, monkeypatch, n=15, points=30, status=None, return_date=None, missed=0):
    store = Store(tmp_path)
    player = {'full_name': 'Test Player', 'team': 'GSW', 'position': 'PG', 'injury_status': status}
    monkeypatch.setattr(store, 'players', lambda **kw: {'p': player})
    injury = {'status': status, 'return_date': return_date}
    monkeypatch.setattr(store, 'injuries', lambda **kw: ({'p': injury, '_unmatched': []} if status or return_date else {'_unmatched': []}))
    for i in range(n + missed):
        tip = NOW - (n + missed - i) * DAY
        date = datetime.fromtimestamp(tip / 1000, timezone.utc).date().isoformat()
        store.db.execute('INSERT INTO games VALUES (?,?,?,?,?,?,?,?)', ('2026', str(i), date, tip, 'complete', 'GSW', 'LAL', 1))
        if i < n:
            store.db.execute('INSERT INTO boxes VALUES (?,?,?,?,?,?,?,?,?)', ('2026', 1, 'p', str(i), date, 'GSW', 'LAL', 30, json.dumps({'min': 30, 'pts': points})))
    store.db.execute('INSERT INTO games VALUES (?,?,?,?,?,?,?,?)', ('2026', 'next', DATE, NOW + DAY, 'pre_game', 'GSW', 'LAL', 2))
    store.db.commit()
    return store, player, {'game_id': 'next', 'date': DATE, 'start_ms': NOW + DAY, 'team': 'GSW', 'opp': 'LAL'}


@pytest.mark.parametrize('status,return_date,low,high', [
    ('Out', '2099-01-01', 0, .02), ('Out', '2000-01-01', .2, .7),
    ('Out', None, 0, 0), ('Doubtful', None, .03, .05),
    ('Questionable', None, .3, .7), ('GTD', None, .3, .75),
    ('Day-To-Day', None, .25, .3), ('Probable', None, .75, .866),
    ('Suspended', None, 0, .02), (None, None, .9, 1.0)])
def test_availability_priors_vary_by_status_and_return_date(tmp_path, monkeypatch, status, return_date, low, high):
    store, _, game = fixture_store(tmp_path, monkeypatch, status=status, return_date=return_date)
    model = Model(store, {'pts': 1}, NOW)
    assert low <= model.p_play('p', game) <= high


def test_espn_status_and_no_team(tmp_path, monkeypatch):
    store, player, game = fixture_store(tmp_path, monkeypatch)
    monkeypatch.setattr(store, 'injuries', lambda: {'p': {'status': 'Out', 'return_date': '2099-01-01'}})
    assert Model(store, {'pts': 1}, NOW).p_play('p', game) <= .02
    player['team'] = None
    assert Model(store, {'pts': 1}, NOW).p_play('p', game) == 0


def test_past_prediction_does_not_read_live_injuries(tmp_path, monkeypatch):
    store, _, game = fixture_store(tmp_path, monkeypatch)
    def forbidden():
        raise AssertionError('historical injury lookup')
    monkeypatch.setattr(store, 'injuries', forbidden)
    dist = Model(store, {'pts': 1}, NOW - 30 * DAY).dist('p', game)
    assert np.isfinite(dist.samples).all()


@pytest.mark.parametrize('n', [3, 40])
def test_thin_history_shrinks_more(tmp_path, monkeypatch, n):
    store, _, game = fixture_store(tmp_path, monkeypatch, n=n, points=60)
    mean = Model(store, {'pts': 1}, NOW).dist('p', game).mean
    if n == 3:
        assert 15 < mean < 45
    else:
        assert 45 < mean < 61


@pytest.mark.parametrize('injected_tip', [NOW, NOW + DAY, NOW + 2 * DAY])
def test_own_and_future_box_never_influence_prediction(tmp_path, monkeypatch, injected_tip):
    store, _, game = fixture_store(tmp_path, monkeypatch)
    store.db.execute('INSERT INTO games VALUES (?,?,?,?,?,?,?,?)', ('2026', 'future', DATE, injected_tip, 'complete', 'GSW', 'LAL', 2))
    # Hold the schedule fixed: a new back-to-back changes availability legitimately.
    before = Model(store, {'pts': 1}, NOW, seed=12).dist('p', game)
    store.db.execute('INSERT INTO boxes VALUES (?,?,?,?,?,?,?,?,?)', ('2026', 2, 'p', 'future', DATE, 'GSW', 'LAL', 40, '{"pts":9999,"min":40}'))
    store.db.commit()
    after = Model(store, {'pts': 1}, NOW, seed=12).dist('p', game)
    assert before.mean == after.mean
    assert before.p_play == after.p_play
    np.testing.assert_array_equal(before.samples, after.samples)


def test_game_tip_caps_later_model_asof(tmp_path, monkeypatch):
    store, _, game = fixture_store(tmp_path, monkeypatch)
    historical = {**game, 'game_id': '10', 'start_ms': NOW - 5 * DAY}
    early = Model(store, {'pts': 1}, historical['start_ms']).dist('p', historical)
    late = Model(store, {'pts': 1}, NOW).dist('p', historical)
    assert early.mean == late.mean
    np.testing.assert_array_equal(early.samples, late.samples)


@pytest.mark.parametrize('fetched_ms,known_ms,expected_blend', [
    (NOW - 1, None, True), (NOW, None, True), (NOW + 10, NOW - 1, False),
    (NOW + DAY, NOW - 1, False), (NOW + 10, None, False)])
def test_projection_must_have_been_known_at_prediction_time(tmp_path, monkeypatch, fetched_ms, known_ms, expected_blend):
    store, _, game = fixture_store(tmp_path, monkeypatch)
    import sleeper_gm.model as module
    monkeypatch.setattr(module, 'DEFAULT_PARAMS', {**module.DEFAULT_PARAMS, 'blend_weight': .8})
    monkeypatch.setattr(module, 'PARAMS_PATH', tmp_path / 'missing.json')
    unblended = Model(store, {'pts': 1}, NOW).dist('p', game).mean
    store.db.execute('INSERT INTO projections VALUES (?,?,?,?,?,?,?)', ('2026', 2, 'p', 'next', '{"pts":80}', known_ms, fetched_ms))
    store.db.commit()
    blended = Model(store, {'pts': 1}, NOW).dist('p', game).mean
    assert (blended > unblended + 20) if expected_blend else (blended == unblended)


def test_return_minutes_haircut_depends_on_missed_team_games(tmp_path, monkeypatch):
    regular, _, game = fixture_store(tmp_path / 'regular', monkeypatch, missed=0)
    absent, _, other = fixture_store(tmp_path / 'absent', monkeypatch, missed=6)
    a = Model(regular, {'pts': 1}, NOW).dist('p', game).mean
    b = Model(absent, {'pts': 1}, NOW).dist('p', other).mean
    assert b < a * .9


def test_samples_are_conditional_reproducible_and_ros_ignores_injury(tmp_path, monkeypatch):
    store, player, game = fixture_store(tmp_path, monkeypatch, status='Out', return_date='2099-01-01')
    model = Model(store, {'pts': 1}, NOW, n_samples=600, seed=7)
    a = model.dist('p', game)
    assert a.p_play <= .02
    assert a.samples.shape == (600,) and np.issubdtype(a.samples.dtype, np.floating)
    assert a.samples.mean() > 15 and np.std(a.samples) > 0
    np.testing.assert_array_equal(a.samples, model.dist('p', game).samples)
    value = model.ros_value('p', NOW)
    player['injury_status'] = None
    assert model.ros_value('p', NOW) == value


def test_crps_and_quantile_metrics_have_known_values():
    from sleeper_gm.backtest import metrics
    result = metrics(np.array([1., 3.]), np.array([1., 1.]), np.array([[0., 2.], [0., 2.]]))
    assert result['mae'] == 1
    assert result['crps'] == 1
    assert result['q90'] == .5


def test_empirical_residual_tail_responds_to_player_volatility(tmp_path, monkeypatch):
    steady, _, game = fixture_store(tmp_path / 'steady', monkeypatch, n=40, points=30)
    volatile, _, other = fixture_store(tmp_path / 'volatile', monkeypatch, n=40, points=30)
    for i in range(40):
        volatile.db.execute('UPDATE boxes SET stats=? WHERE game_id=?',
                            (json.dumps({'pts': 10 if i % 2 else 50, 'min': 30}), str(i)))
    volatile.db.commit()
    stable = Model(steady, {'pts': 1}, NOW, n_samples=4000).dist('p', game)
    varied = Model(volatile, {'pts': 1}, NOW, n_samples=4000).dist('p', other)
    assert np.quantile(varied.samples, .9) > np.quantile(stable.samples, .9) + 10
    assert np.std(varied.samples) > np.std(stable.samples) * 2


def test_last_season_rate_shrinks_sparse_current_season(tmp_path, monkeypatch):
    low, _, game = fixture_store(tmp_path / 'low', monkeypatch, n=3, points=30)
    high, _, other = fixture_store(tmp_path / 'high', monkeypatch, n=3, points=30)
    for store, points in [(low, 15), (high, 60)]:
        for i in range(40):
            tip = NOW - (200 + i) * DAY
            gid = f'prior{i}'
            store.db.execute('INSERT INTO games VALUES (?,?,?,?,?,?,?,?)',
                             ('2025', gid, '2026-01-01', tip, 'complete', 'GSW', 'LAL', 10))
            store.db.execute('INSERT INTO boxes VALUES (?,?,?,?,?,?,?,?,?)',
                             ('2025', 10, 'p', gid, '2026-01-01', 'GSW', 'LAL', 30, json.dumps({'pts': points, 'min': 30})))
        store.db.commit()
    assert Model(high, {'pts': 1}, NOW).dist('p', other).mean > Model(low, {'pts': 1}, NOW).dist('p', game).mean + 15


def test_backtest_and_public_model_produce_identical_prediction(tmp_path, monkeypatch):
    from sleeper_gm.backtest import _run
    from sleeper_gm.model import _projection_index, _params
    store, _, game = fixture_store(tmp_path, monkeypatch, n=15)
    target = {'game_id': '14', 'date': DATE, 'start_ms': NOW - DAY, 'team': 'GSW', 'opp': 'LAL'}
    records = _run(store.box(), {'pts': 1}, _params(target['start_ms']), store._schedule(),
                   store.players(), _projection_index(store, {'pts': 1}), {('p', '14')}, samples=600)
    actual = Model(store, {'pts': 1}, target['start_ms']).dist('p', target)
    assert records[0]['mean'] == actual.mean
    np.testing.assert_array_equal(records[0]['samples'], actual.samples)


def test_same_evening_unfinished_results_do_not_influence_distribution(tmp_path, monkeypatch):
    store, _, game = fixture_store(tmp_path, monkeypatch)
    before = Model(store, {'pts': 1}, NOW).dist('p', game)
    store.db.execute('INSERT INTO games VALUES (?,?,?,?,?,?,?,?)',
                     ('2026', 'overlap', DATE, NOW - 60 * 60 * 1000, 'complete', 'BOS', 'MIA', 2))
    store.db.execute('INSERT INTO boxes VALUES (?,?,?,?,?,?,?,?,?)',
                     ('2026', 2, 'peer', 'overlap', DATE, 'BOS', 'MIA', 40, '{"pts":9999,"min":40}'))
    store.db.commit()
    after = Model(store, {'pts': 1}, NOW).dist('p', game)
    assert before.mean == after.mean
    np.testing.assert_array_equal(before.samples, after.samples)


@pytest.mark.parametrize('status,low,high', [('DTD', .25, .3), ('IR', 0, 0)])
def test_sleeper_abbreviated_injury_status_without_espn_match(tmp_path, monkeypatch, status, low, high):
    store, _, game = fixture_store(tmp_path, monkeypatch, status=status)
    monkeypatch.setattr(store, 'injuries', lambda: {'_unmatched': []})
    assert low <= Model(store, {'pts': 1}, NOW).p_play('p', game) <= high


def test_intraday_historical_availability_never_reads_later_injury(tmp_path, monkeypatch):
    store, _, game = fixture_store(tmp_path, monkeypatch)
    def forbidden():
        raise AssertionError('later intraday injury lookup')
    monkeypatch.setattr(store, 'injuries', forbidden)
    model = Model(store, {'pts': 1}, NOW - 60 * 60 * 1000)
    assert model.p_play('p', game) > .9


@pytest.mark.parametrize('missed', [0, 6])
def test_season_debut_haircut_counts_opening_absence(tmp_path, monkeypatch, missed):
    from sleeper_gm.model import _Engine, DEFAULT_PARAMS
    store, _, game = fixture_store(tmp_path, monkeypatch, n=0, missed=missed)
    engine = _Engine({'pts': 1}, DEFAULT_PARAMS, store._schedule(), store.players(), {})
    history = [{'season': '2025', 'team': 'GSW', 'start_ms': NOW - 200 * DAY}]
    assert engine._haircut(history, {**game, 'season': '2026'}) == (.8 if missed else 1.)


def test_projection_version_selection_uses_snapshot_available_at_asof(tmp_path, monkeypatch):
    import sleeper_gm.model as module
    store, _, game = fixture_store(tmp_path, monkeypatch)
    monkeypatch.setattr(module, 'DEFAULT_PARAMS', {**module.DEFAULT_PARAMS, 'blend_weight': 1.})
    monkeypatch.setattr(module, 'PARAMS_PATH', tmp_path / 'missing.json')
    store.db.execute('INSERT INTO projections VALUES (?,?,?,?,?,?,?)',
                     ('2026', 2, 'p', 'next', '{"pts":90}', NOW + 10, NOW + 10))
    store.db.execute('INSERT INTO projection_history VALUES (?,?,?,?,?,?,?)',
                     ('2026', 2, 'p', 'next', '{"pts":35}', NOW - 10, NOW - 10))
    store.db.commit()
    assert Model(store, {'pts': 1}, NOW).dist('p', game).mean == 35


@pytest.mark.parametrize('second_gap,expected', [(0, .84), (6, .8)])
def test_new_long_absence_takes_precedence_over_second_return_game(second_gap, expected):
    from sleeper_gm.model import _Engine, DEFAULT_PARAMS
    schedule = [{'season': '2026', 'game_id': str(i), 'start_ms': i * DAY,
                 'status': 'complete', 'home': 'GSW', 'away': 'LAL'} for i in range(7 + second_gap)]
    engine = _Engine({'pts': 1}, DEFAULT_PARAMS, schedule, {}, {})
    history = [{'season': '2026', 'team': 'GSW', 'start_ms': 6 * DAY}]
    game = {'season': '2026', 'team': 'GSW', 'start_ms': (7 + second_gap) * DAY}
    assert engine._haircut(history, game) == pytest.approx(expected)


def participation_store(tmp_path, monkeypatch, played, changed, missing=False, prior_played=8):
    store, player, game = fixture_store(tmp_path, monkeypatch, n=4)
    for i in range(4):
        if i >= played:
            if missing:
                store.db.execute('DELETE FROM boxes WHERE game_id=?', (str(i),))
            else:
                store.db.execute('UPDATE boxes SET minutes=0,stats=? WHERE game_id=?',
                                 ('{"min":0,"pts":0}', str(i)))
    old_team = 'WAS' if changed else 'GSW'
    for i in range(20):
        tip = NOW - (220 - i) * DAY
        gid = f'prior{i}'
        minutes = 32 if i < prior_played else 0
        store.db.execute('INSERT INTO games VALUES (?,?,?,?,?,?,?,?)',
                         ('2025', gid, '2026-03-01', tip, 'complete', old_team, 'LAL', 20))
        store.db.execute('INSERT INTO boxes VALUES (?,?,?,?,?,?,?,?,?)',
                         ('2025', 20, 'p', gid, '2026-03-01', old_team, 'LAL', minutes,
                          json.dumps({'min': minutes, 'pts': minutes})))
    store.db.commit()
    return store, player, game


@pytest.mark.parametrize('changed', [False, True])
@pytest.mark.parametrize('missing', [False, True])
@pytest.mark.parametrize('played', [0, 2, 4])
def test_participation_and_ros_count_every_current_team_game(tmp_path, monkeypatch, changed, missing, played):
    store, _, game = participation_store(tmp_path, monkeypatch, played, changed, missing)
    model = Model(store, {'pts': 1}, NOW)
    dist = model.dist('p', game)
    low, high = {0: (0, .15), 2: (.05, .25), 4: (.91, .93)}[played]
    assert low <= dist.p_play <= high
    engine = model._engine(NOW)
    ros_game = {**game, 'game_id': '__ros__', 'start_ms': NOW, 'season': '2026'}
    raw = engine.predict('p', ros_game, NOW, haircut=False)[0]
    assert model.ros_value('p', NOW) == pytest.approx(raw * dist.p_play)
    if played == 0:
        assert dist.mean > 10  # Conditional scoring stays separate from DNP risk.
        assert model.ros_value('p', NOW) < 5


@pytest.mark.parametrize('changed', [False, True])
def test_participation_ordering_and_last_team_prior(tmp_path, monkeypatch, changed):
    probabilities = []
    for played in (0, 2, 4):
        store, _, game = participation_store(tmp_path / str(played), monkeypatch, played, changed)
        probabilities.append(Model(store, {'pts': 1}, NOW).p_play('p', game))
    assert probabilities[0] < probabilities[1] < probabilities[2]
    low, _, game = participation_store(tmp_path / 'low', monkeypatch, 0, changed, prior_played=2)
    high, _, other = participation_store(tmp_path / 'high', monkeypatch, 0, changed, prior_played=18)
    assert Model(low, {'pts': 1}, NOW).p_play('p', game) < Model(high, {'pts': 1}, NOW).p_play('p', other)


def test_rookie_scratches_use_league_prior(tmp_path, monkeypatch):
    store, _, game = fixture_store(tmp_path, monkeypatch, n=0, missed=4)
    model = Model(store, {'pts': 1}, NOW)
    assert model.p_play('p', game) < .2
    assert model.ros_value('p', NOW) < 5


@pytest.mark.parametrize('joined', [False, True])
def test_participation_excludes_games_before_known_join(tmp_path, monkeypatch, joined):
    store, player, game = fixture_store(tmp_path, monkeypatch, n=0, missed=6)
    if joined:
        player['team_changed_at'] = NOW - DAY // 2
    probability = Model(store, {'pts': 1}, NOW).p_play('p', game)
    assert probability == pytest.approx(.6) if joined else probability < .15


@pytest.mark.parametrize('changed', [False, True])
@pytest.mark.parametrize('current_minutes', [12, 36])
@pytest.mark.parametrize('appearances', [2, 4])
def test_current_role_minutes_outweigh_last_season(tmp_path, monkeypatch, changed, current_minutes, appearances):
    import sleeper_gm.model as module
    # Hold the rate at the observed 1 FP/minute to isolate the minutes behavior.
    monkeypatch.setattr(module, 'DEFAULT_PARAMS', {**module.DEFAULT_PARAMS, 'rate_prior_games': 0.})
    monkeypatch.setattr(module, 'PARAMS_PATH', tmp_path / 'missing.json')
    means = []
    for prior_minutes in (8, 40):
        store, _, game = participation_store(tmp_path / str(prior_minutes), monkeypatch, appearances, changed, prior_played=20)
        store.db.execute('UPDATE boxes SET minutes=?,stats=? WHERE season=?',
                         (prior_minutes, json.dumps({'pts': prior_minutes, 'min': prior_minutes}), '2025'))
        store.db.execute('UPDATE boxes SET minutes=?,stats=? WHERE season=? AND minutes>0',
                         (current_minutes, json.dumps({'pts': current_minutes, 'min': current_minutes}), '2026'))
        store.db.commit()
        mean = Model(store, {'pts': 1}, NOW).dist('p', game).mean
        assert abs(mean - current_minutes) < 6
        means.append(mean)
    # A 32-minute old-role difference contributes less than 20% of itself.
    assert abs(means[0] - means[1]) < 32 * .2


@pytest.mark.parametrize('stats,blend,evidence', [
    ({'pts': 80, 'min': 30}, True, False), ({'pts': 80}, True, False),
    ({'pts': 0, 'min': 0}, False, True), ({'pts': 0, 'sp': 0}, False, True),
    ({'pts': 10, 'min': 0}, False, False), ({'min': 0}, False, False),
    ({}, False, False), (None, False, False)])
def test_live_projection_blend_and_zero_minute_evidence(tmp_path, monkeypatch, stats, blend, evidence):
    import sleeper_gm.model as module
    store, _, game = fixture_store(tmp_path, monkeypatch)
    monkeypatch.setattr(module, 'PARAMS_PATH', tmp_path / 'missing.json')
    before = Model(store, {'pts': 1}, NOW).dist('p', game)
    if stats is not None:
        store.db.execute('INSERT INTO projection_snapshots VALUES (?,?,?,?,?,?)',
                         ('2026', 2, 'p', 'next', NOW - 1, json.dumps(stats)))
        store.db.commit()
    after = Model(store, {'pts': 1}, NOW).dist('p', game)
    expected = .5 * before.mean + .5 * 80 if blend else before.mean
    assert after.mean == pytest.approx(expected)
    np.testing.assert_allclose(after.samples - after.mean, before.samples - before.mean, atol=1e-12)
    if evidence:
        assert 0 < after.p_play < before.p_play
    else:
        assert after.p_play == before.p_play


@pytest.mark.parametrize('fetch_offset', [-1, 0, 1])
def test_projection_fetch_must_be_strictly_before_tip(tmp_path, monkeypatch, fetch_offset):
    import sleeper_gm.model as module
    store, _, game = fixture_store(tmp_path, monkeypatch)
    monkeypatch.setattr(module, 'PARAMS_PATH', tmp_path / 'missing.json')
    unblended = Model(store, {'pts': 1}, game['start_ms']).dist('p', game).mean
    store.db.execute('INSERT INTO projection_snapshots VALUES (?,?,?,?,?,?)',
                     ('2026', 2, 'p', 'next', game['start_ms'] + fetch_offset, '{"min":30,"pts":80}'))
    store.db.commit()
    result = Model(store, {'pts': 1}, game['start_ms'] + 10).dist('p', game).mean
    assert result == pytest.approx(.5 * unblended + 40 if fetch_offset < 0 else unblended)


@pytest.mark.parametrize('actual,expected', [(10., 0.), (30., 1.)])
def test_live_blend_fit_uses_pre_tip_data_and_reports_n(actual, expected):
    from sleeper_gm.backtest import _fit_live_blend
    records = [{'raw': 10., 'projection': 30., 'actual': actual}] * 60
    records += [{'raw': 10., 'projection': None, 'actual': 1000.}] * 100
    weight, fit = _fit_live_blend(records)
    assert fit['n'] == 60 and fit['blend_identifiable']
    assert fit['unshrunk_weight'] == expected
    assert weight == pytest.approx((60 * expected + 30 * .5) / 90)
    assert _fit_live_blend(records[60:])[0] == .5
    assert _fit_live_blend(records[60:])[1]['n'] == 0


def test_no_team_has_no_ros_value(tmp_path, monkeypatch):
    store, player, _ = fixture_store(tmp_path, monkeypatch)
    player['team'] = None
    assert Model(store, {'pts': 1}, NOW).ros_value('p', NOW) == 0


@pytest.mark.parametrize('old_team_games', [0, 8])
def test_midseason_trade_excludes_new_team_games_before_arrival(tmp_path, monkeypatch, old_team_games):
    store, _, game = fixture_store(tmp_path, monkeypatch, n=2)
    for i in range(old_team_games):
        tip = NOW - (10 - i) * DAY
        for team in ('GSW', 'WAS'):
            gid = f'{team}{i}'
            store.db.execute('INSERT INTO games VALUES (?,?,?,?,?,?,?,?)',
                             ('2026', gid, DATE, tip, 'complete', team, 'LAL', 1))
            if team == 'WAS':
                store.db.execute('INSERT INTO boxes VALUES (?,?,?,?,?,?,?,?,?)',
                                 ('2026', 1, 'p', gid, DATE, team, 'LAL', 40, '{"pts":40,"min":40}'))
    store.db.commit()
    # Pre-trade GSW games are not absences; both fixtures establish a role.
    assert .89 < Model(store, {'pts': 1}, NOW).p_play('p', game) < .92


@pytest.mark.parametrize('all_star', [False, True])
def test_all_star_appearance_does_not_change_participation_team(tmp_path, monkeypatch, all_star):
    store, _, game = participation_store(tmp_path, monkeypatch, 0, False)
    before = Model(store, {'pts': 1}, NOW).p_play('p', game)
    if all_star:
        tip = NOW - 210 * DAY
        store.db.execute('INSERT INTO games VALUES (?,?,?,?,?,?,?,?)',
                         ('2025', 'exhibition', '2026-02-15', tip, 'complete', 'STP', 'STR', 17))
        store.db.execute('INSERT INTO boxes VALUES (?,?,?,?,?,?,?,?,?)',
                         ('2025', 17, 'p', 'exhibition', '2026-02-15', 'STP', 'STR', 8, '{"min":8,"pts":4}'))
        store.db.commit()
    assert Model(store, {'pts': 1}, NOW).p_play('p', game) == before


@pytest.mark.parametrize('pre_tip', [False, True])
def test_backtest_blend_eligibility_and_public_model_match(tmp_path, monkeypatch, pre_tip):
    from sleeper_gm.backtest import _run
    from sleeper_gm.model import _projection_index, DEFAULT_PARAMS
    import sleeper_gm.model as module
    store, _, _ = fixture_store(tmp_path, monkeypatch, n=15)
    monkeypatch.setattr(module, 'PARAMS_PATH', tmp_path / 'missing.json')
    target = {'game_id': '14', 'date': DATE, 'start_ms': NOW - DAY, 'team': 'GSW', 'opp': 'LAL'}
    store.db.execute('INSERT INTO projection_snapshots VALUES (?,?,?,?,?,?)',
                     ('2026', 1, 'p', '14', target['start_ms'] + (-1 if pre_tip else 1), '{"pts":80,"min":30}'))
    store.db.commit()
    records = _run(store.box(), {'pts': 1}, DEFAULT_PARAMS, store._schedule(), store.players(),
                   _projection_index(store, {'pts': 1}), {('p', '14')}, samples=600)
    actual = Model(store, {'pts': 1}, target['start_ms']).dist('p', target)
    assert records[0]['projection'] == (80 if pre_tip else None)
    assert records[0]['mean'] == actual.mean
    np.testing.assert_array_equal(records[0]['samples'], actual.samples)


@pytest.mark.parametrize('pattern,low,high', [
    ([1, 0, 1, 1], .88, 1.), ([1, 1, 1, 0], .2, .45),
    ([1, 1, 0, 0], .05, .25), ([0, 0, 0, 0], 0., .15)])
@pytest.mark.parametrize('missing', [False, True])
def test_last_team_game_controls_healthy_participation(tmp_path, monkeypatch, pattern, low, high, missing):
    store, _, game = fixture_store(tmp_path, monkeypatch, n=len(pattern))
    for i, played in enumerate(pattern):
        if not played:
            if missing:
                store.db.execute('DELETE FROM boxes WHERE game_id=?', (str(i),))
            else:
                store.db.execute('UPDATE boxes SET minutes=0,stats=? WHERE game_id=?', ('{}', str(i)))
    store.db.commit()
    assert low <= Model(store, {'pts': 1}, NOW).p_play('p', game) <= high


@pytest.mark.parametrize('status', ['Out', 'IR', 'DTD', 'D2D', 'GTD', 'Questionable', 'Doubtful', 'Sus', None])
@pytest.mark.parametrize('report_age', [1, 5, None])
def test_current_injury_absences_do_not_destroy_ros(tmp_path, monkeypatch, status, report_age):
    store, _, _ = fixture_store(tmp_path, monkeypatch, n=15, missed=5, status=status)
    injury = {'date': datetime.fromtimestamp((NOW - report_age * DAY) / 1000, timezone.utc).isoformat(),
              'return_date': DATE, 'status': 'Out'} if report_age is not None else {}
    monkeypatch.setattr(store, 'injuries', lambda: {'p': injury} if injury else {})
    value = Model(store, {'pts': 1}, NOW).ros_value('p', NOW)
    assert value > 20 if status or injury else value < 5


@pytest.mark.parametrize('misses,returned,expected_high', [(2, True, False), (3, True, True),
                                                         (6, True, True), (3, False, False), (6, False, False)])
def test_only_resolved_long_dnp_streaks_are_excluded(tmp_path, monkeypatch, misses, returned, expected_high):
    store, _, game = fixture_store(tmp_path, monkeypatch, n=10 + misses + int(returned))
    for i in range(10, 10 + misses):
        store.db.execute('UPDATE boxes SET minutes=0,stats=? WHERE game_id=?', ('{}', str(i)))
    store.db.commit()
    model = Model(store, {'pts': 1}, NOW)
    probability = model.p_play('p', game)
    if expected_high:
        assert probability > .91
    elif returned:
        assert .88 <= probability < .91
    else:
        assert probability < .25


@pytest.mark.parametrize('source', ['sleeper', 'espn'])
@pytest.mark.parametrize('status,return_date,rate', [
    ('Probable', None, .866), ('Questionable', None, .483), ('Doubtful', None, .042),
    ('GTD', None, .5), ('D2D', None, .3), ('DTD', None, .3), ('Day-To-Day', None, .3),
    ('Out', None, 0.), ('Out', '2099-01-01', 0.), ('Out', DATE, .45),
    ('IR', DATE, 0.), ('Sus', None, 0.), ('Suspended', None, 0.), (None, None, 1.)])
def test_status_rates_have_one_consistent_mapping(tmp_path, monkeypatch, source, status, return_date, rate):
    store, player, game = fixture_store(tmp_path, monkeypatch)
    healthy = Model(store, {'pts': 1}, NOW).p_play('p', game)
    if source == 'sleeper':
        player['injury_status'] = status
    monkeypatch.setattr(store, 'injuries', lambda: {'p': {
        'status': status if source == 'espn' else None, 'return_date': return_date}})
    assert Model(store, {'pts': 1}, NOW).p_play('p', game) == pytest.approx(healthy * rate)


@pytest.mark.parametrize('status', ['DTD', 'D2D', 'Day-To-Day'])
@pytest.mark.parametrize('later', [False, True])
def test_day_to_day_relaxes_after_next_scheduled_game(tmp_path, monkeypatch, status, later):
    store, player, game = fixture_store(tmp_path, monkeypatch)
    if later:
        tip = game['start_ms'] + DAY
        store.db.execute('INSERT INTO games VALUES (?,?,?,?,?,?,?,?)',
                         ('2026', 'later', datetime.fromtimestamp(tip / 1000, timezone.utc).date().isoformat(),
                          tip, 'pre_game', 'GSW', 'LAL', 2))
        store.db.commit()
        game = {**game, 'game_id': 'later', 'start_ms': tip}
    healthy = Model(store, {'pts': 1}, NOW).p_play('p', game)
    player['injury_status'] = status
    assert Model(store, {'pts': 1}, NOW).p_play('p', game) == pytest.approx(healthy * (.75 if later else .3))


@pytest.mark.parametrize('minutes', [29.9, 30., 36.])
@pytest.mark.parametrize('gap', [1, 2])
def test_heavy_minutes_back_to_back_second_night(tmp_path, monkeypatch, minutes, gap):
    store, _, game = fixture_store(tmp_path, monkeypatch)
    store.db.execute('UPDATE boxes SET minutes=?,stats=?', (minutes, json.dumps({'min': minutes, 'pts': 30})))
    store.db.commit()
    ordinary = Model(store, {'pts': 1}, NOW).p_play('p', game)
    prior_tip = game['start_ms'] - gap * DAY
    store.db.execute('INSERT INTO games VALUES (?,?,?,?,?,?,?,?)',
                     ('2026', 'b2b', datetime.fromtimestamp(prior_tip / 1000, timezone.utc).date().isoformat(),
                      prior_tip, 'pre_game', 'GSW', 'LAL', 2))
    store.db.commit()
    actual = Model(store, {'pts': 1}, NOW).p_play('p', game)
    assert actual == pytest.approx(ordinary * (.95 if gap == 1 and minutes >= 30 else 1.))


@pytest.mark.parametrize('offset,expected', [(0, .8), (1, .84), (2, .88), (3, .92), (4, .96), (5, 1.)])
@pytest.mark.parametrize('missed', [2, 3, 6])
def test_return_haircut_fades_over_five_appearances(offset, expected, missed):
    from sleeper_gm.model import _Engine, DEFAULT_PARAMS
    schedule = [{'season': '2026', 'game_id': str(i), 'start_ms': i * DAY,
                 'status': 'complete', 'home': 'GSW', 'away': 'LAL'} for i in range(missed + offset + 2)]
    engine = _Engine({'pts': 1}, DEFAULT_PARAMS, schedule, {}, {})
    history = [{'season': '2026', 'team': 'GSW', 'start_ms': i * DAY}
               for i in [0] + list(range(missed + 1, missed + 1 + offset))]
    game = {'season': '2026', 'team': 'GSW', 'start_ms': (missed + 1 + offset) * DAY}
    assert engine._haircut(history, game) == pytest.approx(expected if missed >= 3 else 1.)


@pytest.mark.parametrize('return_date,expected', [(None, .5), ('2026-10-25', 1.),
    ('2026-11-01', 1.), ('2026-11-02', .75), ('2026-11-04', .75), ('2026-11-22', .25), ('2026-11-23', 0.)])
def test_availability_counts_monday_sunday_weeks(tmp_path, monkeypatch, return_date, expected):
    import sleeper_gm.model as module
    fixed = datetime(2026, 10, 28, 16, tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    monkeypatch.setattr(module, 'datetime', Clock)
    store, _, _ = fixture_store(tmp_path, monkeypatch, status='Out', return_date=return_date)
    cutoff = int(fixed.timestamp() * 1000)
    assert Model(store, {'pts': 1}, cutoff).availability_weeks('p', cutoff) == expected


@pytest.mark.parametrize('status,expected', [(None, 1.), ('Probable', 1.), ('DTD', 1.),
                                          ('Out', .5), ('IR', .5), ('Sus', .5)])
def test_availability_unknown_return_date_uses_labelled_prior(tmp_path, monkeypatch, status, expected):
    store, _, _ = fixture_store(tmp_path, monkeypatch, status=status)
    assert Model(store, {'pts': 1}, NOW).availability_weeks('p', NOW) == expected


@pytest.mark.parametrize('horizon', [0, -1, 1.5])
def test_availability_rejects_invalid_horizon(tmp_path, monkeypatch, horizon):
    store, _, _ = fixture_store(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        Model(store, {'pts': 1}, NOW).availability_weeks('p', NOW, horizon)


@pytest.mark.parametrize('missing_index', [0, 1, 2])
@pytest.mark.parametrize('prior_played', [0, 2, 18])
def test_last_appearance_overrides_old_low_participation(tmp_path, monkeypatch, missing_index, prior_played):
    store, _, game = participation_store(tmp_path, monkeypatch, 4, False, prior_played=prior_played)
    store.db.execute('UPDATE boxes SET minutes=0,stats=? WHERE season=? AND game_id=?',
                     ('{}', '2026', str(missing_index)))
    store.db.commit()
    assert Model(store, {'pts': 1}, NOW).p_play('p', game) >= .88


@pytest.mark.parametrize('current_team', ['GSW', None])
def test_current_free_agent_metadata_does_not_zero_historical_game(tmp_path, monkeypatch, current_team):
    store, player, _ = fixture_store(tmp_path, monkeypatch)
    player['team'] = current_team
    game = {'game_id': '14', 'start_ms': NOW - DAY, 'team': 'GSW'}
    assert Model(store, {'pts': 1}, NOW).p_play('p', game) > .85


@pytest.mark.parametrize('method', ['ros_value', 'availability_weeks'])
def test_historical_values_never_fetch_current_injury(tmp_path, monkeypatch, method):
    store, _, _ = fixture_store(tmp_path, monkeypatch, status='Out')
    def forbidden():
        raise AssertionError('current injury used for historical value')
    monkeypatch.setattr(store, 'injuries', forbidden)
    model = Model(store, {'pts': 1}, NOW)
    assert getattr(model, method)('p', NOW - DAY) > 0


@pytest.mark.parametrize('future_return', [False, True])
def test_return_after_cutoff_does_not_erase_historical_scratches(tmp_path, monkeypatch, future_return):
    store, _, game = fixture_store(tmp_path, monkeypatch, n=12)
    for i in [8, 9, 10]:
        store.db.execute('UPDATE boxes SET minutes=0,stats=? WHERE game_id=?', ('{}', str(i)))
    if not future_return:
        store.db.execute('DELETE FROM boxes WHERE game_id=?', ('11',))
    store.db.commit()
    # Game 11 has not tipped, so both prefixes end with three unexplained misses.
    cutoff = NOW - DAY - 1
    assert Model(store, {'pts': 1}, cutoff).p_play('p', game) < .25


@pytest.mark.parametrize('missed', [13, 14, 20])
def test_long_absence_caps_first_return_minutes(tmp_path, monkeypatch, missed):
    import sleeper_gm.model as module
    monkeypatch.setattr(module, 'PARAMS_PATH', tmp_path / 'missing.json')
    monkeypatch.setattr(module, 'DEFAULT_PARAMS', {**module.DEFAULT_PARAMS, 'rate_prior_games': 0.,
                                                'minutes_prior_games': 0.})
    store, _, game = fixture_store(tmp_path, monkeypatch, n=15, points=40, missed=missed)
    store.db.execute('UPDATE boxes SET minutes=40,stats=?', ('{"pts":40,"min":40}',))
    store.db.commit()
    model = Model(store, {'pts': 1}, NOW)
    assert model.dist('p', game).mean == pytest.approx(25 if missed >= 14 else 32)


@pytest.mark.parametrize('future_games', [1, 4, 6])
@pytest.mark.parametrize('observed_misses', [0, 3])
def test_future_schedule_does_not_invent_missed_games(tmp_path, monkeypatch, future_games, observed_misses):
    store, _, game = fixture_store(tmp_path, monkeypatch, missed=observed_misses)
    baseline = Model(store, {'pts': 1}, NOW).dist('p', game).mean
    for i in range(2, future_games + 1):
        tip = NOW + i * DAY
        gid = f'future{i}'
        store.db.execute('INSERT INTO games VALUES (?,?,?,?,?,?,?,?)',
                         ('2026', gid, DATE, tip, 'pre_game', 'GSW', 'LAL', 2))
        game = {**game, 'game_id': gid, 'start_ms': tip}
    store.db.commit()
    assert Model(store, {'pts': 1}, NOW).dist('p', game).mean == pytest.approx(baseline)


@pytest.mark.parametrize('historical', [False, True])
def test_availability_team_membership_respects_cutoff(tmp_path, monkeypatch, historical):
    store, player, _ = fixture_store(tmp_path, monkeypatch)
    player['team'] = None
    cutoff = NOW - DAY if historical else NOW
    assert Model(store, {'pts': 1}, NOW).availability_weeks('p', cutoff) == (1. if historical else 0.)
