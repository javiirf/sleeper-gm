"""Offline contracts for read-only player-chat signals."""
from copy import deepcopy
import json
import urllib.error

import pytest

from sleeper_gm import api, crowd


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    crowd._cache.clear()
    monkeypatch.setattr(crowd, "_cache_gql", None)
    monkeypatch.setattr(crowd, "_context_cache", None, raising=False)
    yield
    crowd._cache.clear()


def message(mid, created, text):
    return {"message_id": str(mid), "created": created, "text": text}


def game(gid, team="GSW", start=100, status="complete"):
    return {"game_id": gid, "start_time": start, "status": status,
            "metadata": {"home_team": {"team": team}, "away_team": {"team": "OTHER"}}}


def players():
    return {"1085": {"team": "GSW", "metadata": {"channel_id": "empty-channel"}},
            "1945": {"team": "WAS"}, "1362": {"team": "PHI"}}


class FakeGql:
    """Network boundary with distinct channel, game and topic identities."""

    def __init__(self, pages=None):
        self.pages = pages or {}
        self.calls = []
        self.state = {"season": "2026", "season_type": "regular", "week": 2}
        self.games = [game("older", start=100), game("latest", start=200),
                      game("live", start=300, status="in_game"),
                      game("future", start=400, status="pre_game"),
                      game("young-game", "WAS"), game("james-game", "PHI")]
        self.topics = {("1085", "latest"): {"topic_id": "curry-topic"},
                       ("1085", "older"): {"topic_id": "old-topic"},
                       ("1945", "young-game"): {"topic_id": "young-topic"},
                       ("1362", "james-game"): {"topic_id": "james-topic"}}
        self.failure = {}

    def __call__(self, query, variables=None, *, auth=False):
        assert query.lstrip().startswith("query ")
        assert "mutation" not in query
        assert "author" not in query and "user_reactions" not in query
        if "sport_info(" in query:
            kind, value = "sport_info", self.state
            assert auth is False
        elif "scores(" in query:
            kind, value = "scores", self.games
            assert auth is False
            assert variables == {"season": self.state["season"],
                                 "season_type": self.state["season_type"],
                                 "week": self.state["week"]}
        elif "player_performance_topic(" in query:
            kind = "player_performance_topic"
            assert auth is True
            assert variables["season"] == self.state["season"]
            assert variables["season_type"] == self.state["season_type"]
            value = self.topics.get((variables["player"], variables["game"]))
        else:
            kind = "messages"
            assert "messages(parent_id: $parent" in query
            assert 'order_by: "desc"' in query and "limit:" not in query
            assert auth is True
            value = self.pages.get((variables["parent"], variables.get("before")), [])
        self.calls.append((kind, deepcopy(variables)))
        value = self.failure.get(kind, value)
        if isinstance(value, Exception):
            raise value
        return {kind: deepcopy(value)}

    def calls_for(self, kind):
        return [variables for name, variables in self.calls if name == kind]


@pytest.mark.parametrize("phrase,vote", [
    ("lock him", "lock"), ("LOCK IT IN!", "lock"),
    ("Locked that 48 already", "lock"), ("I'm locking this one", "lock"),
    ("lock lock lock", "lock"), ("dont lock", "pass"),
    ("don't lock him", "pass"), ("don\u2019t lock it", "pass"),
    ("not locking yet", "pass"), ("do not lock this score", "pass"),
    ("I would not lock him", "pass"), ("not a lock", "pass"),
    ("never gonna lock 19", "pass"), ("wouldn't lock that", "pass"),
    ("hold till friday", "pass"), ("wait for Sunday", "pass"),
    ("I'll pass on 22", "pass"), ("ride it out", "pass"),
    ("riding with him tomorrow", "pass"), ("holding for next game", "pass"),
    ("I locked, but wait for Friday", "pass"),
    ("don't wait, lock him", "lock"), ("not passing, lock it", "lock"),
    ("don't wait", None), ("lock him?", None), ("lock or wait?", None),
    ("blocked, unlocked, passive, waitress", None), ("great game", None),
    ("@locked @wait", None), ("https://example.com/lock", None),
])
def test_votes(phrase, vote):
    assert crowd._classify(phrase)[0] == vote


@pytest.mark.parametrize("phrase,is_news", [
    ("he's OUT tonight", True), ("ruled out per team", True),
    ("questionable for tomorrow", True), ("doubtful", True),
    ("GTD again", True), ("rest day", True), ("resting tomorrow", True),
    ("DNP tonight", True), ("minutes restriction", True),
    ("25 minute limit", True), ("minutes cap", True),
    ("ankle sprain", True), ("knee soreness", True),
    ("injury update", True), ("hamstring strain", True),
    ("concussion protocol", True), ("he is starting", True),
    ("coach says he will play", True), ("he is NOT out tonight", True),
    ("will he play? questionable", True), ("outstanding game", False),
    ("restored his shot", False), ("@out_news https://example.com/injury", False),
    ("48 points, easy lock", False),
])
def test_news_keywords_preserve_claims(phrase, is_news):
    snippet = crowd._classify(phrase)[1]
    assert (snippet is not None) is is_news
    if is_news:
        assert snippet == phrase


@pytest.mark.parametrize("raw,expected", [
    ("@hoopsfan he's OUT tonight https://example.com/report", "he's OUT tonight"),
    ("@some.name @other-user minutes restriction", "minutes restriction"),
    ("hoopsfan: ruled out", "ruled out"),
    ("Hoops Fan: ruled out", "ruled out"),
    ("> hoopsfan: ruled out", "ruled out"),
    ("hoopsfan said: ruled out", "ruled out"),
    ("@hoopsfan: ruled out", "ruled out"),
    ("<@123456789> <@!987654321> he will play", "he will play"),
    ("&lt;@123456789&gt; he will play", "he will play"),
    ("@[hoopsfan](123456789) @{987654321} starting", "starting"),
    ("@[hoopsfan] starting", "starting"),
    ("[hoopsfan](https://sleeper.com/user/123) OUT", "OUT"),
    ("OUT www.example.com/report", "OUT"),
    ("OUT example.com/report?user=someone", "OUT"),
    ("OUT <https://example.com/report>", "OUT"),
    ("u/hoopsfan OUT", "OUT"),
    ("fan1: OUT\nfan2: minutes restriction", "OUT minutes restriction"),
])
def test_identity_and_url_stripping(raw, expected):
    assert crowd._clean(raw) == expected
    assert crowd._classify(raw)[1] == expected


@pytest.mark.parametrize("size", [119, 120, 121, 500])
def test_snippet_length(size):
    text = "minutes restriction " + "x" * size
    news = crowd._classify(text)[1]
    assert len(news) <= 120
    assert "minutes restriction" in news
    news = crowd._classify("x" * size + " minutes restriction tonight")[1]
    assert len(news) <= 120 and "minutes restriction" in news


@pytest.mark.parametrize("metadata", [None, {}, {"channel_id": "empty-channel"},
                                      '{"channel_id":"empty-channel"}', "bad json"])
def test_reads_game_topic_not_empty_player_channel(metadata):
    p = players()
    p["1085"]["metadata"] = metadata
    fake = FakeGql({("curry-topic", None): [message("1", 1793247146621, "lock it")]})
    result = crowd.signals(p, ["1085"], 1793247146621, gql=fake)
    assert result == {"1085": {"n": 1, "lock": 1, "pass": 0, "lock_share": 1.0,
                               "news": [], "latest_ms": 1793247146621}}
    assert fake.calls_for("player_performance_topic") == [
        {"season": "2026", "season_type": "regular", "player": "1085", "game": "latest"}]
    assert fake.calls_for("messages") == [{"parent": "curry-topic"},
                                           {"parent": "curry-topic", "before": "1"}]


@pytest.mark.parametrize("status,expected", [("complete", "curry-topic"),
    ("in_game", "old-topic"), ("pre_game", "old-topic"), ("postponed", "old-topic")])
@pytest.mark.parametrize("away,json_metadata", [(False, False), (True, True)])
def test_latest_completed_team_game(status, expected, away, json_metadata):
    fake = FakeGql({(expected, None): [message("1", 100, "lock")]})
    fake.games[1]["status"] = status
    if away:
        for row in fake.games:
            row["metadata"]["home_team"], row["metadata"]["away_team"] = (
                row["metadata"]["away_team"], row["metadata"]["home_team"])
    if json_metadata:
        for row in fake.games:
            row["metadata"] = json.dumps(row["metadata"])
    assert crowd.signals(players(), ["1085"], 0, gql=fake)["1085"]["n"] == 1
    assert fake.calls_for("messages")[0]["parent"] == expected


@pytest.mark.parametrize("season,week,season_type", [("2026", 2, "regular"),
                                                    ("2027", 1, "pre")])
def test_current_season_and_week_come_from_sleeper(season, week, season_type):
    fake = FakeGql({("curry-topic", None): [message("1", 1, "lock")]})
    fake.state = {"season": season, "week": week, "season_type": season_type}
    assert crowd.signals(players(), ["1085"], 0, gql=fake)["1085"]["lock"] == 1
    assert fake.calls_for("scores") == [fake.state]


def test_counts_news_order_and_vote_denominator():
    rows = [message("3", 3000, "@someone OUT tonight https://example.com"),
            message("5", 5000, "hold till friday"), message("2", 2000, "minutes restriction"),
            message("6", "6000", "lock him"), message("1", 1000, "starting"),
            message("4", 4000, "will play"), message("7", 7000, "great game")]
    fake = FakeGql({("curry-topic", None): rows})
    result = crowd.signals(players(), ["1085"], 1000, gql=fake)
    assert result == {"1085": {"n": 7, "lock": 1, "pass": 1, "lock_share": .5,
                               "news": ["will play", "OUT tonight", "minutes restriction"],
                               "latest_ms": 7000}}


@pytest.mark.parametrize("count", [0, 1, 49, 50, 51, 85])
def test_cap_is_per_player_and_keeps_newest(count):
    rows = [message(i, 1000 + i, "lock" if i < count - 50 else "wait")
            for i in range(count)]
    fake = FakeGql({("curry-topic", None): rows, ("young-topic", None): rows})
    result = crowd.signals(players(), ["1085", "1945"], 0, gql=fake)
    for pid in ("1085", "1945"):
        assert result[pid]["n"] == min(count, 50)
        assert result[pid]["lock"] == 0
        assert result[pid]["pass"] == min(count, 50)
        assert result[pid]["latest_ms"] == (999 + count if count else None)
        assert result[pid]["lock_share"] == (0.0 if count else None)


@pytest.mark.parametrize("since,expected,calls", [(0, 4, 3), (100, 4, 3),
    (101, 3, 2), (300, 2, 2), (301, 1, 1), (401, 0, 1)])
def test_cutoff_and_message_id_pagination(since, expected, calls):
    fake = FakeGql({
        ("curry-topic", None): [message("m4", 400, "lock"), message("m3", 300, "pass")],
        ("curry-topic", "m3"): [message("m2", 200, "lock"), message("m1", 100, "wait")],
    })
    result = crowd.signals(players(), ["1085"], since, gql=fake)["1085"]
    assert result["n"] == expected
    assert len(fake.calls_for("messages")) == calls
    if calls >= 2:
        assert fake.calls_for("messages")[1]["before"] == "m3"


def test_cap_across_pages_and_duplicate_messages():
    page1 = [message(i, i, "lock") for i in range(100, 70, -1)]
    page2 = [message(i, i, "pass") for i in range(71, 40, -1)]
    fake = FakeGql({("curry-topic", None): page1, ("curry-topic", "71"): page2})
    result = crowd.signals(players(), ["1085"], 0, gql=fake)["1085"]
    assert (result["n"], result["lock"], result["pass"], result["lock_share"]) == (50, 30, 20, .6)
    assert len(fake.calls_for("messages")) == 2


@pytest.mark.parametrize("repeated", [True, False])
def test_paging_cannot_loop_forever(repeated):
    page = [message("5", 500, "lock")]
    pages = {("curry-topic", None): page}
    for i in range(5, 0, -1):
        pages[("curry-topic", str(i))] = page if repeated else [message(i - 1, (i - 1) * 100, "lock")]
    fake = FakeGql(pages)
    result = crowd.signals(players(), ["1085"], 0, gql=fake)["1085"]
    assert len(fake.calls_for("messages")) == (2 if repeated else crowd._MAX_PAGES)
    assert result["n"] == (1 if repeated else crowd._MAX_PAGES)


@pytest.mark.parametrize("text", ["", None, "great game", "lock?"])
def test_nonvotes_count_without_a_share(text):
    fake = FakeGql({("curry-topic", None): [message("1", 1, text)]})
    result = crowd.signals(players(), ["1085"], 0, gql=fake)["1085"]
    assert result["n"] == 1 and result["lock_share"] is None


@pytest.mark.parametrize("missing", ["player", "team", "game", "topic"])
def test_missing_source_returns_zero_without_guessing_a_parent(missing):
    p, fake = players(), FakeGql()
    if missing == "player":
        del p["1085"]
    elif missing == "team":
        del p["1085"]["team"]
    elif missing == "game":
        fake.games = []
    else:
        fake.topics = {}
    assert crowd.signals(p, ["1085"], 0, gql=fake) == {"1085": crowd._empty()}
    assert fake.calls_for("messages") == []


@pytest.mark.parametrize("stage,failure", [
    ("topic", RuntimeError("one player failed")), ("topic", {}),
    ("topic", {"topic_id": None}), ("topic", []),
    ("messages", api.SleeperError("bad parent_id")), ("messages", None),
    ("messages", {}), ("messages", [message("1", "bad", "lock")]),
    ("messages", [{"created": 1, "text": "lock"}]),
    ("messages", [message("1", 1, {"en": "lock"})]),
])
def test_player_failures_do_not_hide_other_players(stage, failure):
    fake = FakeGql({("young-topic", None): [message("1", 1, "lock")]})
    if stage == "topic":
        fake.topics[("1085", "latest")] = failure
    else:
        fake.pages[("curry-topic", None)] = failure
    result = crowd.signals(players(), ["1085", "1945"], 0, gql=fake)
    assert result["1085"] == crowd._empty()
    assert result["1945"]["lock"] == 1
    assert len(crowd._cache) == 1


def test_later_page_error_discards_partial_summary():
    fake = FakeGql({("curry-topic", None): [message("1", 1, "lock")],
                    ("curry-topic", "1"): RuntimeError("broken page")})
    assert crowd.signals(players(), ["1085"], 0, gql=fake) == {"1085": crowd._empty()}
    assert not crowd._cache


@pytest.mark.parametrize("stage", ["sport_info", "scores", "player_performance_topic", "messages"])
@pytest.mark.parametrize("error", [
    ConnectionError("offline"), TimeoutError(), urllib.error.URLError("offline"),
    api.SleeperError("HTTP 503 unavailable"), api.SleeperError("HTTP 429 rate limited"),
    api.SleeperError("Unauthorized"), api.SleeperError("Unauthenticated"),
    api.SleeperError(api.GRAPHQL + ": TimeoutError"), RuntimeError("no Sleeper token: run login"),
])
def test_unavailable_endpoint_returns_empty_and_can_recover(stage, error):
    fake = FakeGql()
    fake.failure[stage] = error
    assert crowd.signals(players(), ["1085", "1945"], 0, gql=fake) == {}
    assert not crowd._cache
    fake.failure.clear()
    assert crowd.signals(players(), ["1085"], 0, gql=fake) == {"1085": crowd._empty()}


@pytest.mark.parametrize("field", ["messages", "player_performance_topic", "scores", "sport_info"])
def test_missing_query_returns_empty(field):
    fake = FakeGql()
    fake.failure[field] = api.SleeperError(f'Cannot query field "{field}" on type "RootQueryType"')
    assert crowd.signals(players(), ["1085"], 0, gql=fake) == {}


@pytest.mark.parametrize("field,value", [("sport_info", None), ("sport_info", {}),
    ("sport_info", {"season": "2026", "season_type": "regular", "week": None}),
    ("scores", None), ("scores", {})])
def test_malformed_shared_context_returns_empty(field, value):
    fake = FakeGql()
    fake.failure[field] = value
    assert crowd.signals(players(), ["1085"], 0, gql=fake) == {}
    assert not crowd._cache


def test_endpoint_failure_discards_earlier_players_too():
    fake = FakeGql({("young-topic", None): TimeoutError()})
    assert crowd.signals(players(), ["1085", "1945"], 0, gql=fake) == {}


@pytest.mark.parametrize("change", ["none", "since", "expired", "reader"])
def test_cache_key_expiry_reader_isolation_and_mutable_results(monkeypatch, change):
    now = [10.0]
    monkeypatch.setattr(crowd.time, "monotonic", lambda: now[0])
    fake = FakeGql({("curry-topic", None): [message("1", 100, "@fan OUT https://example.com")]})
    result = crowd.signals(players(), ["1085"], 0, gql=fake)
    result["1085"]["news"].append("caller mutation")
    result["1085"]["n"] = 99
    before, since = len(fake.calls), 0
    if change == "since":
        since = 1
    elif change == "expired":
        now[0] += crowd._CACHE_SECONDS
    elif change == "reader":
        fake, before = FakeGql(), 0
    result = crowd.signals(players(), ["1085"], since, gql=fake)
    assert len(fake.calls) > before if change != "none" else len(fake.calls) == before
    assert result["1085"]["n"] != 99
    assert "caller mutation" not in result["1085"]["news"]
    cached = json.dumps(list(crowd._cache.values()))
    assert "@fan" not in cached and "https://" not in cached


def test_new_completed_game_refreshes_topic_after_cache_expiry(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(crowd.time, "monotonic", lambda: now[0])
    fake = FakeGql({("curry-topic", None): [message("1", 100, "lock")],
                    ("new-topic", None): [message("2", 200, "wait")]})
    assert crowd.signals(players(), ["1085"], 0, gql=fake)["1085"]["lock_share"] == 1.0
    fake.games[2]["status"] = "complete"
    fake.topics[("1085", "live")] = {"topic_id": "new-topic"}
    now[0] += crowd._CACHE_SECONDS
    assert crowd.signals(players(), ["1085"], 0, gql=fake)["1085"]["lock_share"] == 0.0
    assert fake.calls_for("player_performance_topic")[-1]["game"] == "live"


def test_cache_size_is_bounded(monkeypatch):
    monkeypatch.setattr(crowd, "_CACHE_SIZE", 2)
    fake = FakeGql()
    crowd.signals(players(), ["1085", "1945", "1362"], 0, gql=fake)
    assert len(crowd._cache) == 2
    before = len(fake.calls_for("messages"))
    crowd.signals(players(), ["1085"], 0, gql=fake)
    assert len(fake.calls_for("messages")) > before


def test_author_fields_never_reach_results_or_cache():
    row = {**message("1", 100, "@secret_handle OUT"), "author_id": "private_id",
           "author_display_name": "secret_handle", "author_real_name": "Secret Name"}
    fake = FakeGql({("curry-topic", None): [row]})
    result = crowd.signals(players(), ["1085"], 0, gql=fake)
    encoded = json.dumps([result, list(crowd._cache.values())])
    assert all(s not in encoded for s in ("private_id", "secret_handle", "Secret Name", "author"))


@pytest.mark.parametrize("pids,since", [([], 0), (["1085"], None), (None, 0), (["1085"], "invalid")])
def test_empty_or_invalid_request_does_not_fetch(pids, since):
    fake = FakeGql()
    assert crowd.signals(players(), pids, since, gql=fake) == {}
    assert fake.calls == []
