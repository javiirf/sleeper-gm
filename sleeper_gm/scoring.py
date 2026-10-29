"""A stat line scored with the league's own scoring_settings, the way Sleeper does it."""


def fantasy_points(stats, scoring):
    if not stats:
        return 0.0
    return round(sum(w * (stats.get(k) or 0) for k, w in scoring.items()), 2)


# A common Sleeper NBA points setup, used only when no league is available (e.g. the backtest
# before login). Real runs always score with the league's own scoring_settings.
DEFAULT_SCORING = {"pts": 0.5, "reb": 1.0, "ast": 1.0, "stl": 2.0, "blk": 2.0, "to": -1.0, "tpm": 0.5,
                   "dd": 1.0, "td": 2.0, "bonus_pt_40p": 2.0, "bonus_pt_50p": 2.0, "tf": -2.0, "ff": -2.0}
