import io
import json
import os

import pytest

from sleeper_gm import api
from sleeper_gm.data import Store


def feed(monkeypatch, status='complete', stats=None):
    state = {'status': status, 'pts': 7, 'empty': False, 'calls': []}
    def scores(season, week):
        state['calls'].append(('games', season, week))
        if state['empty']:
            return []
        return [{'game_id': f'g{week}', 'date': '2025-11-01', 'start_time': str(week * 1000),
                 'status': state['status'], 'metadata': {'home_team': {'team': 'GSW'},
                 'away_team': {'team': 'LAL'}}}]
    def rest(path):
        state['calls'].append(path)
        week = int(path.split('?')[0].split('/')[-1])
        row = {'player_id': 'p', 'game_id': f'g{week}', 'date': '2025-11-01',
               'team': 'GSW', 'opponent': 'LAL', 'stats': stats if stats is not None else {'pts': state['pts'], 'sp': 1200},
               'updated_at': 50}
        return [row, {**row, 'player_id': 'TEAM_GSW'}]
    monkeypatch.setattr(api, 'scores', scores)
    monkeypatch.setattr(api, 'rest', rest)
    return state


@pytest.mark.parametrize('status,expected', [('complete', 7), ('in_progress', 99), ('pre_game', 99)])
def test_finished_week_is_immutable_but_other_weeks_refresh(tmp_path, monkeypatch, status, expected):
    state = feed(monkeypatch, status)
    store = Store(tmp_path)
    store.sync('2025', [1])
    state['pts'] = 99
    Store(tmp_path).sync('2025', [1])
    assert store.box()[0]['stats']['pts'] == expected
    assert len(store.box()) == 1
    assert store.games('2025', 1) == [{'game_id': 'g1', 'date': '2025-11-01',
        'start_ms': 1000, 'status': status, 'home': 'GSW', 'away': 'LAL', 'week': 1}]
    assert store.proj('2025', 1)[('p', 'g1')]['pts'] == expected


@pytest.mark.parametrize('stats,minutes', [({'sp': 1500}, 25), ({'min': 31, 'sp': 1500}, 31), ({}, 0)])
def test_minutes_and_raw_stats(tmp_path, monkeypatch, stats, minutes):
    feed(monkeypatch, stats=stats)
    store = Store(tmp_path)
    store.sync('2025', [1])
    assert store.box()[0]['minutes'] == minutes
    assert store.box()[0]['stats'] == stats


@pytest.mark.parametrize('cutoff,ids,count', [(1000, None, 0), (1001, None, 1), (3000, ['p'], 2), (3000, ['other'], 0), (3000, [], 0)])
def test_box_strict_cutoff_and_player_filter(tmp_path, monkeypatch, cutoff, ids, count):
    feed(monkeypatch)
    store = Store(tmp_path)
    store.sync('2025', [1, 2])
    assert len(store.box('2025', before_ms=cutoff, player_ids=ids)) == count
    assert store.box('2026') == []


def test_empty_week_stops_sync_without_caching_forever(tmp_path, monkeypatch):
    state = feed(monkeypatch)
    state['empty'] = True
    store = Store(tmp_path)
    store.sync('2025', range(1, 26))
    assert state['calls'] == [('games', '2025', 1)]
    state['empty'] = False
    store.sync('2025', [1])
    assert len(store.games('2025', 1)) == 1


def test_failed_refresh_keeps_previous_snapshot(tmp_path, monkeypatch):
    state = feed(monkeypatch, 'in_progress')
    store = Store(tmp_path)
    store.sync('2025', [1])
    state['pts'] = 99
    original = api.rest
    def fail(path):
        if '/projections/' in path:
            raise RuntimeError('interrupted')
        return original(path)
    monkeypatch.setattr(api, 'rest', fail)
    with pytest.raises(RuntimeError):
        store.sync('2025', [1])
    assert store.box()[0]['stats']['pts'] == 7


@pytest.mark.parametrize('aged', [False, True])
def test_players_cache_expires(tmp_path, monkeypatch, aged):
    monkeypatch.setattr(api, 'rest', lambda path: {'p': {'full_name': 'First'}})
    store = Store(tmp_path)
    assert store.players()['p']['full_name'] == 'First'
    if aged:
        os.utime(tmp_path / 'players.json', (0, 0))
    monkeypatch.setattr(api, 'rest', lambda path: {'p': {'full_name': 'Second'}})
    assert store.players()['p']['full_name'] == ('Second' if aged else 'First')


@pytest.mark.parametrize('abbreviation,display_name,team', [('GS', 'Golden State Warriors', 'GSW'),
    ('NO', 'New Orleans Pelicans', 'NOP'), ('SA', 'San Antonio Spurs', 'SAS'),
    ('NY', 'New York Knicks', 'NYK'), ('UTAH', 'Utah Jazz', 'UTA'),
    ('WSH', 'Washington Wizards', 'WAS'), ('PHO', 'Phoenix Suns', 'PHX'),
    (None, 'Phoenix Suns', 'PHX')])
def test_injury_matches_normalized_name_and_team(tmp_path, monkeypatch, abbreviation, display_name, team):
    players = {'p': {'first_name': 'Nicolas', 'last_name': 'Claxton', 'team': team},
               'wrong': {'full_name': 'Nicolas Claxton', 'team': 'BOS'}}
    monkeypatch.setattr(api, 'rest', lambda path: players)
    injury = {'athlete': {'displayName': 'Nicolás Claxton Jr.', 'team': {'abbreviation': abbreviation}},
              'status': 'Out', 'details': {'returnDate': '2026-11-01T00:00Z', 'type': 'Knee',
              'detail': 'Sprain', 'side': 'Left'}, 'shortComment': 'Short', 'longComment': 'Long', 'date': '2026-10-28'}
    data = {'injuries': [{'displayName': display_name, 'injuries': [injury,
        {**injury, 'athlete': {'displayName': 'Unknown Player'}}]}]}
    def urlopen(req, timeout):
        assert req.get_header('User-agent').startswith('Mozilla/')
        return io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    result = Store(tmp_path).injuries()
    assert result['p'] == {'status': 'Out', 'return_date': '2026-11-01', 'type': 'Knee',
        'detail': 'Sprain', 'side': 'Left', 'short': 'Short', 'long': 'Long', 'date': '2026-10-28'}
    assert 'wrong' not in result
    assert len(result['_unmatched']) == 1


def test_complete_scores_with_missing_boxes_do_not_freeze_bad_snapshot(tmp_path, monkeypatch):
    state = feed(monkeypatch)
    original = api.rest
    monkeypatch.setattr(api, 'rest', lambda path: [] if '/stats/' in path else original(path))
    store = Store(tmp_path)
    with pytest.raises(api.SleeperError, match='box'):
        store.sync('2025', [1])
    monkeypatch.setattr(api, 'rest', original)
    store.sync('2025', [1])
    assert store.box()[0]['stats']['pts'] == 7


def test_refresh_preserves_pregame_projection_snapshot(tmp_path, monkeypatch):
    state = feed(monkeypatch, 'pre_game')
    monkeypatch.setattr('sleeper_gm.data.time.time', lambda: .1)
    store = Store(tmp_path)
    store.sync('2025', [1])
    state['status'], state['pts'] = 'complete', 99
    monkeypatch.setattr('sleeper_gm.data.time.time', lambda: 2)
    store.sync('2025', [1])
    assert store.proj('2025', 1)[('p', 'g1')]['pts'] == 99
    earlier = [r for r in store._projection_rows() if r['fetched_ms'] == 100]
    assert len(earlier) == 1
    assert json.loads(earlier[0]['stats'])['pts'] == 7


@pytest.mark.parametrize('fetch_ms,expected', [(999, 1), (1000, 0), (1001, 0)])
def test_projection_snapshot_is_recorded_only_before_tip(tmp_path, monkeypatch, fetch_ms, expected):
    feed(monkeypatch, 'pre_game')
    monkeypatch.setattr('sleeper_gm.data.time.time', lambda: fetch_ms / 1000)
    store = Store(tmp_path)
    store.sync('2025', [1])
    snapshots = store.db.execute('SELECT * FROM projection_snapshots').fetchall()
    assert len(snapshots) == expected
    if expected:
        assert snapshots[0]['fetched_ms'] == fetch_ms
        assert snapshots[0]['player_id'] == 'p'
        assert json.loads(snapshots[0]['stats_json'])['pts'] == 7


@pytest.mark.parametrize('new_points', [7, 90])
def test_every_distinct_pre_tip_fetch_survives_refresh(tmp_path, monkeypatch, new_points):
    state = feed(monkeypatch, 'pre_game')
    store = Store(tmp_path)
    for fetched_ms, points in [(100, 7), (200, new_points), (200, new_points), (1001, 99)]:
        monkeypatch.setattr('sleeper_gm.data.time.time', lambda: fetched_ms / 1000)
        state['pts'] = points
        store.sync('2025', [1])
    rows = store.db.execute('SELECT fetched_ms,stats_json FROM projection_snapshots ORDER BY fetched_ms').fetchall()
    assert [(r['fetched_ms'], json.loads(r['stats_json'])['pts']) for r in rows] == [(100, 7), (200, new_points)]
    assert store.proj('2025', 1)[('p', 'g1')]['pts'] == 99


def test_snapshot_time_is_after_projection_response(tmp_path, monkeypatch):
    feed(monkeypatch, 'pre_game')
    state = {'ms': 999}
    original = api.rest
    def response(path):
        result = original(path)
        if '/projections/' in path:
            state['ms'] = 1001
        return result
    monkeypatch.setattr(api, 'rest', response)
    monkeypatch.setattr('sleeper_gm.data.time.time', lambda: state['ms'] / 1000)
    store = Store(tmp_path)
    store.sync('2025', [1])
    assert store.db.execute('SELECT count(*) FROM projection_snapshots').fetchone()[0] == 0


@pytest.mark.parametrize('raw,expected', [('2026-11-04T00:00Z', '2026-11-04'),
    ('2026-11-04', '2026-11-04'), (None, None), ('TBD', None),
    ('2026-02-30', None), ('2026-13-01', None)])
def test_injury_return_date_is_a_real_calendar_date(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setattr(api, 'rest', lambda path: {'p': {'full_name': 'Test Player', 'team': 'GSW'}})
    data = {'injuries': [{'abbreviation': 'GS', 'injuries': [{
        'athlete': {'displayName': 'Test Player'}, 'status': 'Out',
        'date': '2026-10-28T14:25Z', 'details': {'returnDate': raw}}]}]}
    monkeypatch.setattr('urllib.request.urlopen', lambda req, timeout: io.BytesIO(json.dumps(data).encode()))
    record = Store(tmp_path).injuries()['p']
    assert record['return_date'] == expected
    assert record['date'] == '2026-10-28T14:25Z'
