"""Paced public-data sync, chronological parameter fitting, and held-out evaluation.

Run: .venv/bin/python -m sleeper_gm.backtest
Historical injury probabilities are deliberately not evaluated.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path
import time

import numpy as np

from sleeper_gm import api
from sleeper_gm.data import Store
from sleeper_gm.model import (BUNDLED_PARAMS, DEFAULT_PARAMS, RESULT_LAG_MS, SLEEPER_BLEND_PRIOR,
                      _Engine, _projection_index)
from sleeper_gm.scoring import fantasy_points


def metrics(actual, means, samples):
    actual, means, samples = np.asarray(actual), np.asarray(means), np.asarray(samples)
    if not len(actual):
        return {'n': 0, 'mae': float('nan'), 'crps': float('nan'),
                'q10': float('nan'), 'q50': float('nan'), 'q90': float('nan')}
    n = samples.shape[1]
    ordered = np.sort(samples, axis=1)
    spread = ordered @ (2 * np.arange(1, n + 1) - n - 1) / n ** 2
    crps = np.mean(np.abs(samples - actual[:, None]), axis=1) - spread
    quantiles = np.quantile(samples, [.1, .5, .9], axis=1)
    return {'n': len(actual), 'mae': float(np.mean(np.abs(actual - means))),
            'crps': float(crps.mean()), **{f'q{int(q * 100)}': float(np.mean(actual <= v))
            for q, v in zip([.1, .5, .9], quantiles)}}


def _selected(rows, maximum, predicate):
    candidates = [r for r in rows if predicate(r)]
    if len(candidates) > maximum:
        candidates.sort(key=lambda r: hashlib.blake2b(
            f"{r['player_id']}:{r['game_id']}".encode(), digest_size=8).digest())
        candidates = candidates[:maximum]
    return {(r['player_id'], r['game_id']) for r in candidates}


def _run(rows, scoring, params, schedule, players, projections, selected=None, samples=0):
    """Stream the exact chronological prefix instead of re-querying SQLite per tip.

    Each row is delayed six hours before entering history. The prediction and
    its residual are made before observing that row, including equal-tip games.
    """
    engine = _Engine(scoring, params, schedule, players, projections)
    pending = deque()
    output = []
    for row in rows:
        tip = row['start_ms']
        while pending and pending[0][0] <= tip:
            _, previous, predicted = pending.popleft()
            engine.observe(previous, predicted)
        game = {**engine.schedule[row['game_id']], 'team': row['team'], 'opp': row['opp']}
        if game['status'] != 'complete':
            continue
        if row['minutes'] <= 0:
            pending.append((tip + RESULT_LAG_MS, row, 0))
            continue
        take = selected is None or (row['player_id'], row['game_id']) in selected
        # Mean-only grid search need not calculate unused historical residuals.
        if take or samples:
            raw, projection, average = engine.components(row['player_id'], game)
            blend = params['blend_weight'] if projection is not None else 0
            mean = max(.25, (1 - blend) * raw + blend * (projection or 0))
        else:
            raw = mean = 0
        if take:
            archived = projections.get((row['player_id'], row['game_id']))
            entry = {'player_id': row['player_id'], 'game_id': row['game_id'], 'tip': tip,
                     'season': game['season'], 'week': game['week'], 'actual': fantasy_points(row['stats'], scoring),
                     'raw': raw, 'mean': mean, 'projection': projection, 'average': average,
                     'archived': archived[-1][0] if archived else None,
                     'high': average is not None and average >= 20}
            if samples:
                entry['samples'] = engine.samples(row['player_id'], game, mean, samples, 0, raw_mean=raw)
            output.append(entry)
        pending.append((tip + RESULT_LAG_MS, row, raw))
    return output


def _fit(rows, scoring, schedule, players, projections, selected, label):
    best, best_loss, identifiable = None, float('inf'), 0
    for rate, minutes in itertools.product([3., 10., 30.], [1.5, 4., 10.]):
        params = {**DEFAULT_PARAMS, 'rate_half_life': rate, 'minutes_half_life': minutes}
        records = _run(rows, scoring, params, schedule, players, projections, selected)
        raw = np.array([r['raw'] for r in records])
        actual = np.array([r['actual'] for r in records])
        projection = np.array([r['projection'] if r['projection'] is not None else r['raw'] for r in records])
        identifiable = sum(r['projection'] is not None for r in records)
        for blend in ([0., .25, .5, .75, 1.] if identifiable else [SLEEPER_BLEND_PRIOR]):
            loss = float(np.mean(np.abs(actual - ((1 - blend) * raw + blend * projection))))
            if loss < best_loss:
                best_loss, best = loss, {**params, 'blend_weight': blend}
    print(f'{label}: mean fit n={len(selected)}, MAE={best_loss:.3f}, pre-tip projections={identifiable}', flush=True)
    records = _run(rows, scoring, best, schedule, players, projections, selected, samples=200)
    actual = np.array([r['actual'] for r in records])
    means = np.array([r['mean'] for r in records])
    draws = np.array([r['samples'] for r in records])
    high = np.array([r['high'] for r in records])
    best_tail = float('inf')
    for scale in [.75, .9, 1., 1.1, 1.25, 1.4]:
        q90 = np.quantile(means[:, None] + scale * (draws - means[:, None]), .9, axis=1)
        error = actual - q90
        loss = np.maximum(.9 * error, -.1 * error)
        objective = (loss.mean() + loss[high].mean()) / 2 if high.any() else loss.mean()
        if objective < best_tail:
            best_tail = float(objective)
            best['residual_scale'] = scale
    return best, {'n': len(selected), 'mae': best_loss, 'q90_pinball': best_tail,
                  'eligible_projections': identifiable, 'blend_identifiable': bool(identifiable)}


def _fit_live_blend(records):
    """Fit only genuinely pre-tip forecasts with already-observed outcomes.

    A 30-game prior shrinks a tiny live sample toward 0.5. The unshrunk MAE
    optimum and sample size are reported separately. Never applied to the
    held-out games used here; this weight is for subsequent production runs.
    """
    eligible = [r for r in records if r['projection'] is not None]
    if not eligible:
        return SLEEPER_BLEND_PRIOR, {'n': 0, 'blend_identifiable': False,
                                    'weight': SLEEPER_BLEND_PRIOR, 'source': 'labelled prior'}
    raw = np.array([r['raw'] for r in eligible])
    projection = np.array([r['projection'] for r in eligible])
    actual = np.array([r['actual'] for r in eligible])
    grid = np.linspace(0, 1, 101)
    losses = np.abs(actual[:, None] - (raw[:, None] + grid * (projection - raw)[:, None])).mean(axis=0)
    best = float(min(zip(losses, abs(grid - SLEEPER_BLEND_PRIOR), grid))[2])
    weight = (len(eligible) * best + 30 * SLEEPER_BLEND_PRIOR) / (len(eligible) + 30)
    return weight, {'n': len(eligible), 'blend_identifiable': True, 'weight': weight,
                    'unshrunk_weight': best, 'unshrunk_mae': float(losses.min()),
                    'prior_games': 30, 'source': 'pre-tip snapshots, shrunk to labelled prior'}


def _metric_rows(records, period):
    rows = []
    for high in (False, True):
        segment = [r for r in records if not high or r['high']]
        label = '20+ prior average' if high else 'All players'
        for name, key in [('Model', 'mean'), ('Season average (point mass)', 'average'),
                          ('Sleeper archive (retrospective point mass)', 'archived')]:
            selected = [r for r in segment if r[key] is not None]
            actual = np.array([r['actual'] for r in selected])
            means = np.array([r[key] for r in selected])
            draws = np.array([r['samples'] for r in selected]) if key == 'mean' else means[:, None]
            rows.append({'period': period, 'segment': label, 'model': name, **metrics(actual, means, draws)})
    return rows


def _table(rows):
    lines = ['| Period | Players | Predictor | N | MAE | CRPS | C10 | C50 | C90 |',
             '|:--|:--|:--|--:|--:|--:|--:|--:|--:|']
    for row in rows:
        lines.append(f"| {row['period']} | {row['segment']} | {row['model']} | {row['n']} | "
                     f"{row['mae']:.2f} | {row['crps']:.2f} | {100 * row['q10']:.1f}% | "
                     f"{100 * row['q50']:.1f}% | {100 * row['q90']:.1f}% |")
    return '\n'.join(lines)


def _calibration(records):
    lines = ['| Period | Players | Threshold | Predicted probability bin | N | Predicted | Observed |',
             '|:--|:--|--:|:--|--:|--:|--:|']
    for period in ('2025', '2026'):
        for high in (False, True):
            selected = [r for r in records if r['season'] == period and (not high or r['high'])]
            for threshold in (30, 40):
                probs = np.array([np.mean(r['samples'] >= threshold) for r in selected])
                actual = np.array([r['actual'] >= threshold for r in selected])
                for low, upper in zip([0, .1, .25, .5, .75, .9], [.1, .25, .5, .75, .9, 1.01]):
                    mask = (probs >= low) & (probs < upper)
                    if not mask.any():
                        continue
                    lines.append(f"| {period} | {'20+ prior average' if high else 'All players'} | {threshold} | "
                                 f"{low:.2f}-{min(1, upper):.2f} | {mask.sum()} | "
                                 f"{probs[mask].mean():.3f} | {actual[mask].mean():.3f} |")
    return '\n'.join(lines)


def _scoring(league_id):
    """The league's own scoring_settings: --league, else the first league you manage, else the default."""
    from sleeper_gm.scoring import DEFAULT_SCORING
    try:
        if not league_id:
            from sleeper_gm import config
            from sleeper_gm.cli import discover
            found = discover(config.load())
            league_id = found[0][0] if found else None
        if league_id:
            return api.rest(f'/v1/league/{league_id}')['scoring_settings'], "your league's scoring settings"
    except Exception:
        pass
    return dict(DEFAULT_SCORING), 'the default points scoring in sleeper_gm/scoring.py'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=None, help='data cache (default: the sleeper-gm cache dir)')
    parser.add_argument('--league', default=None, help='score with this league (default: your first managed league)')
    parser.add_argument('--bundled', action='store_true', help='maintainers: replace the model_params.json shipped in the package')
    parser.add_argument('--no-sync', action='store_true', help='Use a previously synced database')
    parser.add_argument('--report-only', action='store_true', help='Write logs/backtest.md without replacing model_params.json')
    parser.add_argument('--samples', type=int, default=600)
    parser.add_argument('--max-fit-rows', type=int, default=6000)
    args = parser.parse_args(argv)
    if args.samples <= 0 or args.max_fit_rows <= 0:
        parser.error('samples and max-fit-rows must be positive')
    started = time.monotonic()
    from sleeper_gm import config
    store = Store(args.root or str(config.CACHE_DIR))
    state = api.rest('/v1/state/nba')
    season = int(state['season'])
    if not args.no_sync:
        store.sync(str(season - 1), range(1, 26))
        store.sync(str(season), range(1, int(state['week']) + 1))
    scoring, scoring_source = _scoring(args.league)
    asof_ms = int(time.time() * 1000)
    schedule = store._schedule()
    game_index = {g['game_id']: g for g in schedule}
    # One SQL query followed by a strict time-ordered prefix is equivalent to
    # store.box(before_ms=tip) for every target, with an additional result lag.
    rows = [r for r in store.box(before_ms=asof_ms)
            if game_index[r['game_id']]['status'] == 'complete'
            and r['start_ms'] + RESULT_LAG_MS <= asof_ms]
    players, projections = store.players(), _projection_index(store, scoring)
    initial_cutoff = min(g['start_ms'] for g in schedule if g['season'] == '2025' and g['week'] >= 6)
    final_cutoff = min(g['start_ms'] for g in schedule if g['season'] == '2026')
    early = [r for r in rows if r['start_ms'] + RESULT_LAG_MS <= initial_cutoff]
    initial_selected = _selected(early, args.max_fit_rows, lambda r: r['minutes'] > 0 and game_index[r['game_id']]['week'] >= 2)
    initial, initial_fit = _fit(early, scoring, schedule, players, projections, initial_selected, 'Initial 2025 weeks 2-5')
    historical = [r for r in rows if r['start_ms'] + RESULT_LAG_MS <= final_cutoff]
    final_selected = _selected(historical, args.max_fit_rows, lambda r: r['minutes'] > 0 and game_index[r['game_id']]['week'] >= 6)
    final, final_fit = _fit(historical, scoring, schedule, players, projections, final_selected, 'Production 2025 weeks 6-25')
    params = {'params': final, 'trained_before_ms': final_cutoff, 'initial_params': initial,
              'initial_trained_before_ms': initial_cutoff, 'initial_fit': initial_fit, 'fit': final_fit,
              'scoring': scoring, 'created_at': datetime.now(timezone.utc).isoformat(),
              'blend_note': 'Default 0.5 is a labelled prior: retrospective 2026 archive MAE 5.11 versus model 5.63; archive revisions may be post-tip. Only recorded pre-tip fetches can fit a weight.'}
    # Evaluation data cannot choose either parameter set: 2025 uses weeks 2-5;
    # 2026 uses 2025 only. Save after the evaluation has succeeded.
    eval_2025 = {(r['player_id'], r['game_id']) for r in historical if r['minutes'] > 0 and game_index[r['game_id']]['week'] >= 6}
    eval_2026 = {(r['player_id'], r['game_id']) for r in rows if r['minutes'] > 0 and game_index[r['game_id']]['season'] == '2026'}
    print(f'Evaluating {len(eval_2025)} 2025 and {len(eval_2026)} 2026 player-games, {args.samples} samples each', flush=True)
    records_2025 = _run(historical, scoring, initial, schedule, players, projections, eval_2025, samples=args.samples)
    records_2026 = _run(rows, scoring, final, schedule, players, projections, eval_2026, samples=args.samples)
    records = records_2025 + records_2026
    live_weight, live_fit = _fit_live_blend(records)
    production = {**final, 'blend_weight': live_weight}
    params.update(params=production, trained_before_ms=asof_ms,
                  evaluation_params=final, evaluation_trained_before_ms=final_cutoff,
                  fit={**final_fit, 'eligible_projections': live_fit['n'],
                       'blend_identifiable': live_fit['blend_identifiable']}, live_blend_fit=live_fit)
    print(f"Live blend: n={live_fit['n']}, w={live_weight:.3f} ({live_fit['source']})", flush=True)
    table = _table(_metric_rows(records_2025, '2025 W6-25') + _metric_rows(records_2026, '2026 finished'))
    elapsed = time.monotonic() - started
    archived_n = sum(r['archived'] is not None for r in records)
    eligible_n = sum(r['projection'] is not None for r in records)
    text = f'''# Conditional scoring backtest

Generated {datetime.now(timezone.utc).isoformat()}. Runtime {elapsed:.1f} seconds.
Scoring: {scoring_source}. {len(records)} evaluated player-games;
all positive-minute finished games from 2025 week 6 onward and finished 2026 games
whose tips are at least six hours before the run cutoff.
No evaluation sampling. Parameter fitting used {initial_fit['n']} early-season and
{final_fit['n']} full-season rows, selected deterministically by player/game hash if capped.

{table}

C10/C50/C90 are fractions of actual scores at or below the predicted quantile;
targets are 10/50/90 percent. C90 is the headline upper-tail calibration measure.
20+ means the player's strictly pre-game, current-season fantasy-point average.
Season-average baselines omit season debuts. All distributions condition on playing;
zero-minute DNP rows are excluded from conditional scoring metrics but retained as
participation evidence. Zero and negative fantasy scores with minutes remain.

Baselines are point masses, so their CRPS equals MAE and their three quantiles coincide.
Sleeper archive is a retrospective diagnostic, not a leakage-free competitor:
{archived_n} evaluation rows have archive projections, but only {eligible_n} have positive-minute
or minutes-unspecified forecasts actually fetched before tip. Vendor timestamps on later
archive fetches do not qualify. Every pre-tip fetch is retained in projection_snapshots.
SLEEPER_BLEND_PRIOR=0.5 is a labelled prior: the previous 2026 retrospective archive
MAE was 5.11 versus model 5.63, but the archive may contain post-tip revisions.
The current held-out comparison is conditional on that prior choice, not an independent
validation of it. Zero-minute forecasts never replace the conditional scoring mean.
Only explicit zero-points, zero-minutes rows contribute one nonparticipation observation.
The model's own residual spread is retained around the blended mean.

Production blend fit: n={live_fit['n']} completed player-games with pre-tip forecasts,
w={live_weight:.4f}. This fit is for future games only. With data, the MAE-optimal weight
is shrunk toward 0.5 with 30 games of prior strength; without data it remains the prior.

## Chronology and limits

2025 evaluation uses parameters chosen using weeks 2-5 only; week 1 warms history.
2026 evaluation uses parameters fitted on 2025 weeks 6-25, with weeks 1-5 warming history.
No 2026 outcome selects the parameters used to evaluate 2026. The separate production
blend fit can use completed 2026 outcomes after evaluation. Mean parameters minimize training MAE; spread scale
minimizes mean 90th-quantile pinball loss, equally weighting all players and prior-20+ players.
The grid is rate half-life [3,10,30] games, minutes half-life [1.5,4,10] games,
blend [0,.25,.5,.75,1] only if identifiable (otherwise prior 0.5), residual scale [.75,.9,1,1.1,1.25,1.4].
The remaining shrinkage strengths and return-minute haircut are structural priors, not fitted.

The runner streams the same strict history prefix as Store.box(before_ms=tip).
Equal-tip rows never see one another. Results enter history six hours after tip because
historical completion timestamps are unavailable. This conservative lag excludes overlapping
NBA games in normal scheduling; games lasting more than six hours cannot be ruled out.
Residuals are actual/prediction from predictions made before each historical result.
The model uses this same replay and sampling implementation. Current player positions are
used for positional priors because historical position snapshots are unavailable.
Historical injury probabilities are not tested. Live status rates use the external
2026 official-report sample; ESPN return-date adjustments are labelled priors. Current-season metadata and historical archive timestamps
cannot establish that every upstream correction was originally published before tip.

Participation uses completed team games since the observed team switch (or opening
night without switch evidence), counting zero-minute and missing rows as misses.
An already-observed return after 3+ consecutive DNPs identifies an injury episode;
those DNPs leave the healthy participation denominator. Future returns are never used.
Current injuries are used only within five minutes of the live clock, never in an
historical prediction. DNPs on/after the ESPN report date and the current trailing
DNP streak are excluded for a currently listed/tagged player. ESPN's date is a report
update rather than reliable onset, so extending the exclusion backward through the
ongoing streak is a labelled injury-episode prior. This also covers a Sleeper tag
without a matched ESPN record. Historical injury/scratch labels are unavailable;
the 3-game rule can misclassify a resolved bench spell as an injury.

A last-season healthy participation rate, else 0.6, starts at three pseudo-games;
current observations and prior decay with a three-game half-life. Four straight
appearances reset an obsolete low prior to 1. The player's resulting rate is shrunk
9:1 toward 0.91 after a healthy appearance and 3:1 toward 0.24 after a healthy DNP.
Each additional consecutive healthy scratch multiplies the latter by 0.7.
The .91/.24 centers come from the user-supplied 2025-26 Sleeper league measurements
([sleeper-lineup](https://github.com/ckloote/sleeper-lineup)); these shrinkage strengths
and streak decay are labelled priors, not fitted or validated by conditional metrics.
Current metadata's team_changed_at is respected only when known by the cutoff.

Status multipliers live in one labelled PLAY_PRIORS dictionary. Official-report rates
are Probable .866, Questionable .483, Doubtful .042, from 2,007 designations Jan 18-Oct 22
2026 ([source](https://isheplayingtonight.com/nba-injuries/questionable-play-rate)).
[Sleeper tag meanings](https://support.sleeper.com/en/articles/4714981-basketball-injury-designations)
anchor GTD .5 and DTD/D2D .3 next game, .75 later scheduled games. The latter two rates
are judgement priors. Out is 0 without a date or before the ESPN return date, .45
on/after that date (prior). IR and Sus are 0. Healthy status multiplies by 1.
A 30+ current-season minutes/game player receives a .95 multiplier on the second
night of a back-to-back. This is a prior motivated by the user-cited ESPN 2016 finding
that 74% of rest DNPs occurred on that night, not a measured conditional risk ratio.

ROS is healthy per-game talent/role times healthy participation; no current injury
probability, return haircut, back-to-back penalty, or single-game projection enters it.
availability_weeks separately counts the current Monday-Sunday week and the next h-1
weeks in America/New_York; return by a week's Sunday counts that week as available.
The partial return week counts once because Lock-In needs one game. This is a
return-date assumption, not a schedule-based guarantee of a playable game. An
undated Out/IR/Sus/DNR uses .5 (labelled prior), healthy or short-term status without
a date uses 1. Historical calls without injury snapshots use the neutral healthy prior.

Return-minute priors last five appearances after 3+ missed team games, fading through
.80/.84/.88/.92/.96 before 1.00. After 14+ missed games, a first-game 25-minute cap
also fades over those five appearances. A new absence resets the taper. The literature
motivates a ramp, not these exact weights: [knee/ankle study](https://pmc.ncbi.nlm.nih.gov/articles/PMC11126319/)
reports a 10.9% game-score decline over 20 return games, and PNAS Nexus pgac176 reports
minutes falling from 25.8 to 21.6 after 14+ games missed. These are not fitted values.
Current-role minutes dominate after two appearances; old-team minutes do not set the prior.
Report-only run: {args.report_only}. Existing model_params.json was {'preserved' if args.report_only else 'replaced'};
the evaluation parameter sets below were used for this run.

## Chosen parameters

Initial evaluation parameters:
```json
{json.dumps(initial, indent=2)}
```

2026 evaluation parameters:
```json
{json.dumps(final, indent=2)}
```

Production parameters (including the separate completed-snapshot blend fit):
```json
{json.dumps(production, indent=2)}
```

Training diagnostics:
```json
{json.dumps({'initial': initial_fit, '2026_evaluation': final_fit, 'live_blend': live_fit}, indent=2)}
```

## Threshold calibration

{_calibration(records)}
'''
    from sleeper_gm import config
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = config.STATE_DIR / 'backtest.md'
    PARAMS_PATH = BUNDLED_PARAMS if args.bundled else config.STATE_DIR / 'model_params.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if not args.report_only:
        PARAMS_PATH.write_text(json.dumps(params, indent=2) + '\n')
    print(table, flush=True)
    destination = str(path) if args.report_only else f'{path} and {PARAMS_PATH}'
    print(f'Wrote {destination}; runtime {elapsed:.1f}s', flush=True)


if __name__ == '__main__':
    main()
