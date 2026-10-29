"""sleeper-gm: an automated manager for Sleeper fantasy basketball (Lock-In mode).

  sleeper-gm login [--from-browser [--cdp URL]]   save your Sleeper session token (0600)
  sleeper-gm leagues                              the leagues it would manage
  sleeper-gm status [--league ID]                 read-only: win chance, every slot, what it would do
  sleeper-gm run [--live] [--force] [--brief]     one cycle for every managed league
  sleeper-gm on | off                             allow / forbid writes (default off)
  sleeper-gm schedule install|remove|status       run by itself every 30 minutes
  sleeper-gm web [--port 8770] [--host 127.0.0.1] the phone/web app
  sleeper-gm mcp                                  MCP server (stdio) for Claude and other agents
  sleeper-gm review [WEEK] [--league ID]          a finished week vs Sleeper's hindsight max
  sleeper-gm backtest                             walk-forward test of the projection model
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import traceback
from datetime import datetime

from . import api, auth, claims, config, directives, notify, week

DUE_AHEAD_MS = 3 * 3600 * 1000


def store():
    from .data import Store
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return Store(str(config.CACHE_DIR))


def ensure_history(st, season):
    """Bootstrap last season once, then refresh only the current week, at most every 20 minutes.
    Failed refreshes fall back to cached data and do not advance the successful-sync clock."""
    try:
        rs, path = _run_state()
        if rs.get("last_sync") is not None and 0 <= time.time() - rs["last_sync"] < 20 * 60:
            return None
        prev = str(int(season) - 1)
        if st.db.execute("SELECT 1 FROM games WHERE season=? LIMIT 1", (prev,)).fetchone() is None:
            st.sync(prev, range(1, 26))
        cur = int(api.rest("/v1/state/nba", fresh=True)["leg"])
        st.sync(season, [cur])
        rs, path = _run_state()
        rs["last_sync"] = time.time()
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rs))
        return None
    except Exception as e:
        return f"data refresh failed, using cached data: {api.redact(repr(e))}"


def discover(cfg, verbose=False):
    """Every in-season Lock-In NBA league the token's user is on, with their roster. A league that
    fails to load is reported and skipped; it never stops the others."""
    uid = str(auth.claims()["user_id"])
    state = api.rest("/v1/state/nba")
    season = state.get("league_season") or state["season"]
    out = []
    for L in api.rest(f"/v1/user/{uid}/leagues/nba/{season}") or []:
        lid = L["league_id"]
        if cfg["leagues"] != "auto" and lid not in [str(x) for x in cfg["leagues"]]:
            continue
        why = None
        if L.get("status") not in ("in_season", "post_season"):
            why = f"status {L.get('status')}"
        elif (L.get("settings") or {}).get("game_mode") != 1:
            why = "not Lock-In mode (only Lock-In is supported)"
        r = None
        if not why:
            try:
                rosters = api.rest(f"/v1/league/{lid}/rosters") or []
                r = next((r for r in rosters if str(r.get("owner_id")) == uid or uid in [str(c) for c in r.get("co_owners") or []]), None)
                why = None if r else "no roster of yours"
            except api.SleeperError as e:
                why = f"could not load its rosters ({e})"
        if verbose:
            print(f"{lid}  {L.get('name')}: " + (f"skipped ({why})" if why else f"managed (roster {r['roster_id']})"))
        if not why:
            out.append((lid, r["roster_id"], L.get("name")))
    return out


def _run_state():
    p = config.STATE_DIR / "run.json"
    try:
        return json.loads(p.read_text()), p
    except (FileNotFoundError, ValueError):
        return {}, p


def _today():
    return datetime.now(week.ET).strftime("%Y-%m-%d")


def brief_due(cfg, league_id, rs=None):
    rs = rs if rs is not None else _run_state()[0]
    hh, mm = (int(x) for x in cfg["brief_at_et"].split(":"))
    now_et = datetime.now(week.ET)
    return (rs.get("brief") or {}).get(str(league_id)) != _today() and (now_et.hour, now_et.minute) >= (hh, mm)


def due(cfg, now_ms):
    """Schedule check plus one public roster read per known league; no model or history."""
    rs, _ = _run_state()
    hh, mm = (int(x) for x in cfg["brief_at_et"].split(":"))
    now_et = datetime.now(week.ET)
    missing_brief = (any((rs.get("brief") or {}).get(str(lid)) != _today() for lid in rs["leagues"])
                     if "leagues" in rs else rs.get("brief_date") != _today())
    if (now_et.hour, now_et.minute) >= (hh, mm) and missing_brief:
        return True, True
    rosters = rs.get("rosters") or {}
    known = {str(lid) for lid in rs.get("leagues") or []} | set(rosters)
    for lid in known:
        saved = rosters.get(lid, {})
        if directives.fingerprint(config.STATE_DIR / lid) != saved.get("directives", ""):
            return True, False
    for lid, saved in rosters.items():
        state = claims.read_state(config.STATE_DIR / lid)
        previous, following = claims.processing_bounds(now_ms, state)
        last = saved.get("full_ms", 0)
        # Leave ten minutes to plan before the freeze, rounding down to a :10/:40 tick.
        cutoff = following - claims.FREEZE_MS - 10 * 60_000
        final_tick = ((cutoff - 10 * 60_000) // (30 * 60_000)) * (30 * 60_000) + 10 * 60_000
        if last < previous <= now_ms or last < final_tick <= now_ms < following:
            return True, False
        try:
            roster = next((r for r in api.rest(f"/v1/league/{lid}/rosters", fresh=True) or []
                           if str(r.get("roster_id")) == str(saved["rid"])), None)
        except api.SleeperError:
            return True, False  # let the full cycle report a failed refresh
        if roster is None or roster_hash(roster) != saved.get("hash"):
            return True, False
    state = api.rest("/v1/state/nba", fresh=True)
    last = rs.get("last_full_ms", 0)
    for g in api.scores(state["season"], int(state["leg"])):
        start = int(g["start_time"])
        st = g.get("status")
        if st == "in_game" or (st == "pre_game" and start - now_ms < DUE_AHEAD_MS) or (st == "complete" and start > last - 4 * 3600 * 1000):
            return True, False
    return False, False


def roster_hash(roster):
    fields = {"players": sorted(roster.get("players") or []),
              "reserve": sorted(roster.get("reserve") or []),
              "starters": list(roster.get("starters") or [])}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def cmd_run(args):
    cfg = config.load()
    live = bool(args.live and cfg["live"])
    now_ms = int(time.time() * 1000)
    go, _brief = (True, args.brief) if args.force else due(cfg, now_ms)
    if not go:
        print("nothing to do right now")
        return
    st = store()
    season = api.rest("/v1/state/nba", fresh=True)["season"]
    cold = st.db.execute("SELECT 1 FROM games WHERE season=? LIMIT 1", (str(season),)).fetchone() is None
    warn = ensure_history(st, str(season)) if cold else None
    from .cycle import Busy, Ctx, run_league
    from .review import backfill, record_review
    rs, path = _run_state()
    rs.setdefault("brief", {})
    results = []
    leagues = discover(cfg)
    rs["leagues"] = [str(lid) for lid, _rid, _name in leagues]
    for lid, rid, name in leagues:
        if args.league and str(args.league) != str(lid):
            continue
        ctx = None
        try:
            brief = args.brief or brief_due(cfg, lid, rs)
            ctx = Ctx(cfg, lid, rid, live, st)
            if warn:
                ctx.append("errors", {"t": time.time(), "warning": warn})
            snap = run_league(ctx, brief=brief)
            observed = getattr(ctx, "roster_for_gate", None)
            if observed is not None:
                rs.setdefault("rosters", {})[str(lid)] = {"rid": rid, "hash": roster_hash(observed),
                                                         "full_ms": now_ms,
                                                         "directives": getattr(ctx, "directives_for_gate", "")}
            results.append({"league": name, "p_win": snap["p_win"], "acted": snap["acted"]})
            if brief:
                if snap["notified"]:
                    rs["brief"][str(lid)] = _today()
            for _week, text, row in backfill(lid, rid):
                delivered = notify.send(cfg, text)
                channels = cfg.get("notify") or {}
                if delivered or not any(channels.get(k) for k in ("command", "telegram_chat_id", "ntfy_topic")):
                    record_review(lid, row)
        except Busy as e:
            results.append({"league": name, "skipped": str(e)})
        except Exception as e:
            error = {"t": time.time(), "error": api.redact(repr(e)), "trace": api.redact(traceback.format_exc())}
            try:
                if ctx is not None:
                    ctx.append("errors", error)
                else:
                    print(error["error"], file=sys.stderr)
            except Exception:
                print(error["error"], file=sys.stderr)
            notify.send(cfg, f"sleeper-gm: {name} run FAILED ({api.redact(repr(e))}). Nothing after the failure was written.")
            results.append({"league": name, "error": api.redact(repr(e))})
    rs["last_full_ms"] = now_ms
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rs))
    if not cold:
        warn = ensure_history(st, str(season))
        if warn:
            print(warn, file=sys.stderr)
    print(json.dumps({"live": live, "results": results}, indent=1, default=str))


def cmd_status(args):
    from .cycle import Ctx, _claims_check, bench_role, load, plan_details
    from .policy import decide
    cfg = config.load()
    st = store()
    ensure_history(st, str(api.rest("/v1/state/nba")["season"]))
    for lid, rid, name in discover(cfg):
        if args.league and str(args.league) != str(lid):
            continue
        ctx = Ctx(cfg, lid, rid, False, st)
        wk, me, opp = load(ctx)
        pending_drops = _claims_check(ctx, wk, me)
        excluded = {s for s, (p, _x, _g) in me.pending.items() if p in pending_drops}
        dec = decide(me, opp, len(wk.days), forbidden_locks=excluded) if wk.days else None
        print(f"\n{name} (week {wk.week}), days left {wk.days}")
        for directive in getattr(wk, "directives", []):
            print(f"  Instruction: {directive['label']}")
        for warning in ctx.acted:
            print(f"  {warning}")
        if not dec:
            continue
        print(f"win chance {dec.p_win:.1%}   you ~{dec.mean_total:.1f}   opponent ~{dec.opp_mean:.1f}")
        for s, slot in enumerate(me.slots):
            p = me.starters[s]
            tag = ""
            if s in me.locked:
                tag = f"locked {me.locked[s]:.1f}"
            elif s in me.inflight:
                tag = "playing now"
            elif s in me.pending:
                tag = (f"{me.pending[s][1]:.1f} finished, open value {dec.cont.get(s, 0):.1f} -> "
                       + ("LOCK" if s in dec.locks else "pass"))
            print(f"  {slot:<4} {wk.name(p) if p != '0' else '(empty)':<26} {tag}")
        print("  today:", ", ".join(f"{me.slots[s]} {wk.name(p)}" for s, p in dec.assign_today.items()) or "nobody plays")
        plans = plan_details(wk, me, dec)
        print("  Plan (ET; later looks require passing; same-day swaps need a final box):")
        for s, plan in plans.items():
            print("   ", notify.plan_line({"slot": me.slots[s], "plan": plan}),
                  f"[expected {plan['expected_value']:.1f}]")
        bench = [p for p in me.positions if p not in me.starters]
        if bench:
            print("  Bench:")
            for p in bench:
                count = len(me.games.get(p, {}))
                print(f"    {wk.name(p)}: {bench_role(wk, me, plans, p)}; {count} {'game' if count == 1 else 'games'} left")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sleeper-gm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("login")
    p.add_argument("--from-browser", action="store_true")
    p.add_argument("--cdp", default="http://127.0.0.1:9222")
    sub.add_parser("leagues")
    p = sub.add_parser("status")
    p.add_argument("--league")
    p = sub.add_parser("run")
    p.add_argument("--live", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--brief", action="store_true")
    p.add_argument("--league")
    sub.add_parser("on")
    sub.add_parser("off")
    p = sub.add_parser("schedule")
    p.add_argument("action", choices=["install", "remove", "status"])
    p.add_argument("--cron", action="store_true", help="use crontab even where systemd/launchd exist")
    p = sub.add_parser("web")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--demo", action="store_true", help="serve the bundled demo data")
    sub.add_parser("mcp")
    p = sub.add_parser("review")
    p.add_argument("week", nargs="?", type=int)
    p.add_argument("--league")
    sub.add_parser("backtest")
    a = ap.parse_args(argv)
    if a.cmd == "login":
        c = auth.from_browser(a.cdp) if a.from_browser else auth.login_interactive()
        print(f"saved token for {c.get('display_name')} (expires {datetime.fromtimestamp(c.get('exp', 0)):%Y-%m-%d})")
    elif a.cmd == "leagues":
        discover(config.load(), verbose=True)
    elif a.cmd == "status":
        cmd_status(a)
    elif a.cmd == "run":
        cmd_run(a)
    elif a.cmd in ("on", "off"):
        live = config.set_live(a.cmd == "on")
        print("live: scheduled runs will make changes" if live else "off: runs only report what they would do")
    elif a.cmd == "schedule":
        from . import schedule
        print(getattr(schedule, a.action)(cron=a.cron))
    elif a.cmd == "web":
        from . import web
        web.serve(a.host, a.port, demo=a.demo)
    elif a.cmd == "mcp":
        from . import mcp_server
        mcp_server.main()
    elif a.cmd == "review":
        from .review import review
        cfg = config.load()
        for lid, rid, name in discover(cfg):
            if a.league and str(a.league) != str(lid):
                continue
            wk = a.week or int(api.rest("/v1/state/nba", fresh=True)["leg"]) - 1
            print(review(lid, rid, wk, persist=True)[0])
    elif a.cmd == "backtest":
        from . import backtest
        backtest.main([])
    else:
        ap.print_help()


if __name__ == "__main__":
    main(sys.argv[1:])
