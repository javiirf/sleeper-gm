"""Sleeper access: public REST, GraphQL reads, and the few writes the manager makes.

Writes go through `write()`, which refuses unless live mode is on, and every
write is read back over authenticated GraphQL (REST is Cloudflare-cached and
cannot confirm a write).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

REST = "https://api.sleeper.app/v1"
REST2 = "https://api.sleeper.com"
GRAPHQL = "https://sleeper.com/graphql"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"

_last_call = 0.0


class SleeperError(RuntimeError):
    """Every message is passed through redact(): the token must never reach a log or a notification."""

    def __init__(self, msg):
        super().__init__(redact(str(msg)))


def redact(text):
    """Remove the session token (and anything shaped like a JWT) from text."""
    import re
    text = str(text)
    try:
        from .auth import _cached
        tok = _cached()
        if tok:
            text = text.replace(tok, "<token>")
    except Exception:
        pass
    return re.sub(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+", "<token>", text)


def _pace(gap=0.35):
    global _last_call
    wait = _last_call + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _http(url, data=None, headers=None, tries=3):
    """GET/POST with retries. Writes pass tries=1: a timed-out write may have committed,
    so it is reconciled against fresh state by the caller, never blindly resent."""
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    for i in range(tries):
        _pace()
        req = urllib.request.Request(url, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = redact(e.read().decode(errors="replace"))[:300]
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise SleeperError(f"HTTP {e.code} {url}: {body}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise SleeperError(f"{url}: {type(e).__name__}: {e}") from None
        except ValueError as e:  # e.g. an invalid header value: never echo it, it may be the token
            raise SleeperError(f"{url}: invalid request ({type(e).__name__})") from None


def rest(path, fresh=False):
    """GET a public REST path: "/v1/..." hits api.sleeper.app, anything else (/stats, /projections) api.sleeper.com."""
    url = REST + path[3:] if path.startswith("/v1/") else REST2 + path
    if fresh:
        url += ("&" if "?" in url else "?") + f"_={int(time.time())}"
    return _http(url)


def token():
    from .auth import token as _token
    return _token()


def gql(query, variables=None, auth=False, write=False):
    h = {"content-type": "application/json"}
    if auth:
        h["authorization"] = token()
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    d = _http(GRAPHQL, data=body, headers=h, tries=1 if write else 3)
    if d.get("errors"):
        raise SleeperError("; ".join(e.get("message", "?") for e in d["errors"]))
    return d["data"]


LEG_FIELDS = "leg round roster_id matchup_id points proj_points max_points starters starters_games subs"


def legs_with_players(league_id, week):
    """Legs plus each roster's player_map, whose injury_status is fresher than the
    once-a-day player dictionary (used to decide IR moves)."""
    q = '{ matchup_legs(round:%d, league_id:"%s"){ %s player_map } }' % (int(week), league_id, LEG_FIELDS)
    return gql(q, auth=True)["matchup_legs"]


def legs(league_id, week):
    """Authenticated, uncached view of every roster's matchup leg for the week."""
    return gql('{ matchup_legs(round:%d, league_id:"%s"){ %s } }' % (int(week), league_id, LEG_FIELDS), auth=True)["matchup_legs"]


def league_rosters(league_id):
    return gql('{ league_rosters(league_id:"%s"){ roster_id owner_id players starters reserve settings } }' % league_id, auth=True)["league_rosters"]


def scores(season, week):
    q = '{ scores(sport:"nba", season_type:"regular", season:"%s", week:%d){ date game_id status start_time week metadata } }'
    return gql(q % (season, int(week)))["scores"]


def league_transactions(league_id, leg):
    q = '{ league_transactions(league_id:"%s", leg:%d){ transaction_id type status adds drops settings created leg roster_ids } }'
    return gql(q % (league_id, int(leg)), auth=True)["league_transactions"]


# ---------------------------------------------------------------- writes

class Writer:
    """All writes. `live=False` records what would be sent and sends nothing."""

    def __init__(self, live, log):
        self.live = live
        self.log = log  # callable(dict)

    def _send(self, kind, query, variables, summary):
        entry = {"t": time.time(), "kind": kind, "summary": summary, "live": self.live, "query": query, "variables": variables}
        if not self.live:
            entry["result"] = "dry-run"
            self.log(entry)
            return None
        try:
            data = gql(query, variables, auth=True, write=True)
            entry["result"] = "ok"
            self.log(entry)
            return data
        except SleeperError as e:
            entry["result"] = f"error: {e}"
            self.log(entry)
            raise

    def set_leg(self, league_id, roster_id, week, starters, starters_games=None, summary=""):
        """Lineup (and optionally Lock-In picks) on the week's matchup leg, as Sleeper's web app saves it.

        `starters` is positional (one id per starting slot, "0" for empty).
        `starters_games` must be the FULL player->game map: existing locks plus new ones.
        """
        args = f'league_id:"{league_id}", roster_id:{int(roster_id)}, leg:{int(week)}, round:{int(week)}, starters:{json.dumps(starters)}'
        if starters_games is not None:
            q = "mutation update_matchup_leg($starters_games: Map) { update_matchup_leg(%s, starters_games: $starters_games) { %s } }" % (args, LEG_FIELDS)
            v = {"starters_games": json.dumps(starters_games)}
        else:
            q = "mutation update_matchup_leg { update_matchup_leg(%s) { %s } }" % (args, LEG_FIELDS)
            v = {}
        return self._send("set_leg", q, v, summary)

    def free_agent(self, league_id, roster_id, add=None, drop=None, summary=""):
        adds = [add] if add else []
        drops = [drop] if drop else []
        q = ('mutation { league_create_transaction(league_id:"%s", type:"free_agent", k_adds:%s, v_adds:%s, k_drops:%s, v_drops:%s)'
             ' { transaction_id status type adds drops } }') % (
            league_id, json.dumps(adds), json.dumps([int(roster_id)] * len(adds)), json.dumps(drops), json.dumps([int(roster_id)] * len(drops)))
        return self._send("free_agent", q, {}, summary)

    def set_reserve(self, league_id, roster_id, reserve, summary=""):
        q = 'mutation { roster_update_reserve(league_id:"%s", roster_id:%d, reserve:%s) { roster_id starters players reserve } }' % (
            league_id, int(roster_id), json.dumps(reserve))
        return self._send("set_reserve", q, {}, summary)
