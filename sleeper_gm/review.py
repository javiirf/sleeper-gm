"""Review of a finished week: points vs Sleeper's hindsight max, the result, and how well the
win chances we acted on were calibrated. `review.jsonl` in the league's state dir is the season
record the phone app and the weekly brief read; only finished weeks are written, once each.
CLI: `sleeper-gm review [WEEK]`.
"""
import glob
import json
import logging

from . import api, config


def predictions(league_id, week):
    out = []
    for f in sorted(glob.glob(str(config.league_state(league_id) / "logs" / "decisions-*.jsonl"))):
        for line in open(f):
            r = json.loads(line)
            if r.get("week") == week and r.get("p_win") is not None:
                out.append(r)
    return out


def recorded(league_id):
    try:
        with open(config.league_state(league_id) / "review.jsonl") as f:
            return {json.loads(l)["week"] for l in f if l.strip()}
    except FileNotFoundError:
        return set()


def record_review(league_id, row):
    """Record a finished review after delivery, once per week."""
    if row["week"] not in recorded(league_id):
        with open(config.league_state(league_id) / "review.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")


def review(league_id, roster_id, week, persist=False):
    """(text, row) for `week`. Writes it to the season record only when persist=True and the
    week is over (it is before Sleeper's current week) and it is not already recorded."""
    legs = {l["roster_id"]: l for l in api.legs(league_id, week)}
    me = legs.get(roster_id)
    if me is None:
        raise ValueError("our roster has no matchup leg")
    opp = next((l for r, l in legs.items() if me.get("matchup_id") is not None
                and l.get("matchup_id") == me["matchup_id"] and r != roster_id), None)
    if opp is None:
        raise ValueError("our roster has no opponent")
    pts, opp_pts, mx = me.get("points") or 0, opp.get("points") or 0, me.get("max_points") or 0
    won = 1.0 if pts > opp_pts else 0.5 if pts == opp_pts else 0.0
    preds = predictions(league_id, week)
    first = preds[0]["p_win"] if preds else None
    brier = sum((r["p_win"] - won) ** 2 for r in preds) / len(preds) if preds else None
    league_eff = sorted(((l.get("points") or 0) / l["max_points"] for l in legs.values() if l.get("max_points")), reverse=True)
    my_eff = pts / mx if mx else None
    rank = 1 + sum(1 for e in league_eff if my_eff is not None and e > my_eff)
    row = {"week": week, "points": pts, "opp_points": opp_pts, "max_points": mx, "won": won,
           "efficiency": my_eff, "efficiency_rank": rank, "teams": len(league_eff),
           "p_win_first": first, "brier": brier, "runs": len(preds),
           "locks": len(me.get("starters_games") or {})}
    finished = int(week) < int(api.rest("/v1/state/nba", fresh=True)["leg"])
    if persist and finished:
        record_review(league_id, row)
    res = "WON" if won == 1 else "TIED" if won == 0.5 else "LOST"
    if not finished:
        res = "IN PROGRESS"
    text = [f"Sleeper GM, week {week} review: {res} {pts:.1f} to {opp_pts:.1f}"]
    if mx:
        text.append(f"- used {my_eff:.0%} of the {mx:.1f} hindsight max ({rank} of {len(league_eff)} in the league at that)")
    if first is not None:
        text.append(f"- first call of the week was {first:.0%} to win; {len(preds)} runs, Brier {brier:.3f}")
    return "\n".join(text), row


def backfill(league_id, roster_id):
    """Return (week, text, row) for unrecorded finished weeks, without persisting.

    Missing matchups are skipped reviews; transient failures are retried next run.
    """
    cur = int(api.rest("/v1/state/nba", fresh=True)["leg"])
    league = api.rest(f"/v1/league/{league_id}")
    start = int((league.get("settings") or {}).get("start_week") or 1)
    have = recorded(league_id)
    out = []
    for w in range(start, cur):
        if w in have:
            continue
        try:
            text, row = review(league_id, roster_id, w)
        except ValueError as e:
            reason = api.redact(str(e))
            logging.getLogger(__name__).warning("Skipping league %s week %s review: %s", league_id, w, reason)
            row = {"week": w, "skipped": True, "reason": reason}
            text = f"Sleeper GM, week {w} review: skipped ({reason})"
        except Exception as e:
            logging.getLogger(__name__).warning("Retrying league %s week %s review next run: %s", league_id, w, api.redact(str(e)))
            continue
        out.append((w, text, row))
    return out
