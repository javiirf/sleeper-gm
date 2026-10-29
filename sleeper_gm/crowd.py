"""Read-only, heuristic signals from Sleeper NBA player performance discussions.

Verified live on 2026-10-29 at https://sleeper.com/graphql. The fantasy HTML at
https://sleeper.com/leagues loaded
https://sleepercdn.com/js/bundle-cf8c2e018e710f218a70cd18b154a1b4.js.
The homepage now serves a separate scores app. The fantasy bundle loads
``topics(channel_id: ..., order_by: "last_created")`` and then
``messages(parent_id: topic_id, ...)``. Player metadata.channel_id is a
container, NOT the message parent: both its messages and topic listing were
empty. Introspection revealed the working player/game topic resolver below;
that resolver was not present in the downloaded web bundle.

Exact query shapes used here::

    query CrowdSport { sport_info(sport: "nba") }
    query CrowdGames($season: String!, $season_type: String!, $week: Int!) {
      scores(sport: "nba", season: $season, season_type: $season_type,
             week: $week) { game_id status start_time metadata }
    }
    query CrowdTopic($season: String!, $season_type: String!,
                     $player: String!, $game: String!) {
      player_performance_topic(sport: "nba", season: $season,
        season_type: $season_type, player_id: $player, game_id: $game) {
        topic_id
      }
    }
    query CrowdMessages($parent: Snowflake!, $before: Snowflake) {
      messages(parent_id: $parent, before: $before, order_by: "desc") {
        message_id text created
      }
    }

sport_info is a Map containing season, season_type and week. scores.metadata
contains home_team/away_team maps with team abbreviations. Resolve each
player's latest completed team game in the CURRENT week to its topic_id.
Do not mix previous games' discussions or guess parents from channel IDs.
No completed game this week means no signal; since_ms filters messages within
that selected game topic, not games or topic creation dates. Live checks
returned distinct topics for two Curry games and 50 recent messages each for
Curry, Trae Young and LeBron James. These topics are league-independent.

Message.created and Score.start_time are Unix MILLISECONDS. For example,
1793247146621 was 2026-10-29T04:12:26.621Z. Live message text was a string
(LocalizedString scalar), not a language map. messages returns [Message],
has no limit argument, and defaults to order_by: "desc" and show_hidden:
false. Omit before on page one; subsequent before values are message_id
cursors, not timestamps. Topic and message reads use auth=True; sport_info
and scores work without authentication. No author fields or reactions are
requested. No mutations, subscriptions, channel joins or read receipts run.

Rules: case-insensitive whole words lock/locked/locking/locks vote lock;
wait/waiting, pass/passed/passing, hold/holding and ride/riding vote pass.
Negated locking (including dont lock and not locking) votes pass. Negated
waiting words are ignored. Pass wins conflicting statements; messages with
a question mark do not vote. Repetition counts once per message. Availability
and injury keywords supply news snippets, preserving negation and uncertainty;
these are unverified chat claims, not official reports. Sarcasm, unmarked
names in prose and context such as a basketball "pass" cannot be inferred.

Explicit @mentions, mention markup, username prefixes and URLs are removed
before classification and caching. Only sanitized summaries enter the bounded
60-second in-memory cache, keyed by (season, season_type, player, game,
since_ms). The shared schedule context also expires after 60 seconds. Neither
raw messages nor author information is cached or persisted.
"""
from __future__ import annotations

from collections import OrderedDict
import html
import json
import re
import time
import urllib.error

from sleeper_gm import api


_SPORT_QUERY = 'query CrowdSport { sport_info(sport: "nba") }'
_SCORES_QUERY = '''query CrowdGames($season: String!, $season_type: String!, $week: Int!) {
  scores(sport: "nba", season: $season, season_type: $season_type, week: $week) {
    game_id status start_time metadata
  }
}'''
_TOPIC_QUERY = '''query CrowdTopic($season: String!, $season_type: String!,
                                 $player: String!, $game: String!) {
  player_performance_topic(sport: "nba", season: $season,
    season_type: $season_type, player_id: $player, game_id: $game) {
    topic_id
  }
}'''
_QUERY = '''query CrowdMessages($parent: Snowflake!, $before: Snowflake) {
  messages(parent_id: $parent, before: $before, order_by: "desc") {
    message_id text created
  }
}'''
_MAX_MESSAGES = 50
_MAX_PAGES = 5
_CACHE_SIZE = 128
_CACHE_SECONDS = 60
_cache = OrderedDict()
_cache_gql = None
_context_cache = None

_LOCK = re.compile(r"\block(?:ed|ing|s)?\b", re.I)
_PASS_WORDS = r"(?:wait(?:ing)?|pass(?:ed|ing)?|hold(?:ing)?|ride|riding)"
_PASS = re.compile(rf"\b{_PASS_WORDS}\b", re.I)
_NEGATION = (
    r"(?:no|not|never|don't|dont|do\s+not|won't|wont|wouldn't|wouldnt|"
    r"shouldn't|shouldnt|can't|cant|cannot|isn't|isnt|haven't|havent|hasn't|hasnt)"
    r"(?:\s+(?:a|the|really|ever|yet|even|gonna|going|to|be|have|already)){0,4}\s+"
)
_NEG_LOCK = re.compile(rf"\b{_NEGATION}lock(?:ed|ing|s)?\b", re.I)
_NEG_PASS = re.compile(rf"\b{_NEGATION}{_PASS_WORDS}\b", re.I)
_NEWS = re.compile(
    r"\b(?:out|ruled\s+out|questionable|doubtful|gtd|rest|resting|dnp|"
    r"minutes?[ -]+(?:restriction|limit|cap)s?|injur(?:y|ies|ed)|"
    r"sprain(?:ed)?|strain(?:ed)?|sore(?:ness)?|hurt|illness|sick|"
    r"concussion|fractur(?:e|ed)|broken|ankle|knee|hamstring|calf|"
    r"achilles|groin|starting|will\s+play)\b", re.I,
)


def _empty():
    return {"n": 0, "lock": 0, "pass": 0, "lock_share": None,
            "news": [], "latest_ms": None}


def _clean(text):
    """Strip explicit identities/links, not arbitrary player names in prose."""
    text = html.unescape(text)
    # Markdown links and Sleeper/Discord-style mention encodings can contain
    # both a display name and an ID. Remove the entire construct.
    text = re.sub(r"!?@?\[[^\]\n]*\]\([^\)\n]*\)", " ", text)
    text = re.sub(r"<@[^>]*>|@\{[^}]*\}|@\[[^\]]*\]", " ", text)
    text = re.sub(r"(?i)(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>]+", " ", text)
    text = re.sub(r"(?i)\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|co|app|gg)\b[^\s<>]*", " ", text)
    text = re.sub(r"(?<!\w)@[\w.-]+|\b[uU]/[\w-]+", " ", text)
    text = re.sub(r"<[^>]*>", " ", text)
    # A pasted line such as 'hoopsfan: OUT tonight' must lose its byline.
    text = re.sub(r"(?m)^\s*(?:>\s*)?[\w.-]+(?:[ \t]+[\w.-]+){0,3}"
                  r"[ \t]*(?::|\bsaid:)[ \t]*", "", text)
    return " ".join(text.split()).strip(" :")


def _classify(text):
    """Return one vote or None, plus at most one sanitized news snippet."""
    clean = _clean(text)
    normalized = clean.lower().replace("\u2019", "'").replace("\u2018", "'")
    vote = None
    if "?" not in normalized:
        if _NEG_LOCK.search(normalized) or _PASS.search(_NEG_PASS.sub(" ", normalized)):
            vote = "pass"
        elif _LOCK.search(normalized):
            vote = "lock"
    match = _NEWS.search(clean)
    # Center long messages on the availability phrase instead of unrelated intro.
    start = max(0, match.start() - 40) if match else 0
    news = clean[start:start + 120].strip() if match else None
    return vote, news


def _unavailable(error):
    """api.gql wraps transport failures in SleeperError without a cause."""
    if isinstance(error, (OSError, TimeoutError, urllib.error.URLError)):
        return True
    message = str(error).lower()
    if isinstance(error, api.SleeperError):
        return (message.startswith("http ") or api.GRAPHQL in message
                or "unauthorized" in message or "unauthenticated" in message
                or any(f'cannot query field "{field}"' in message for field in
                       ("messages", "player_performance_topic", "sport_info", "scores")))
    return isinstance(error, RuntimeError) and "no sleeper token" in message


def _read(topic, since_ms, gql):
    """Read descending pages with bounded work, retaining no raw-message cache."""
    result = _empty()
    seen = set()
    before = None
    for _ in range(_MAX_PAGES):
        variables = {"parent": topic}
        if before is not None:
            variables["before"] = before
        rows = gql(_QUERY, variables, auth=True)["messages"]
        if not isinstance(rows, list):
            raise ValueError("invalid messages list")
        if not rows:
            break
        # Sort defensively, including before applying the local cap.
        rows = sorted(rows, key=lambda row: int(row["created"]), reverse=True)
        progressed = False
        reached_since = False
        for row in rows:
            created = int(row["created"])
            if created < since_ms:
                reached_since = True
                break
            mid = str(row["message_id"])
            if mid in seen:
                continue
            seen.add(mid)
            progressed = True
            text = row.get("text")
            if text is not None and not isinstance(text, str):
                raise ValueError("invalid message text")
            vote, news = _classify(text or "")
            result["n"] += 1
            result["latest_ms"] = max(result["latest_ms"] or created, created)
            if vote:
                result[vote] += 1
            if news and len(result["news"]) < 3:
                result["news"].append(news)
            if result["n"] == _MAX_MESSAGES:
                break
        cursor = str(rows[-1]["message_id"])
        if (result["n"] == _MAX_MESSAGES or reached_since or not progressed
                or cursor == before):
            break
        before = cursor
    votes = result["lock"] + result["pass"]
    result["lock_share"] = result["lock"] / votes if votes else None
    return result


def _context(gql):
    """Cache current NBA state and latest completed games, never chat payloads."""
    global _context_cache
    if _context_cache and time.monotonic() - _context_cache[0] < _CACHE_SECONDS:
        return _context_cache[1:]
    state = gql(_SPORT_QUERY, auth=False)["sport_info"]
    if isinstance(state, str):
        state = json.loads(state)
    season, season_type, week = str(state["season"]), state["season_type"], int(state["week"])
    if not season.isdigit() or not season_type or week < 1:
        raise ValueError("invalid NBA state")
    rows = gql(_SCORES_QUERY, {"season": season, "season_type": season_type,
                              "week": week}, auth=False)["scores"]
    if not isinstance(rows, list):
        raise ValueError("invalid NBA games")
    latest = {}
    for row in rows:
        if row["status"] != "complete":
            continue
        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        start = int(row["start_time"])
        game = row["game_id"]
        if not game:
            raise ValueError("missing game ID")
        for side in ("home_team", "away_team"):
            team = (metadata.get(side) or {}).get("team")
            if team and (team not in latest or start > latest[team][0]):
                latest[team] = (start, str(game))
    _context_cache = (time.monotonic(), season, season_type, latest)
    return season, season_type, latest


def signals(players, pids, since_ms, gql=api.gql):
    """Return recent summaries from each player's latest completed game topic.

    The interface and vote rules are unchanged. n counts unique messages with
    created >= since_ms, up to 50 per player and five message pages. lock_share
    is lock / (lock + pass), or None without votes. news holds at most three
    snippets, newest first. Missing players, teams, completed games or topics,
    malformed player data and individual read failures yield a zero summary.
    Transport, authentication, missing-query or shared-context failures return
    {} for the whole call. Failures are not cached. Switching the injected gql
    reader clears both caches to isolate data sources.
    """
    global _cache_gql, _context_cache
    if gql is not _cache_gql:
        _cache.clear()
        _context_cache = None
        _cache_gql = gql
    try:
        since_ms = int(since_ms)
        pids = list(pids)
    except Exception:
        return {}
    if not pids:
        return {}
    context = None
    results = {}
    for pid in pids:
        try:
            results[pid] = _empty()
            player = players.get(pid) or {}
            team = player.get("team")
            if not team:
                continue
            if context is None:
                try:
                    context = _context(gql)
                except Exception:
                    return {}
            season, season_type, games = context
            if team not in games:
                continue
            game = games[team][1]
            key = (season, season_type, str(pid), game, since_ms)
            cached = _cache.get(key)
            now = time.monotonic()
            if cached and now - cached[0] < _CACHE_SECONDS:
                summary = cached[1]
            else:
                topic = gql(_TOPIC_QUERY, {"season": season, "season_type": season_type,
                                           "player": str(pid), "game": game},
                            auth=True)["player_performance_topic"]
                if not topic or not topic.get("topic_id"):
                    continue
                summary = _read(str(topic["topic_id"]), since_ms, gql)
                _cache[key] = (time.monotonic(), summary)
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_SIZE:
                _cache.popitem(last=False)
            results[pid] = {**summary, "news": list(summary["news"])}
        except Exception as error:
            if _unavailable(error):
                return {}
            # Never log an exception or a response: either can contain identities.
            continue
    return results
