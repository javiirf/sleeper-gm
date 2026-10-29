"""Public Sleeper snapshots and conservative ESPN name/team matching."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
import urllib.request

from sleeper_gm import api

ESPN_INJURIES = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries'
TEAM_ALIASES = {'GS': 'GSW', 'NO': 'NOP', 'SA': 'SAS', 'NY': 'NYK',
                'UTAH': 'UTA', 'WSH': 'WAS', 'PHO': 'PHX'}
TEAM_NAMES = dict(zip((
    'Atlanta Hawks|Boston Celtics|Brooklyn Nets|Charlotte Hornets|Chicago Bulls|'
    'Cleveland Cavaliers|Dallas Mavericks|Denver Nuggets|Detroit Pistons|Golden State Warriors|'
    'Houston Rockets|Indiana Pacers|LA Clippers|Los Angeles Lakers|Memphis Grizzlies|Miami Heat|'
    'Milwaukee Bucks|Minnesota Timberwolves|New Orleans Pelicans|New York Knicks|Oklahoma City Thunder|'
    'Orlando Magic|Philadelphia 76ers|Phoenix Suns|Portland Trail Blazers|Sacramento Kings|'
    'San Antonio Spurs|Toronto Raptors|Utah Jazz|Washington Wizards').lower().split('|'),
    'ATL BOS BKN CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS'.split()))
TEAM_NAMES['los angeles clippers'] = 'LAC'


def _name(value):
    plain = ''.join(c for c in unicodedata.normalize('NFKD', value or '') if not unicodedata.combining(c))
    words = re.sub(r'[^a-z0-9\s]', '', plain.lower()).split()
    while words and words[-1] in {'jr', 'sr', 'ii', 'iii', 'iv'}:
        words.pop()
    return ''.join(words)


def _team(value):
    value = (value or '').upper()
    return TEAM_ALIASES.get(value, value)


def _espn_team(group, athlete):
    team = athlete.get('team') or {}
    abbreviation = team.get('abbreviation') or group.get('abbreviation')
    if abbreviation:
        return _team(abbreviation)
    for name in (group.get('displayName'), team.get('displayName')):
        if (name or '').lower() in TEAM_NAMES:
            return TEAM_NAMES[name.lower()]
    for link in team.get('links', []) + athlete.get('links', []):
        match = re.search(r'/nba/team/(?:[^?]*/)?name/([a-z]+)', link.get('href', ''), re.I)
        if match:
            return _team(match[1])
    return ''


class Store:
    def __init__(self, root='cache'):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'gm.sqlite', timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS games (
                season TEXT, game_id TEXT, date TEXT, start_ms INTEGER, status TEXT,
                home TEXT, away TEXT, week INTEGER, PRIMARY KEY(season, game_id));
            CREATE INDEX IF NOT EXISTS games_tip ON games(start_ms);
            CREATE TABLE IF NOT EXISTS boxes (
                season TEXT, week INTEGER, player_id TEXT, game_id TEXT, date TEXT,
                team TEXT, opp TEXT, minutes REAL, stats TEXT,
                PRIMARY KEY(season, player_id, game_id));
            CREATE TABLE IF NOT EXISTS projections (
                season TEXT, week INTEGER, player_id TEXT, game_id TEXT, stats TEXT,
                known_ms INTEGER, fetched_ms INTEGER,
                PRIMARY KEY(season, player_id, game_id));
            CREATE TABLE IF NOT EXISTS projection_history (
                season TEXT, week INTEGER, player_id TEXT, game_id TEXT, stats TEXT,
                known_ms INTEGER, fetched_ms INTEGER,
                PRIMARY KEY(season, player_id, game_id, fetched_ms));
            CREATE TABLE IF NOT EXISTS projection_snapshots (
                season TEXT, week INTEGER, player_id TEXT, game_id TEXT,
                fetched_ms INTEGER, stats_json TEXT,
                PRIMARY KEY(season, player_id, game_id, fetched_ms));
            INSERT OR IGNORE INTO projection_snapshots
                SELECT p.season,p.week,p.player_id,p.game_id,p.fetched_ms,p.stats
                FROM projection_history p JOIN games g USING(season,game_id)
                WHERE p.fetched_ms<g.start_ms;
            INSERT OR IGNORE INTO projection_snapshots
                SELECT p.season,p.week,p.player_id,p.game_id,p.fetched_ms,p.stats
                FROM projections p JOIN games g USING(season,game_id)
                WHERE p.fetched_ms<g.start_ms;
        ''')

    def sync(self, season: str, weeks):
        """Atomically replace open weeks; never refresh a completed nonempty week."""
        season = str(season)
        for week in weeks:
            week = int(week)
            cached = self.games(season, week)
            if cached and all(g['status'] == 'complete' for g in cached):
                continue
            games = api.scores(season, week)
            if not games:
                break
            boxes = api.rest(f'/stats/nba/{season}/{week}?season_type=regular')
            projections = api.rest(f'/projections/nba/{season}/{week}?season_type=regular')
            fetched_ms = int(time.time() * 1000)
            game_rows = []
            for game in games:
                metadata = game.get('metadata') or {}
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                game_rows.append((season, str(game['game_id']), game['date'], int(game['start_time']),
                                  game['status'], (metadata.get('home_team') or {}).get('team'),
                                  (metadata.get('away_team') or {}).get('team'), week))
            box_rows, proj_rows = [], []
            for row in boxes:
                pid = str(row['player_id'])
                if pid.startswith('TEAM_'):
                    continue
                stats = row.get('stats') or {}
                minutes = stats.get('min')
                if minutes is None:
                    minutes = (stats.get('sp') or 0) / 60
                box_rows.append((season, week, pid, str(row['game_id']), row.get('date'),
                                 row.get('team'), row.get('opponent'), float(minutes), json.dumps(stats)))
            for row in projections:
                pid = str(row['player_id'])
                if pid.startswith('TEAM_'):
                    continue
                # Archive rows are sometimes revised after tip. Preserve provenance.
                stamps = [int(row[k]) for k in ('updated_at', 'last_modified') if row.get(k)]
                proj_rows.append((season, week, pid, str(row['game_id']), json.dumps(row.get('stats') or {}),
                                  max(stamps) if stamps else None, fetched_ms))
            boxed_games = {row[3] for row in box_rows}
            if any(row[4] == 'complete' and row[1] not in boxed_games for row in game_rows):
                raise api.SleeperError(f'incomplete box snapshot for {season} week {week}; retry sync later')
            with self.db:
                # Retain observed pre-tip versions before refreshing the latest view.
                # A future refit can then use genuine pregame forecasts.
                self.db.execute('''INSERT OR IGNORE INTO projection_history
                    SELECT p.* FROM projections p JOIN games g USING(season,game_id)
                    WHERE p.season=? AND p.week=? AND p.fetched_ms<g.start_ms''', (season, week))
                for table in ('games', 'boxes', 'projections'):
                    self.db.execute(f'DELETE FROM {table} WHERE season=? AND week=?', (season, week))
                self.db.executemany('INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?)', game_rows)
                self.db.executemany('INSERT OR REPLACE INTO boxes VALUES (?,?,?,?,?,?,?,?,?)', box_rows)
                self.db.executemany('INSERT OR REPLACE INTO projections VALUES (?,?,?,?,?,?,?)', proj_rows)
                # Timestamp the response, not the request. Preserve every fetch,
                # even unchanged forecasts, but never a fetch at or after tip.
                self.db.execute('''INSERT OR IGNORE INTO projection_snapshots
                    SELECT p.season,p.week,p.player_id,p.game_id,p.fetched_ms,p.stats
                    FROM projections p JOIN games g USING(season,game_id)
                    WHERE p.season=? AND p.week=? AND p.fetched_ms<g.start_ms''', (season, week))
                self.db.execute('''INSERT OR IGNORE INTO projection_history
                    SELECT p.* FROM projections p JOIN games g USING(season,game_id)
                    WHERE p.season=? AND p.week=? AND p.fetched_ms<g.start_ms''', (season, week))

    def _cached_json(self, filename, max_age_s, fetch):
        path = self.root / filename
        if path.exists() and time.time() - path.stat().st_mtime < max_age_s:
            try:
                return json.loads(path.read_text())
            except (ValueError, OSError):
                pass
        data = fetch()
        # Unique temp paths allow other readers to see only complete JSON.
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', dir=self.root, prefix=filename, delete=False) as stream:
            json.dump(data, stream)
            temporary = Path(stream.name)
        temporary.replace(path)
        return data

    def players(self, max_age_h=12):
        return self._cached_json('players.json', max_age_h * 3600, lambda: api.rest('/v1/players/nba'))

    def injuries(self, max_age_min=30):
        def fetch():
            req = urllib.request.Request(ESPN_INJURIES, headers={'User-Agent': api.UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        data = self._cached_json('injuries.json', max_age_min * 60, fetch)
        lookup = {}
        for pid, player in self.players().items():
            if pid.startswith('TEAM_') or not player.get('team'):
                continue
            name = player.get('full_name') or f"{player.get('first_name', '')} {player.get('last_name', '')}"
            lookup.setdefault((_name(name), _team(player['team'])), []).append(pid)
        result = {'_unmatched': []}
        groups = data.get('injuries', [])
        if isinstance(groups, dict):
            groups = groups.values()
        for group in groups:
            for injury in group.get('injuries', []):
                athlete = injury.get('athlete') or {}
                name, team = athlete.get('displayName', ''), _espn_team(group, athlete)
                details = injury.get('details') or {}
                return_date = str(details.get('returnDate') or '')[:10]
                try:
                    return_date = date.fromisoformat(return_date).isoformat()
                except ValueError:
                    return_date = None
                entry = {'status': injury.get('status'), 'return_date': return_date,
                         'type': details.get('type'), 'detail': details.get('detail'), 'side': details.get('side'),
                         'short': injury.get('shortComment'), 'long': injury.get('longComment'), 'date': injury.get('date')}
                ids = lookup.get((_name(name), team), [])
                if len(ids) == 1:
                    result[ids[0]] = entry
                else:
                    result['_unmatched'].append({'name': name, 'team': team, **entry})
        return result

    def games(self, season, week):
        return [dict(row) for row in self.db.execute(
            'SELECT game_id,date,start_ms,status,home,away,week FROM games WHERE season=? AND week=? ORDER BY start_ms,game_id',
            (str(season), int(week)))]

    def box(self, season=None, before_ms=None, player_ids=None):
        clauses, values = [], []
        if season is not None:
            clauses.append('b.season=?')
            values.append(str(season))
        if before_ms is not None:
            clauses.append('g.start_ms<?')
            values.append(int(before_ms))
        if player_ids is not None:
            player_ids = list(player_ids)
            if not player_ids:
                return []
            clauses.append('b.player_id IN (' + ','.join('?' for _ in player_ids) + ')')
            values.extend(str(pid) for pid in player_ids)
        query = ('SELECT b.player_id,b.game_id,b.date,g.start_ms,b.team,b.opp,b.minutes,b.stats '
                 'FROM boxes b JOIN games g ON b.season=g.season AND b.game_id=g.game_id')
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        rows = []
        for row in self.db.execute(query + ' ORDER BY g.start_ms,b.game_id,b.player_id', values):
            item = dict(row)
            item['stats'] = json.loads(item['stats'])
            rows.append(item)
        return rows

    def proj(self, season, week):
        return {(row['player_id'], row['game_id']): json.loads(row['stats']) for row in self.db.execute(
            'SELECT player_id,game_id,stats FROM projections WHERE season=? AND week=?', (str(season), int(week)))}

    def _schedule(self):
        """Internal model/backtest index; public games() keeps its exact contract."""
        return [dict(row) for row in self.db.execute('SELECT * FROM games ORDER BY start_ms,game_id')]

    def _projection_rows(self):
        return [dict(row) for row in self.db.execute(
            '''SELECT p.* FROM projections p WHERE NOT EXISTS (
                   SELECT 1 FROM projection_snapshots s WHERE s.season=p.season
                   AND s.player_id=p.player_id AND s.game_id=p.game_id AND s.fetched_ms=p.fetched_ms)
               UNION SELECT p.* FROM projection_history p WHERE NOT EXISTS (
                   SELECT 1 FROM projection_snapshots s WHERE s.season=p.season
                   AND s.player_id=p.player_id AND s.game_id=p.game_id AND s.fetched_ms=p.fetched_ms)
               UNION SELECT season,week,player_id,game_id,stats_json,NULL,fetched_ms
               FROM projection_snapshots ORDER BY fetched_ms''')]
