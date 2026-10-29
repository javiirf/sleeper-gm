"""Waiver planning and durable ownership. Only api.Writer can send a claim mutation."""
from __future__ import annotations

import json
import statistics
import time
import uuid
from datetime import datetime, timedelta

from . import api, moves, week
from .policy import decide, make_plan

DEFAULT_PROCESS_SECOND = 3 * 3600 + 10 * 60
FREEZE_MS = 15 * 60 * 1000
WAIVER_FAILURE_LIMIT = 3
# Spending first priority requires six extra P(win) points (15 ROS points at KAPPA).
# Linearly reduce that opportunity cost to zero at last priority. This is a heuristic.
FIRST_PRIORITY_EDGE = 0.06
REPLACE_MARGIN = 0.01  # require a full P(win) point over a still-worthwhile pending claim


def read_state(directory):
    try:
        return json.loads((directory / "claims.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(directory, state):
    path = directory / "claims.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(api.redact(json.dumps(state)))
    tmp.replace(path)


def processing_bounds(now_ms, state):
    """Previous and next estimated daily run, in ET, including DST transitions.

    This estimates when claims process, never when any individual player clears waivers.
    """
    now = datetime.fromtimestamp(now_ms / 1000, week.ET)
    seconds = int(state.get("process_second", DEFAULT_PROCESS_SECOND))
    today = now.replace(hour=seconds // 3600, minute=seconds % 3600 // 60,
                        second=seconds % 60, microsecond=0)
    previous = today if today.timestamp() * 1000 <= now_ms else today - timedelta(days=1)
    following = previous + timedelta(days=1)
    return int(previous.timestamp() * 1000), int(following.timestamp() * 1000)


def learn_time(state, rows):
    times = []
    for row in rows:
        stamp = row.get("status_updated")
        if row.get("type") != "waiver" or row.get("status") not in ("complete", "failed") or not stamp:
            continue
        dt = datetime.fromtimestamp(float(stamp) / 1000, week.ET)
        times.append(dt.hour * 3600 + dt.minute * 60 + dt.second)
    if times:
        state["process_second"] = int(statistics.median(times))


def fa_result(directory, success=False, waiver_failure=False):
    state = read_state(directory)
    if success:
        state["fa_waiver_failures"] = 0
    elif waiver_failure:
        state["fa_waiver_failures"] = state.get("fa_waiver_failures", 0) + 1
    save_state(directory, state)


def fa_disabled(directory):
    return read_state(directory).get("fa_waiver_failures", 0) >= WAIVER_FAILURE_LIMIT


def owned_players(row, key, rid):
    return [p for p, r in (row.get(key) or {}).items() if str(r) == str(rid)]


def seq(row):
    return int((row.get("settings") or {}).get("seq") or 0)


def order(row):
    settings = row.get("settings") or {}
    # The web app sorts by bid, explicit priority, then id. seq is the fallback.
    return (-int(settings.get("waiver_bid") or 0), int(settings.get("priority", seq(row))),
            seq(row), str(row.get("transaction_id", "")))


def priority_cost(wk, roster):
    if wk.league["settings"].get("waiver_type", 0) != 0:
        return 0.0
    count = len(wk.rosters)
    position = int((roster.get("settings") or {}).get("waiver_position") or 1)
    return FIRST_PRIORITY_EDGE * max(0, count - position) / max(1, count - 1)


def plan(wk, me, opp, roster, owner, process_ms, limit, existing=()):
    """Value alternatives after the owner's claims, conservatively assuming those succeed."""
    if limit <= 0 or not wk.days or wk.league["settings"].get("disable_adds"):
        return []
    if wk.league["settings"].get("waiver_type") == 2 and wk.league["settings"].get("waiver_bid_min", 0) > 0:
        return []  # no evidence-based paid bid policy; never silently spend FAAB
    active = set(roster.get("players") or []) - set(roster.get("reserve") or [])
    max_active, _ = moves.roster_limits(wk.league)
    reserved_drops = {p for r in owner for p in owned_players(r, "drops", wk.my_rid)}
    reserved_adds = {p for r in owner for p in owned_players(r, "adds", wk.my_rid)}
    team = me
    projected = set(roster.get("players") or [])
    for row in sorted(owner, key=order):
        drops = owned_players(row, "drops", wk.my_rid)
        adds = owned_players(row, "adds", wk.my_rid)
        if any(p not in projected for p in drops) or any(p in projected for p in adds):
            continue  # an alternative whose required drop is already gone cannot succeed
        if len((active - set(drops)) | set(adds)) > max_active:
            continue  # a roster-full owner claim fails instead of consuming another vacancy
        for a in adds:
            d = drops[0] if drops else None
            team = moves.with_player(wk, team, a, d, after_ms=process_ms)
        projected.difference_update(drops)
        projected.update(adds)
        active.difference_update(drops)
        active.update(adds)
    droppable = sorted((p for p in active if p in me.positions and p not in reserved_drops
                        and not moves.untouchable(wk, me, p)), key=lambda p: moves.keep_value(wk, p))
    drops = [None] if len(active) < max_active else droppable[:3]
    candidates = [p for p in moves.free_agents(wk, limit=None) if p not in reserved_adds]
    D = len(wk.days)
    screened = sorted(candidates[:60], key=lambda p: make_plan(
        moves.with_player(wk, team, p, after_ms=process_ms), D).value, reverse=True)
    shortlist = list(dict.fromkeys(screened[:8] + candidates[:4]))
    base = decide(team, opp, D, n=4000).p_win
    cost = priority_cost(wk, roster)
    scored = {}

    def evaluate(a, d):
        key = (a, d)
        if key in scored:
            return scored[key]
        if a not in candidates or (d is not None and d not in droppable) or (d is None and len(active) >= max_active):
            return None
        p = decide(moves.with_player(wk, team, a, d, after_ms=process_ms), opp, D, n=4000).p_win
        score = p - base + moves.KAPPA * (moves.keep_value(wk, a) - (moves.keep_value(wk, d) if d else 0))
        threshold = (moves.SWAP_EDGE if d else 0) + cost
        if score <= 0 or score < threshold:
            scored[key] = None
        else:
            why = (f"{wk.name(a)}" + (f" for {wk.name(d)}" if d else ", open roster spot")
                   + f", week win {base:.0%}->{p:.0%}; edge {score:.1%}, required {threshold:.1%} after waivers")
            scored[key] = {"add": a, "drop": d, "score": score, "why": why}
        return scored[key]

    # Revalue each incumbent's actual add/drop even outside the scoring shortlist.
    # Preserve its place unless it is invalid, below its hurdle, or clearly outscored.
    selected = []
    vacancies = max(0, max_active - len(active))

    def fits(candidate, others):
        return (candidate["add"] not in {m["add"] for m in others}
                and (candidate["drop"] is not None or sum(m["drop"] is None for m in others) < vacancies))

    for row in sorted(existing, key=order):
        adds, cuts = owned_players(row, "adds", wk.my_rid), owned_players(row, "drops", wk.my_rid)
        if len(adds) != 1 or len(cuts) > 1:
            continue
        current = evaluate(adds[0], cuts[0] if cuts else None)
        if current and len(selected) < limit and fits(current, selected):
            selected.append(current)
    ranked = []
    for a in shortlist:
        options = [m for d in drops if (m := evaluate(a, d)) is not None]
        if options:
            ranked.append(max(options, key=lambda m: m["score"]))
    for candidate in sorted(ranked, key=lambda m: (-m["score"], m["add"])):
        if candidate in selected:
            continue
        if len(selected) < limit and fits(candidate, selected):
            selected.append(candidate)
            continue
        replaceable = [i for i, current in enumerate(selected)
                       if candidate["score"] - current["score"] >= REPLACE_MARGIN - 1e-12
                       and fits(candidate, selected[:i] + selected[i + 1:])]
        if replaceable:
            i = min(replaceable, key=lambda i: selected[i]["score"])
            selected[i] = candidate
    return selected


def pending(ctx):
    # Omit leg to include claims surviving the Sunday/Monday rollover.
    return [r for r in api.league_transactions(ctx.league_id, status="pending", roster_id=ctx.rid) or []
            if r.get("type") in (None, "waiver")]


def identity(row):
    return tuple(tuple(sorted((str(p), str(r)) for p, r in (row.get(k) or {}).items()))
                 for k in ("adds", "drops"))


def request_identity(record, rid):
    return identity({"adds": {record["add"]: rid},
                     "drops": {record["drop"]: rid} if record.get("drop") else {}})


def match_intent(intent, rows, rid, history=()):
    tid = intent.get("transaction_id")
    if tid:
        return next((r for r in [*rows, *history] if str(r.get("transaction_id")) == tid), None)
    if "pending_before" not in intent:
        return None  # older intents without a baseline cannot prove ownership by identity
    before = set(intent["pending_before"])
    matches = [r for r in rows if r.get("transaction_id") and str(r["transaction_id"]) not in before
               and identity(r) == request_identity(intent, rid)]
    return matches[0] if len(matches) == 1 else None


def resolve_intents(state, rows, rid, history=()):
    intents = state.setdefault("intents", {})
    for local_id, intent in list(intents.items()):
        got = match_intent(intent, rows, rid, history)
        if got is not None:
            tid = str(got["transaction_id"])
            state.setdefault("bot", {})[tid] = {**intent, "leg": got.get("leg", intent["leg"])}
            intents.pop(local_id)


def result_text(wk, row, rid):
    add = ", ".join(wk.name(p) for p in owned_players(row, "adds", rid))
    drop = ", ".join(wk.name(p) for p in owned_players(row, "drops", rid))
    notes = str((row.get("metadata") or {}).get("notes") or "")
    if row.get("status") == "complete":
        return f"Got {add}" + (f" (dropped {drop})" if drop else "")
    if "another" in notes.lower():
        return f"Lost {add} to another team"
    if "too many players" in notes.lower() or "roster full" in notes.lower():
        return f"Failed: roster full ({add})"
    return f"Failed: {add} ({notes or 'waiver claim refused'})"


def observe(ctx, wk):
    """Learn the clock and reconcile results from authenticated reads, once per full run."""
    state = read_state(ctx.dir)
    tracked = state.setdefault("bot", {})
    watched = state.setdefault("watched", {})
    rows = list(ctx.pending_claims)
    legs = {wk.week, max(1, wk.week - 1)} | {int(r["leg"]) for r in [*tracked.values(), *watched.values()]
                                              if r.get("leg") and not r.get("reported")}
    history = []
    for leg in sorted(legs):
        history.extend(api.league_transactions(ctx.league_id, leg) or [])
    learn_time(state, history)
    # A human free-agent add is evidence that free agency works again too.
    successes = [int(r.get("status_updated") or r.get("created") or 0) for r in history
                 if r.get("type") == "free_agent" and r.get("status") == "complete"
                 and owned_players(r, "adds", ctx.rid)]
    if successes and max(successes) > state.get("last_fa_success", 0):
        state["last_fa_success"], state["fa_waiver_failures"] = max(successes), 0
    resolve_intents(state, rows, ctx.rid, history)
    for row in rows:
        tid = str(row.get("transaction_id") or "")
        if tid:
            watched.setdefault(tid, {"leg": row.get("leg", wk.week)})
    results = []
    for row in history:
        tid = str(row.get("transaction_id"))
        record = watched.get(tid) or tracked.get(tid)
        if record is None or record.get("reported") or row.get("status") not in ("complete", "failed", "cancelled"):
            continue
        for records in (tracked, watched):
            if tid in records:
                records[tid]["reported"] = True
        if row["status"] != "cancelled":
            message = result_text(wk, row, ctx.rid)
            ctx.acted.append(message)
            results.append({"transaction_id": tid, "message": message, "status": row["status"],
                            "t": row.get("status_updated")})
    if results:
        state["results"] = results
    save_state(ctx.dir, state)
    return state


def view(ctx, wk):
    state = read_state(ctx.dir)
    bot, owner = [], []
    for row in sorted(getattr(ctx, "pending_claims", []), key=order):
        tid = str(row.get("transaction_id"))
        entry = {"add": ", ".join(wk.name(p) for p in owned_players(row, "adds", ctx.rid)),
                 "drop": ", ".join(wk.name(p) for p in owned_players(row, "drops", ctx.rid)) or None}
        if tid in state.get("bot", {}):
            entry.update(seq=seq(row), why=state["bot"][tid].get("why", ""))
            bot.append(entry)
        else:
            owner.append(entry)
    return {"bot": bot, "owner": owner, "results": state.get("results", []),
            "next_processing_ms": processing_bounds(wk.now_ms, state)[1],
            "available": getattr(ctx, "claims_available", False)}


def budget_available(ctx, state):
    if ctx.claim_writes_today() < ctx.cfg.get("max_claim_writes_per_day", 6):
        return True
    today = datetime.fromtimestamp(time.time(), week.ET).strftime("%Y-%m-%d")
    if state.get("budget_reported_day") != today:
        ctx.acted.append("claim write budget used")
        state["budget_reported_day"] = today
        save_state(ctx.dir, state)
    return False


def reconcile(ctx, wk, me, opp):
    """Revalue bot claims; change only with a clear edge, budget and pending read-back."""
    state = read_state(ctx.dir)
    process_ms = processing_bounds(int(time.time() * 1000), state)[1]
    if process_ms - int(time.time() * 1000) <= FREEZE_MS:
        return
    bot = state.setdefault("bot", {})
    owner = [r for r in ctx.pending_claims if str(r.get("transaction_id")) not in bot]
    existing = [r for r in ctx.pending_claims if str(r.get("transaction_id")) in bot]
    wanted = plan(wk, me, opp, wk.rosters[ctx.rid], owner, process_ms, ctx.cfg.get("max_claims", 3), existing)
    if not ctx.live:
        ctx.acted.extend("Would claim: " + m["why"] for m in wanted)
        return
    if not budget_available(ctx, state):
        return
    if state.get("intents"):
        ctx.acted.append("NEEDS YOU: a claim submission is unconfirmed; no further claims sent")
        return

    def allowed():
        # Keep the original target if slow planning or reads cross the freeze boundary.
        return int(time.time() * 1000) < process_ms - FREEZE_MS and budget_available(ctx, state)

    desired = {request_identity(m, ctx.rid) for m in wanted}
    for row in list(ctx.pending_claims):
        tid = str(row.get("transaction_id"))
        if tid not in bot or identity(row) in desired:
            continue
        if not allowed():
            return
        try:
            ctx.writer.cancel_claim(ctx.league_id, row.get("leg", wk.week), tid, summary="replace stale bot claim")
        except api.SleeperError as e:
            ctx.acted.append(f"Claim cancellation uncertain ({e}); checking pending claims")
        ctx.pending_claims = pending(ctx)
        budget_available(ctx, state)
        if any(str(r.get("transaction_id")) == tid for r in ctx.pending_claims):
            return
        ctx.acted.append("Stale bot claim no longer pending")
    bid = 0 if wk.league["settings"].get("waiver_type") == 2 else None
    for i, m in enumerate(wanted):
        row = next((r for r in ctx.pending_claims if identity(r) == request_identity(m, ctx.rid)), None)
        if row is None:
            if not allowed():
                return
            if len([r for r in ctx.pending_claims if str(r.get("transaction_id")) in bot]) >= ctx.cfg.get("max_claims", 3):
                return
            # A fresh baseline immediately before submit is essential for timeout ownership.
            before = pending(ctx)
            if before != ctx.pending_claims:
                ctx.pending_claims = before
                ctx.acted.append("Pending claims changed; leaving the queue for the next plan")
                return
            if not allowed():
                return
            local_id = uuid.uuid4().hex  # local only; never sent to Sleeper
            record = {**m, "leg": wk.week, "pending_before": [str(r["transaction_id"]) for r in before]}
            state.setdefault("intents", {})[local_id] = record
            save_state(ctx.dir, state)
            response = None
            try:
                response = ctx.writer.submit_claim(ctx.league_id, ctx.rid, m["add"], m["drop"], summary=m["why"], bid=bid)
            except api.SleeperError as e:
                ctx.acted.append(f"Claim submission uncertain ({e}); checking pending claims")
            tid = str(((response or {}).get("submit_waiver_claim") or {}).get("transaction_id") or "")
            if tid:
                record["transaction_id"] = tid
                bot[tid] = dict(record)
                # Keep the intent until pending read-back succeeds, even if that read raises.
                save_state(ctx.dir, state)
            ctx.pending_claims = pending(ctx)
            row = match_intent(record, ctx.pending_claims, ctx.rid)
            if row is None:
                ctx.acted.append("Claim not confirmed in pending list; no further claims sent")
                budget_available(ctx, state)
                return
            tid = str(row["transaction_id"])
            bot[tid] = {**record, "leg": row.get("leg", wk.week)}
            state["intents"].pop(local_id, None)
            state.setdefault("watched", {})[tid] = {"leg": row.get("leg", wk.week)}
            save_state(ctx.dir, state)
            ctx.acted.append("Claim placed: " + m["why"])
            budget_available(ctx, state)
            if identity(row) != request_identity(m, ctx.rid) or (bid is not None and (row.get("settings") or {}).get("waiver_bid") != bid):
                ctx.acted.append("Claim read-back differs from the plan; no further claims sent this run")
                return
        tid = str(row.get("transaction_id"))
        if tid not in bot:
            continue
        bot[tid]["why"], bot[tid]["score"] = m["why"], m["score"]
        save_state(ctx.dir, state)
        priorities = [int(r["settings"]["priority"]) for r in ctx.pending_claims
                      if str(r.get("transaction_id")) not in bot
                      and (r.get("settings") or {}).get("priority") is not None]
        if not priorities:
            continue  # the server's new seq puts this claim after the existing queue
        priority = max(priorities) + 1 + i
        settings = row.get("settings") or {}
        if settings.get("priority") == priority:
            continue
        if not allowed():
            return
        sent_settings = {k: v for k, v in settings.items() if k != "seq"}
        sent_settings["priority"] = priority
        try:
            ctx.writer.update_claim(ctx.league_id, row.get("leg", wk.week), tid, priority,
                                    summary=m["why"], settings=settings)
        except api.SleeperError as e:
            ctx.acted.append(f"Claim update uncertain ({e}); checking pending claims")
        ctx.pending_claims = pending(ctx)
        budget_available(ctx, state)
        got = next((r for r in ctx.pending_claims if str(r.get("transaction_id")) == tid), None)
        if not got or any((got.get("settings") or {}).get(k) != v for k, v in sent_settings.items()):
            ctx.acted.append("Claim update not confirmed; leaving the remaining queue unchanged")
            return
