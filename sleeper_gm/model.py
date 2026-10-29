"""Conditional scoring distributions with prequential residuals and live availability."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
from pathlib import Path

import numpy as np

from sleeper_gm.data import TEAM_NAMES
from sleeper_gm.scoring import fantasy_points

# Status play multipliers, all in one labelled dictionary. Official-report rates
# Jan 18-Oct 22 2026, n=2,007: https://isheplayingtonight.com/nba-injuries/questionable-play-rate
# Sleeper tag meanings: https://support.sleeper.com/en/articles/4714981-basketball-injury-designations
# B2B multiplier .95 is a judgement prior motivated by ESPN's 2016 finding
# that 74% of rest DNPs occurred on the second night, not an estimated risk ratio.
# DTD later-game .75, Out returning .45 and undated horizon .5 are judgement
# priors, not measured play rates. Healthy status adds no second availability tax.
PLAY_PRIORS = {None: 1., 'probable': .866, 'questionable': .483, 'doubtful': .042,
               'gtd': .5, 'day-to-day': .3, 'day-to-day_later': .75,
               'out': 0., 'out_before_return': 0., 'out_returning': .45,
               'ir': 0., 'suspended': 0., 'dnr': 0., 'free_agent': 0.,
               'unavailable_undated_weeks': .5, 'heavy_minutes_b2b': .95}
STATUS_ALIASES = {'dtd': 'day-to-day', 'd2d': 'day-to-day', 'sus': 'suspended'}
INJURY_STATUSES = frozenset({'out', 'ir', 'day-to-day', 'gtd', 'questionable', 'doubtful', 'suspended'})
LEAGUE_TZ = ZoneInfo('America/New_York')
# Shrinkage/decay strengths are labelled priors, not fits to the evaluation set.
# Last-state centers: user-supplied 2025-26 ckloote/sleeper-lineup measurements,
# P(play | last played)=.91, P(play | last DNP)=.236, rounded to .24.
LAST_PLAYED_PRIOR = .91
LAST_DNP_PRIOR = .24
LAST_PLAYED_STRENGTH = 9.
LAST_DNP_STRENGTH = 3.
SCRATCH_STREAK_DECAY = .7
# Return prior: five appearances, .80/.84/.88/.92/.96, then full minutes.
# Research motivates a taper, not these exact weights: PMC11126319 reports
# -10.9% game score over 20 return games; PNAS Nexus pgac176 reports 25.8 ->
# 21.6 minutes after 14+ missed games. A 25-minute first-game cap for 14+
# missed games fades over the same five appearances. No fit claimed.
RETURN_GAMES = 5
INJURY_STREAK_GAMES = 3
LONG_ABSENCE_GAMES = 14
RETURN_MINUTES_CAP = 25.
# Labelled prior, not a leakage-free fit: the 2026 retrospective Sleeper archive
# had MAE 5.11 versus 5.63 for our model, but may contain post-tip revisions.
SLEEPER_BLEND_PRIOR = .5
PARTICIPATION_PRIOR = .6
PARTICIPATION_PRIOR_GAMES = 3.
PARTICIPATION_HALF_LIFE = 3.
NBA_TEAMS = frozenset(TEAM_NAMES.values())
DEFAULT_PARAMS = {'blend_weight': SLEEPER_BLEND_PRIOR, 'rate_half_life': 15.0, 'minutes_half_life': 5.0,
                  'rate_prior_games': 10.0, 'minutes_prior_games': 5.0,
                  'residual_prior_games': 30.0, 'residual_scale': 1.0,
                  'return_haircut': .8, 'residual_noise': .025}
BUNDLED_PARAMS = Path(__file__).with_name('model_params.json')


def _params_path():
    """A refit from `sleeper-gm backtest` (in the user's state dir) wins over the bundled fit."""
    from sleeper_gm import config
    user = config.STATE_DIR / 'model_params.json'
    return user if user.exists() else BUNDLED_PARAMS


PARAMS_PATH = BUNDLED_PARAMS
# A conservative final-result lag prevents earlier games that evening informing later tips.
RESULT_LAG_MS = 6 * 60 * 60 * 1000


@dataclass(frozen=True)
class GameDist:
    p_play: float
    samples: np.ndarray
    mean: float


def _params(asof_ms):
    params = dict(DEFAULT_PARAMS)
    PARAMS_PATH = globals()['PARAMS_PATH']
    if PARAMS_PATH == BUNDLED_PARAMS:
        PARAMS_PATH = _params_path()
    if not PARAMS_PATH.exists():
        return params
    data = json.loads(PARAMS_PATH.read_text())
    if asof_ms >= data.get('trained_before_ms', float('inf')):
        params.update(data.get('params', {}))
        # Older artifacts disabled the blend when it was unidentifiable.
        if not data.get('fit', {}).get('blend_identifiable', False):
            params['blend_weight'] = SLEEPER_BLEND_PRIOR
    elif asof_ms >= data.get('evaluation_trained_before_ms', float('inf')):
        params.update(data.get('evaluation_params', {}))
    elif asof_ms >= data.get('initial_trained_before_ms', float('inf')):
        params.update(data.get('initial_params', {}))
    return params


def _season(game):
    if game.get('season'):
        return str(game['season'])
    date = datetime.fromtimestamp(game['start_ms'] / 1000, timezone.utc)
    return str(date.year if date.month >= 9 else date.year - 1)


def _projection_index(store, scoring):
    result = defaultdict(list)
    for row in store._projection_rows():
        stats = json.loads(row['stats'])
        minutes = stats.get('min')
        if minutes is None and stats.get('sp') is not None:
            minutes = stats['sp'] / 60
        result[(row['player_id'], row['game_id'])].append((
            fantasy_points(stats, scoring) if stats else None, row['known_ms'], row['fetched_ms'],
            minutes, stats.get('pts') == 0 and minutes == 0))
    return result


def _projection_snapshot(index, pid, game, asof_ms):
    # A vendor timestamp on a later archive fetch cannot establish provenance.
    candidates = [r for r in index.get((pid, game['game_id']), [])
                  if r[2] <= asof_ms and r[2] < game['start_ms']]
    return max(candidates, key=lambda r: r[2]) if candidates else None


def _projection(index, pid, game, asof_ms):
    row = _projection_snapshot(index, pid, game, asof_ms)
    return row[0] if row is not None and row[3] != 0 else None


class _Engine:
    """Incremental history used identically by Model and the walk-forward runner.

    Call predict for all rows at a tip before observing any of those rows. Only
    completed rows with positive minutes enter conditional-on-playing history.
    """
    def __init__(self, scoring, params, schedule, players, projections):
        self.scoring, self.params = scoring, params
        self.projections = projections
        self.players = players
        self.schedule = {g['game_id']: g for g in schedule}
        self.team_tips = defaultdict(list)
        self.completed_team_tips = defaultdict(list)
        self.team_games = defaultdict(list)
        for g in schedule:
            if g['status'] in {'canceled', 'postponed'}:
                continue
            for team in (g['home'], g['away']):
                self.team_tips[(_season(g), team)].append(g['start_ms'])
                if g['status'] == 'complete':
                    self.completed_team_tips[(_season(g), team)].append(g['start_ms'])
                self.team_games[(_season(g), team)].append(g)
        for tips in list(self.team_tips.values()) + list(self.completed_team_tips.values()):
            tips.sort()
        self.history = defaultdict(list)
        self.appearances = defaultdict(list)
        self.position_totals = defaultdict(lambda: np.zeros(3))
        self.player_totals = defaultdict(lambda: np.zeros(3))
        self.residuals = defaultdict(lambda: deque(maxlen=80))
        self.pool = defaultdict(lambda: deque(maxlen=3000))
        self._weights = {}
        prior_stats = {'pts': .65, 'reb': .20, 'ast': .13, 'stl': .025, 'blk': .02,
                       'to': .08, 'tpm': .07, 'ftmi': .035, 'dd': .005, 'td': .0005}
        self.fallback_rate = max(.05, fantasy_points(prior_stats, scoring))

    def _stint(self, rows, team):
        """The latest observed stint includes DNP rows, not just appearances."""
        last_other = max((i for i, r in enumerate(rows) if r['team'] != team), default=-1)
        return [r for r in rows[last_other + 1:] if r['team'] == team]

    def _participation_games(self, rows, season, team, asof_ms, joined=0):
        played = {r['game_id'] for r in rows if r['minutes'] > 0}
        return [(g, float(g['game_id'] in played)) for g in self.team_games[(season, team)]
                if g['status'] == 'complete' and g['start_ms'] >= joined
                and g['start_ms'] + RESULT_LAG_MS <= asof_ms]

    def participation(self, pid, game, asof_ms, projection_evidence=True, injury=None, injured=False):
        season, team = _season(game), game.get('team')
        if not team:
            return 0.
        rows = self.appearances.get(pid, [])
        previous = [r for r in rows if r['season'] == str(int(season) - 1)]
        old_games = []
        if previous:
            last_team = previous[-1]['team']
            stint = self._stint(previous, last_team)
            old_games = self._participation_games(stint, str(int(season) - 1), last_team,
                                                   asof_ms, stint[0]['start_ms'])
        current = [r for r in rows if r['season'] == season]
        stint = self._stint(current, team)
        joined = stint[0]['start_ms'] if stint and any(r['team'] != team for r in current) else 0
        player = self.players.get(pid, {})
        changed = player.get('team_changed_at')
        if changed and player.get('team') == team:
            try:
                changed = int(changed)
            except (TypeError, ValueError):
                changed = int(datetime.fromisoformat(changed.replace('Z', '+00:00')).timestamp() * 1000)
            if changed <= asof_ms:
                joined = max(joined, changed)
        current_games = self._participation_games(stint, season, team, asof_ms, joined)
        games = sorted(old_games + current_games, key=lambda item: item[0]['start_ms'])
        excluded = set()
        streak = []
        # Only an already-observed return resolves an historical injury streak.
        # Prefix filtering above prevents future returns from rewriting backtests.
        for i, (_, played) in enumerate(games):
            if not played:
                streak.append(i)
            else:
                if len(streak) >= INJURY_STREAK_GAMES:
                    excluded.update(streak)
                streak = []
        if injured:
            # ESPN date is a report update, not reliably injury onset. The
            # ongoing streak is one injury episode, even if it predates the update.
            # With no dated record, this is the labelled current-episode prior.
            excluded.update(streak)
            reported = (injury or {}).get('date')
            if reported:
                reported = str(reported)[:10]
                excluded.update(i for i, (g, played) in enumerate(games)
                                if not played and (g.get('date') or '') >= reported)
        healthy = [(g, value) for i, (g, value) in enumerate(games) if i not in excluded]
        previous_values = [value for g, value in healthy if _season(g) != season]
        prior = float(np.mean(previous_values)) if previous_values else PARTICIPATION_PRIOR
        values = np.array([value for g, value in healthy if _season(g) == season])
        if len(values) >= 4 and values[-4:].all():
            prior = 1.
        n = len(values)
        weights = np.exp2(-np.arange(n - 1, -1, -1) / PARTICIPATION_HALF_LIFE)
        strength = PARTICIPATION_PRIOR_GAMES * 2 ** (-n / PARTICIPATION_HALF_LIFE)
        total = float(weights.sum() + strength)
        own = (float(np.dot(values, weights)) + strength * prior) / total
        # State comes from the last *healthy* team game, never from an injury DNP.
        # With no current-season healthy evidence, use the player's healthy rate;
        # an old season-ending DNP must not assert a present coach's decision.
        if n and values[-1]:
            probability = (LAST_PLAYED_STRENGTH * LAST_PLAYED_PRIOR + own) / (LAST_PLAYED_STRENGTH + 1)
        elif n:
            scratches = next((i for i, value in enumerate(values[::-1]) if value), n)
            probability = (LAST_DNP_STRENGTH * LAST_DNP_PRIOR + own) / (LAST_DNP_STRENGTH + 1)
            probability *= SCRATCH_STREAK_DECAY ** (scratches - 1)
        else:
            probability = own
        snapshot = _projection_snapshot(self.projections, pid, game, asof_ms) if projection_evidence else None
        if snapshot is not None and snapshot[4]:
            probability *= total / (total + 1)
        return float(probability)

    def _position(self, pid):
        return self.players.get(pid, {}).get('position') or 'ALL'

    def _prior(self, pid):
        own = self.player_totals[pid]
        group = self.position_totals[self._position(pid)] - own
        all_players = self.position_totals['ALL'] - own
        league_rate = all_players[0] / all_players[1] if all_players[1] else self.fallback_rate
        rate = (group[0] + 600 * league_rate) / (group[1] + 600)
        minutes = (all_players[1] + 20 * 18) / (all_players[2] + 20)
        return rate, minutes

    def _weighted(self, values, half_life):
        n = len(values)
        key = (n, half_life)
        if key not in self._weights:
            w = np.exp2(-np.arange(n - 1, -1, -1) / half_life)
            self._weights[key] = w / w.sum()
        return float(np.dot(values, self._weights[key]))

    def _return_progress(self, history, game, asof_ms=None):
        """Most recent 3+ game absence, and completed appearances since return."""
        season = _season(game)
        relevant = [r for r in history if r['season'] == season]
        cutoff = min(game['start_ms'], asof_ms) if asof_ms is not None else game['start_ms']
        # Include the target as a possible first return; then scan back at most
        # five appearances. Canceled/postponed games are absent from team_tips.
        for offset in range(min(RETURN_GAMES, len(relevant) + 1)):
            next_game = game if offset == 0 else relevant[-offset]
            previous = relevant[-offset - 1] if len(relevant) > offset else None
            if previous and next_game.get('team') != previous['team']:
                continue
            tips = self.completed_team_tips[(season, next_game.get('team'))]
            start = bisect_left(tips, previous['start_ms'] + 1) if previous else 0
            end = min(bisect_left(tips, next_game['start_ms']), bisect_right(tips, cutoff - RESULT_LAG_MS))
            missed = end - start
            if missed >= INJURY_STREAK_GAMES:
                return offset, missed
        return RETURN_GAMES, 0

    def _haircut(self, history, game, asof_ms=None):
        offset, _ = self._return_progress(history, game, asof_ms)
        return 1 - (1 - self.params['return_haircut']) * (RETURN_GAMES - offset) / RETURN_GAMES

    def components(self, pid, game, asof_ms=None, haircut=True):
        season = _season(game)
        history = self.history.get(pid, [])
        current = [r for r in history if r['season'] == season]
        previous = [r for r in history if r['season'] == str(int(season) - 1)]
        # Rates transfer across teams; minutes belong to the current role.
        current_role = self._stint([r for r in self.appearances.get(pid, []) if r['season'] == season],
                                  game.get('team'))
        role_ids = {r['game_id'] for r in current_role}
        role_minutes = [r['minutes'] for r in current if r['game_id'] in role_ids]
        prior_rate, prior_minutes = self._prior(pid)
        if previous:
            n = len(previous)
            last_rate = sum(r['points'] for r in previous) / sum(r['minutes'] for r in previous)
            prior_rate = (n * last_rate + 15 * prior_rate) / (n + 15)
            prior_minutes = (sum(r['minutes'] for r in previous) + 5 * prior_minutes) / (n + 5)
            if previous[-1]['team'] != game.get('team'):
                prior_minutes = self._prior(pid)[1]
        n = len(current)
        if current:
            minutes = [r['minutes'] for r in current]
            points = [r['points'] for r in current]
            rate = self._weighted(points, self.params['rate_half_life']) / self._weighted(minutes, self.params['rate_half_life'])
            strength = self.params['rate_prior_games']
            rate = (n * rate + strength * prior_rate) / (n + strength)
            minute_values = role_minutes
            recent_minutes = self._weighted(minute_values, self.params['minutes_half_life']) if minute_values else prior_minutes
            # After two appearances the current role controls minutes. A large
            # role shift further weakens the old-season minutes prior.
            strength = min(self.params['minutes_prior_games'], 1. if len(minute_values) >= 2 else 3.)
            if abs(recent_minutes - prior_minutes) >= 6 or (previous and previous[-1]['team'] != game.get('team')):
                strength = min(strength, .5)
            minutes = ((len(minute_values) * recent_minutes + strength * prior_minutes) / (len(minute_values) + strength)
                       if minute_values else prior_minutes)
            average = sum(points) / n
        else:
            rate, minutes, average = prior_rate, prior_minutes, None
        if haircut:
            healthy_minutes = minutes
            minutes *= self._haircut(history, game, asof_ms)
            offset, missed = self._return_progress(history, game, asof_ms)
            if missed >= LONG_ABSENCE_GAMES:
                cap = RETURN_MINUTES_CAP + max(0., healthy_minutes - RETURN_MINUTES_CAP) * offset / RETURN_GAMES
                minutes = min(minutes, cap)
        raw_mean = max(.25, rate * minutes)
        projection = _projection(self.projections, pid, game, asof_ms if asof_ms is not None else game['start_ms'])
        return raw_mean, projection, average

    def predict(self, pid, game, asof_ms=None, haircut=True):
        raw, projection, average = self.components(pid, game, asof_ms, haircut)
        blend = self.params['blend_weight'] if projection is not None else 0
        return max(.25, (1 - blend) * raw + blend * (projection or 0)), average

    def observe(self, row, predicted):
        game = self.schedule.get(row['game_id'])
        if game is None or game['status'] != 'complete':
            return
        # The regular-season feed also contains All-Star teams. Those are not
        # NBA roster changes and must not restart a player's team stint.
        if row['team'] in NBA_TEAMS:
            self.appearances[row['player_id']].append({**row, 'season': _season(game)})
        if row['minutes'] <= 0:
            return
        points = fantasy_points(row['stats'], self.scoring)
        record = {**row, 'season': _season(game), 'points': points}
        prior_n = len(self.history[row['player_id']])
        self.history[row['player_id']].append(record)
        values = np.array([points, row['minutes'], 1])
        self.position_totals['ALL'] += values
        self.player_totals[row['player_id']] += values
        position = self._position(row['player_id'])
        if position != 'ALL':
            self.position_totals[position] += values
        # Exclude uninformative cold-start ratios, but retain negative fantasy scores.
        if prior_n >= 3 and predicted >= 3:
            ratio = points / predicted
            self.residuals[row['player_id']].append(ratio)
            self.pool[int(predicted // 10)].append(ratio)

    def samples(self, pid, game, mean, n_samples, seed, raw_mean=None):
        raw_mean = mean if raw_mean is None else raw_mean
        digest = hashlib.blake2b(f"{seed}:{pid}:{game['game_id']}".encode(), digest_size=8).digest()
        rng = np.random.default_rng(int.from_bytes(digest, 'little'))
        personal = np.asarray(self.residuals.get(pid, []), dtype=float)
        bucket = int(raw_mean // 10)
        pooled = list(self.pool.get(bucket, []))
        if len(pooled) < 50:
            pooled += list(self.pool.get(bucket - 1, [])) + list(self.pool.get(bucket + 1, []))
        if pooled:
            pooled = np.asarray(pooled, dtype=float)
        else:
            # Only a cold-start fallback; empirical residuals replace it as data arrive.
            pooled = np.random.default_rng(0).lognormal(-.45 ** 2 / 2, .45, 2048)
        weight = len(personal) / (len(personal) + self.params['residual_prior_games'])
        residual = rng.choice(pooled, n_samples)
        if len(personal):
            mask = rng.random(n_samples) < weight
            residual[mask] = rng.choice(personal, int(mask.sum()))
        center = weight * (personal.mean() if len(personal) else 1) + (1 - weight) * pooled.mean()
        residual /= max(.1, center)
        residual = 1 + self.params['residual_scale'] * (residual - 1)
        residual += rng.normal(0, self.params['residual_noise'], n_samples)
        return mean + raw_mean * (residual - 1)


def _history_engine(rows, cutoff, scoring, params, schedule, players, projections):
    engine = _Engine(scoring, params, schedule, players, projections)
    pending = deque()
    for row in rows:
        tip = row['start_ms']
        if tip >= cutoff:
            break
        while pending and pending[0][0] <= tip:
            _, old, predicted = pending.popleft()
            engine.observe(old, predicted)
        game = engine.schedule.get(row['game_id'])
        if not game or game['status'] != 'complete':
            continue
        game = {**game, 'team': row['team'], 'opp': row['opp']}
        mean = engine.components(row['player_id'], game)[0] if row['minutes'] > 0 else 0
        pending.append((tip + RESULT_LAG_MS, row, mean))
    while pending and pending[0][0] <= cutoff:
        _, row, mean = pending.popleft()
        engine.observe(row, mean)
    return engine


class Model:
    def __init__(self, store, scoring: dict, asof_ms: int, n_samples=600, seed=0):
        if n_samples <= 0:
            raise ValueError('n_samples must be positive')
        self.store, self.scoring, self.asof_ms = store, dict(scoring), int(asof_ms)
        self.n_samples, self.seed = int(n_samples), seed
        self.params = _params(self.asof_ms)
        self._rows = store.box(before_ms=self.asof_ms)
        self._schedule = store._schedule()
        self._games = {g['game_id']: g for g in self._schedule}
        self._players = store.players()
        self._projections = _projection_index(store, self.scoring)
        self._engines = {}
        self._injuries = None

    def _engine(self, cutoff):
        if cutoff not in self._engines:
            self._engines[cutoff] = _history_engine(self._rows, cutoff, self.scoring, _params(cutoff),
                self._schedule, self._players, self._projections)
        return self._engines[cutoff]

    def _live_injury(self, player_id, cutoff):
        # Historical predictions cannot use today's status or ESPN report.
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if cutoff < now_ms - 5 * 60 * 1000:
            return {}, [], False
        if self._injuries is None:
            self._injuries = self.store.injuries()
        injury = self._injuries.get(str(player_id), {})
        player = self._players.get(str(player_id), {})
        statuses = [(player.get('injury_status') or '').lower(), (injury.get('status') or '').lower()]
        statuses = [STATUS_ALIASES.get(status, status) for status in statuses]
        return injury, statuses, bool(injury) or any(s in INJURY_STATUSES for s in statuses)

    def _injury_prior(self, player_id, game):
        cutoff = min(self.asof_ms, game['start_ms'])
        injury, statuses, _ = self._live_injury(player_id, cutoff)
        if not game.get('team') or (statuses and not self._players.get(str(player_id), {}).get('team')):
            return PLAY_PRIORS['free_agent']
        unavailable = [PLAY_PRIORS[s] for s in statuses if s in {'ir', 'suspended', 'dnr'}]
        if unavailable:
            return min(unavailable)
        if 'out' in statuses:
            return_date = injury.get('return_date')
            if return_date:
                game_date = game.get('date') or datetime.fromtimestamp(game['start_ms'] / 1000, LEAGUE_TZ).date().isoformat()
                return PLAY_PRIORS['out_before_return' if return_date > game_date else 'out_returning']
            return PLAY_PRIORS['out']
        team = game.get('team') or self._players.get(str(player_id), {}).get('team')
        remaining = [g for g in self._schedule if team in (g['home'], g['away'])
                     and g['status'] not in {'canceled', 'postponed'} and g['start_ms'] >= cutoff]
        next_tip = min((g['start_ms'] for g in remaining), default=game['start_ms'])
        statuses = ['day-to-day_later' if s == 'day-to-day' and game['start_ms'] > next_tip else s for s in statuses]
        return min((PLAY_PRIORS.get(s or None, PLAY_PRIORS[None]) for s in statuses), default=PLAY_PRIORS[None])

    def p_play(self, player_id, game):
        player_id = str(player_id)
        game = {**self._games.get(game['game_id'], {}), **game}
        game.setdefault('team', self._players.get(player_id, {}).get('team'))
        cutoff = min(self.asof_ms, int(game['start_ms']))
        engine = self._engine(cutoff)
        injury, _, injured = self._live_injury(player_id, cutoff)
        participation = engine.participation(player_id, game, cutoff, injury=injury, injured=injured)
        history = [r for r in engine.history.get(player_id, [])
                   if r['season'] == _season(game) and r['team'] == game.get('team')]
        minutes = np.mean([r['minutes'] for r in history]) if history else 0.
        day = datetime.fromtimestamp(game['start_ms'] / 1000, LEAGUE_TZ).date()
        b2b = any(datetime.fromtimestamp(tip / 1000, LEAGUE_TZ).date() == day - timedelta(days=1)
                  for tip in engine.team_tips[(_season(game), game.get('team'))] if tip < game['start_ms'])
        rest = PLAY_PRIORS['heavy_minutes_b2b'] if b2b and minutes >= 30 else 1.
        return self._injury_prior(player_id, game) * participation * rest

    def dist(self, player_id: str, game: dict):
        player_id = str(player_id)
        game = {**self._games.get(game['game_id'], {}), **game}
        game.setdefault('team', self._players.get(player_id, {}).get('team'))
        cutoff = min(self.asof_ms, int(game['start_ms']))
        engine = self._engine(cutoff)
        mean, _ = engine.predict(player_id, game, cutoff)
        raw = engine.components(player_id, game, cutoff)[0]
        samples = engine.samples(player_id, game, mean, self.n_samples, self.seed, raw_mean=raw)
        return GameDist(self.p_play(player_id, game), samples.astype(float), float(mean))

    def ros_value(self, player_id, asof_ms):
        cutoff = min(self.asof_ms, int(asof_ms))
        engine = self._engine(cutoff)
        known_seasons = [g['season'] for g in self._schedule if g['start_ms'] <= cutoff]
        season = max(known_seasons) if known_seasons else _season({'start_ms': cutoff})
        game = {'game_id': '__ros__', 'start_ms': cutoff, 'season': season,
                'team': self._players.get(str(player_id), {}).get('team')}
        mean, _ = engine.predict(str(player_id), game, cutoff, haircut=False)
        injury, _, injured = self._live_injury(player_id, cutoff)
        return float(mean * engine.participation(str(player_id), game, cutoff, projection_evidence=False,
                                                injury=injury, injured=injured))

    def availability_weeks(self, pid, asof_ms, horizon_weeks=4):
        """Fraction of current + next h-1 Monday-Sunday weeks with return by Sunday.

        Lock-In needs one playable game, so a partial return week counts once.
        This is return-date availability, not a schedule or participation forecast.
        Out/IR/suspension without a date uses the labelled .5 horizon prior.
        Historical calls without injury snapshots use the neutral healthy prior.
        """
        if not isinstance(horizon_weeks, int) or horizon_weeks <= 0:
            raise ValueError('horizon_weeks must be a positive integer')
        cutoff = min(self.asof_ms, int(asof_ms))
        injury, statuses, _ = self._live_injury(pid, cutoff)
        if statuses and not self._players.get(str(pid), {}).get('team'):
            return 0.
        return_date = injury.get('return_date')
        if return_date is None:
            unavailable = any(s in {'out', 'ir', 'suspended', 'dnr'} for s in statuses)
            return PLAY_PRIORS['unavailable_undated_weeks'] if unavailable else 1.
        today = datetime.fromtimestamp(cutoff / 1000, LEAGUE_TZ).date()
        monday = today - timedelta(days=today.weekday())
        returns = date.fromisoformat(return_date)
        return sum(returns <= monday + timedelta(days=7 * i + 6)
                   for i in range(horizon_weeks)) / horizon_weeks
