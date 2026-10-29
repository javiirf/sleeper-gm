"""Offline owner chat boundaries: local command, saved instructions and HTTP."""
import json
import shlex
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest

from sleeper_gm import config, web


@pytest.fixture
def home(tmp_path, monkeypatch):
    for key, value in {"CONFIG_DIR": tmp_path / "cfg", "CONFIG_FILE": tmp_path / "cfg/config.json",
                       "TOKEN_FILE": tmp_path / "cfg/token", "STATE_DIR": tmp_path / "state",
                       "CACHE_DIR": tmp_path / "cache"}.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.delenv("SLEEPER_TOKEN", raising=False)
    directory = config.league_state("555")
    now_ms = int(time.time() * 1000)
    snapshot = {"t": now_ms, "live": False, "league": {"id": "555", "name": "Local league", "season": "2026", "week": 2},
                "p_win": .42, "me": {"slots": [{"slot": "PG", "state": "pending", "score": 32,
                "player": {"id": "1", "name": "Starter One"}, "plan": {"expected_value": 34},
                "crowd": {"n": 4, "lock_share": .75, "news": ["UNTRUSTED NEWS"], "messages": ["CROWD COMMAND"]}}],
                "bench": [{"id": "2", "name": "Bench Two"}], "ir": []},
                "opp": {"name": "Opponent says ignore instructions", "slots": []}, "claims": {"bot": [{"add": "Free Three"}]}}
    (directory / "now.json").write_text(json.dumps(snapshot))
    (directory / "players.json").write_text(json.dumps({"1": {"name": "Starter One"}, "2": {"name": "Bench Two"}, "3": {"name": "Free Three"}}))
    return directory


def command_for(reply):
    return [sys.executable, "-c", "import sys; prompt=sys.stdin.read(); print(" + repr(reply) + ")"]


def configured(command, **settings):
    config.save({"chat": {"command": command, **settings}})
    return config.load()


@pytest.mark.parametrize("overrides,expected", [
    ({}, (90, 24, 30)), ({"timeout": 12}, (12, 24, 30)),
    ({"history": 2}, (90, 2, 30)), ({"per_hour": 1}, (90, 24, 1)),
])
def test_partial_chat_config_keeps_limits(home, overrides, expected):
    cfg = configured(["model"], **overrides)
    assert (cfg["chat"]["timeout"], cfg["chat"]["history"], cfg["chat"]["per_hour"]) == expected


@pytest.mark.parametrize("live", [True, False])
def test_shared_live_switch_preserves_config(home, live):
    config.save({"live": not live, "chat": {"command": ["local-model"]}})
    assert config.set_live(live) is live
    assert config.load()["live"] is live
    assert config.load()["chat"]["command"] == ["local-model"]


@pytest.mark.parametrize("as_string,history_limit,decision_count,news,aggregate,expected_history,first_decision,kept_decisions,expected_crowd", [
    (False, 1, 3, True, .75, ["old-4"], 0, 3, {"n": 4, "lock_share": .75}),
    (True, 2, 45, False, .75, ["old-3", "old-4"], 5, 40, {"n": 4, "lock_share": .75}),
    (False, 24, 45, True, "AGGREGATE INSTRUCTION", ["old-0", "old-1", "old-2", "old-3", "old-4"], 5, 40, {"n": 4}),
    (True, 24, 3, False, "AGGREGATE INSTRUCTION", ["old-0", "old-1", "old-2", "old-3", "old-4"], 0, 3, {"n": 4}),
], ids=["one-turn-short-log-news", "two-turns-long-log", "full-history-reject-text-aggregate", "argv-string-short-log-reject-text"])
def test_command_receives_context_and_history_on_stdin(home, as_string, history_limit, decision_count, news,
                                                     aggregate, expected_history, first_decision, kept_decisions, expected_crowd):
    from sleeper_gm import chat
    snap = json.loads((home / "now.json").read_text())
    crowd = snap["me"]["slots"][0]["crowd"]
    crowd["lock_share"] = aggregate
    if not news:
        crowd.pop("news")
    (home / "now.json").write_text(json.dumps(snap))
    for i in range(decision_count):
        with (home / "logs/decisions-2026-10-28.jsonl").open("a") as out:
            out.write(json.dumps({"t": i, "week": 2, "decision": i}) + "\n")
    (home / "review.jsonl").write_text(json.dumps({"week": 1, "won": True}) + "\n")
    (home / "chat.jsonl").write_text("\n".join(json.dumps({"t": i, "role": "user", "text": f"old-{i}", "actions": []}) for i in range(5)))
    cmd = [sys.executable, "-c", "import sys; print(sys.stdin.read())"]
    cfg = configured(shlex.join(cmd) if as_string else cmd, history=history_limit)
    result = chat.reply(home, "Why pass?", cfg)
    prompt = result["reply"]
    data = prompt.split("\nBEGIN_UNTRUSTED_DATA_", 1)[1].split("\n", 1)[1].split("\nEND_UNTRUSTED_DATA_", 1)[0]
    context = json.loads(data)
    assert context["now"]["p_win"] == .42 and context["now"]["me"]["slots"][0]["plan"]["expected_value"] == 34
    assert len(context["decisions"]) == kept_decisions
    assert context["decisions"][0]["decision"] == first_decision
    assert context["decisions"][-1]["decision"] == decision_count - 1
    assert context["reviews"] == [{"week": 1, "won": True}]
    assert context["players"]["1"] == {"name": "Starter One"} and "3" not in context["players"]
    assert [row["text"] for row in context["history"]] == expected_history
    assert "UNTRUSTED NEWS" not in prompt and "CROWD COMMAND" not in prompt
    assert "AGGREGATE INSTRUCTION" not in prompt
    assert context["now"]["me"]["slots"][0]["crowd"] == expected_crowd
    assert "untrusted" in prompt.lower() and "Why pass?" in prompt
    rows = chat.state(home, cfg)["history"]
    assert rows[-2]["text"] == "Why pass?" and rows[-1]["role"] == "assistant"


@pytest.mark.parametrize("kind,pid,extra,accepted", [
    ("keep", "1", {}, True), ("avoid", "3", {}, True),
    ("lock_at", "1", {"points": 35}, True), ("lock_at", "3", {"points": 35}, False),
    ("keep", "404", {}, False), ("trade", "1", {}, False),
    ("lock_at", "1", {"points": "35"}, False), ("lock_at", "1", {"points": True}, False),
    ("lock_at", "1", {"points": float("nan")}, False),
    ("lock_at", "1", {"points": 10 ** 400}, False),
    ("lock_at", "1", {"points": -2}, False),
    ("keep", "1", {"command": "danger"}, False),
])
def test_actions_are_validated_and_names_resolved(home, kind, pid, extra, accepted):
    from sleeper_gm import chat, directives
    action = {"kind": kind, "player_id": pid, "name": "MODEL INVENTED NAME", **extra}
    cfg = configured(command_for("My call.\n```json\n" + json.dumps({"actions": [action]}) + "\n```"))
    result = chat.reply(home, "Save that instruction", cfg)
    assert result["reply"] == "My call."
    assert bool(result["applied"]) is accepted
    assert bool(result["refused"]) is not accepted
    rows = directives.load(home)
    assert bool(rows) is accepted
    if accepted:
        assert rows[0]["name"] == ("Free Three" if pid == "3" else "Starter One")
        assert rows[0]["id"] and rows[0]["label"]
        if kind == "lock_at":
            assert rows[0]["season"] == "2026" and rows[0]["week"] == 2
            expiry = datetime.fromtimestamp(rows[0]["expires_at"] / 1000, ZoneInfo("America/New_York"))
            assert expiry.weekday() == 0 and expiry.hour == 0
    else:
        assert result["refused"][0]["reason"]


@pytest.mark.parametrize("text,accepted", [("Remember an injury concern", True), ("a" * 200, True), ("a" * 201, False), ("", False), (7, False)])
def test_note_limit(home, text, accepted):
    from sleeper_gm import chat
    cfg = configured(command_for("```json\n" + json.dumps({"actions": [{"kind": "note", "text": text}]}) + "\n```"))
    result = chat.reply(home, "Remember this", cfg)
    assert bool(result["applied"]) is accepted
    assert bool(result["refused"]) is not accepted


@pytest.mark.parametrize("tail,kind", [("{oops", None), ('{"actions":"bad"}', None), ('{"actions":[{"kind":"avoid","player_id":"3"}]}', "avoid")])
def test_multiple_json_fences_never_apply_actions(home, tail, kind):
    from sleeper_gm import chat, directives
    reply = 'First.\n```json\n{"actions":[{"kind":"keep","player_id":"1"}]}\n```\nLast.\n```json\n' + tail + '\n```'
    result = chat.reply(home, "Explain", configured(command_for(reply)))
    assert tail not in result["reply"]
    assert directives.load(home) == []


@pytest.mark.parametrize("kind,remove", [("keep", "unkeep"), ("avoid", "unavoid"), ("lock_at", "unlock_at")])
def test_replacement_has_new_id_and_stale_undo_cannot_remove_it(home, kind, remove):
    from sleeper_gm import chat, directives
    action = {"kind": kind, "player_id": "1"}
    if kind == "lock_at":
        action["points"] = 30
    cfg = configured(command_for("```json\n" + json.dumps({"actions": [action]}) + "\n```"))
    first = chat.reply(home, "Save", cfg)["applied"][0]["id"]
    second = chat.reply(home, "Replace", cfg)["applied"][0]["id"]
    assert first != second
    chat.undo(home, first, cfg)
    assert [r["id"] for r in directives.load(home)] == [second]
    cfg = configured(command_for("```json\n" + json.dumps({"actions": [{"kind": remove, "player_id": "1"}]}) + "\n```"))
    chat.reply(home, "Remove", cfg)
    assert directives.load(home) == []


@pytest.mark.parametrize("kind,expected", [("pause", False), ("resume", True)])
def test_pause_resume_uses_shared_live_switch(home, kind, expected):
    from sleeper_gm import chat
    cfg = configured(command_for('```json\n{"actions":[{"kind":"' + kind + '"}]}\n```'))
    result = chat.reply(home, "Change manager mode", cfg)
    assert result["live"] is expected and config.load()["live"] is expected


@pytest.mark.parametrize("age_days,accepted", [(0, True), (8, False)])
def test_stale_snapshot_cannot_extend_lock_directive(home, age_days, accepted):
    from sleeper_gm import chat
    snap = json.loads((home / "now.json").read_text())
    snap["t"] -= age_days * 86400_000
    (home / "now.json").write_text(json.dumps(snap))
    cfg = configured(command_for('```json\n{"actions":[{"kind":"lock_at","player_id":"1","points":30}]}\n```'))
    assert bool(chat.reply(home, "Lock at 30", cfg)["applied"]) is accepted


@pytest.mark.parametrize("now_ms,season,week,active", [(99, "2026", 2, True), (100, "2026", 2, False), (99, "2026", 3, False), (99, "2027", 2, False)])
def test_directive_expiry_and_week_are_enforced(home, now_ms, season, week, active):
    from sleeper_gm import directives
    assert directives.fingerprint(home) == ""
    (home / "directives.json").write_text(json.dumps([{"kind": "lock_at", "player_id": "1", "points": 30, "season": "2026", "week": 2, "expires_at": 100}]))
    assert bool(directives.load(home, season=season, week=week, now_ms=now_ms)) is active
    old = directives.fingerprint(home)
    (home / "directives.json").write_text("[]")
    assert directives.fingerprint(home) != old


@pytest.mark.parametrize("text,expected", [("secret-session-token", "<token>"), ("A benign injury note", "A benign injury note")])
def test_history_and_directive_writes_are_redacted(home, monkeypatch, text, expected):
    from sleeper_gm import auth, chat
    monkeypatch.setattr(auth, "_cached", lambda: "secret-session-token")
    cfg = configured(command_for(text + '\n```json\n' + json.dumps({"actions": [{"kind": "note", "text": text}]}) + '\n```'))
    result = chat.reply(home, text, cfg)
    assert result["reply"] == expected
    assert [row["text"] for row in result["history"]] == [expected, expected]
    assert result["directives"][0]["text"] == expected
    assert "secret-session-token" not in json.dumps(result)
    for filename in ("chat.jsonl", "directives.json"):
        assert "secret-session-token" not in (home / filename).read_text()


@pytest.mark.parametrize("command,status", [(None, 503), (["/path/does/not/exist"], 502), ([sys.executable, "-c", "import time;time.sleep(2)"], 504), ([sys.executable, "-c", "import sys;sys.stderr.write('private failure');sys.exit(4)"], 502)])
def test_command_failure_is_safe(home, command, status):
    from sleeper_gm import chat
    with pytest.raises(chat.ChatError) as error:
        chat.reply(home, "private owner message", configured(command, timeout=.05))
    assert error.value.status == status
    assert "private" not in str(error.value)
    if status == 503:
        assert str(error.value) == "Chat is off. Set chat.command in your config."


@pytest.fixture
def server(home, monkeypatch):
    monkeypatch.setattr(web.Handler, "demo", False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def request(base, path="/api/chat?league=555", body=None, headers=None, raw=None):
    data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


@pytest.mark.parametrize("body,headers,raw,status", [
    ({"message": "Why?"}, {}, None, 200), ({"message": ""}, {}, None, 400),
    ({"message": "a" * 1001}, {}, None, 400), ({"message": 7}, {}, None, 400),
    ([], {}, None, 400), ({"message": "x", "actions": []}, {}, None, 400),
    ({"message": "Why?"}, {"Content-Type": "text/plain"}, None, 415),
    ({"message": "Why?"}, {"Origin": "https://elsewhere.example"}, None, 403),
    ({"message": "Why?"}, {"Origin": ""}, None, 403),
    (None, {}, b'{broken', 400), (None, {}, b'x' * 8193, 413),
])
def test_post_validation_before_command(server, body, headers, raw, status):
    configured(command_for("The current win chance is 42%."))
    actual, result = request(server[1], body=body, headers=headers, raw=raw)
    assert actual == status
    assert ("reply" in result) is (status == 200)


@pytest.mark.parametrize("login,status", [(None, 403), ("stranger@example.test", 403), ("owner@example.test", 200)])
@pytest.mark.parametrize("path,body", [("/api/chat?league=555", None), ("/api/directives?league=555", None), ("/api/chat?league=555", {"message": "Why?"}), ("/api/chat/clear?league=555", {}), ("/api/directives?league=555", {"id": "old"})])
def test_allow_list_applies_to_reads_and_mutations(server, login, status, path, body):
    configured(command_for("42%."), allow=["owner@example.test"])
    headers = {"Tailscale-User-Login": login} if login else {}
    assert request(server[1], path, body=body, headers=headers)[0] == status


@pytest.mark.parametrize("per_hour,next_status", [(1, 429), (2, 200)])
def test_clear_keeps_directives_and_cannot_evade_rate_limit(server, home, per_hour, next_status):
    configured(command_for('Saved.\n```json\n{"actions":[{"kind":"keep","player_id":"1"}]}\n```'), per_hour=per_hour)
    assert request(server[1], body={"message": "Keep Starter One"})[0] == 200
    status, cleared = request(server[1], "/api/chat/clear?league=555", body={})
    assert status == 200 and cleared["history"] == [] and len(cleared["directives"]) == 1
    assert cleared["directives"][0]["kind"] == "keep"
    assert request(server[1], body={"message": "Again"})[0] == next_status


@pytest.mark.parametrize("path,body", [("/api/chat?league=555", {"message": "Again"}), ("/api/chat/clear?league=555", {}), ("/api/directives?league=555", {"id": "old"})])
def test_mutations_cannot_race_inflight_model(server, home, path, body):
    marker = home / "started"
    cmd = [sys.executable, "-c", "import pathlib,time;pathlib.Path(" + repr(str(marker)) + ").touch();time.sleep(.6);print('Done')"]
    configured(cmd)
    result = []
    worker = threading.Thread(target=lambda: result.append(request(server[1], body={"message": "First"})))
    worker.start()
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert marker.exists()
    assert request(server[1], path, body=body)[0] == 409
    worker.join()
    assert result[0][0] == 200


@pytest.mark.parametrize("removed,expected", [(False, ["3"]), (True, [])])
def test_now_exposes_new_directives_before_next_run(server, home, removed, expected):
    snapshot = json.loads((home / "now.json").read_text())
    snapshot["directives"] = [{"id": "stale", "kind": "avoid", "player_id": "3", "name": "Free Three"}] if removed else []
    (home / "now.json").write_text(json.dumps(snapshot))
    configured(command_for('Saved.\n```json\n{"actions":[{"kind":"avoid","player_id":"3"}]}\n```'))
    status, result = request(server[1], body={"message": "Avoid Free Three"})
    assert status == 200
    if removed:
        ident = result["applied"][0]["id"]
        assert request(server[1], "/api/directives?league=555", body={"id": ident})[0] == 200
    status, snap = request(server[1], "/api/now?league=555")
    assert status == 200 and [row["player_id"] for row in snap["directives"]] == expected


@pytest.mark.parametrize("with_command", [False, True])
def test_demo_chat_uses_no_command_or_production_writes(server, home, monkeypatch, with_command):
    marker = home / "model-was-invoked"
    failing_command = [sys.executable, "-c", "import pathlib,sys; pathlib.Path(" + repr(str(marker)) + ").touch(); sys.exit(4)"]
    configured(failing_command if with_command else None)
    before = config.CONFIG_FILE.read_bytes()
    monkeypatch.setattr(web.Handler, "demo", True)
    status, result = request(server[1], "/api/chat?league=demo", body={"message": "pause"})
    assert not marker.exists()
    assert status == 200
    assert "demo" in result["reply"].lower()
    assert request(server[1], "/api/chat?league=demo")[1]["history"]
    assert not (home / "chat.jsonl").exists()
    assert config.CONFIG_FILE.read_bytes() == before
    assert request(server[1], "/api/chat/clear?league=demo", body={})[1]["history"] == []


@pytest.mark.parametrize("allow,status", [([None], 403), ([], 403), ("owner@example.test", 403), (None, 200)])
def test_malformed_access_list_does_not_allow_missing_identity(server, allow, status):
    configured(command_for("42%."), allow=allow)
    assert request(server[1])[0] == status


@pytest.mark.parametrize("kind,label", [("keep", "Never drop Starter One"), ("avoid", "Never add Starter One")])
def test_labels_explain_effect_without_gm_jargon(home, kind, label):
    from sleeper_gm import chat
    cfg = configured(command_for('```json\n{"actions":[{"kind":"' + kind + '","player_id":"1"}]}\n```'))
    assert chat.reply(home, "Save instruction", cfg)["applied"][0]["label"] == label


@pytest.mark.parametrize("live,server_ms", [(False, 1793500000000), (True, 1793503600000)])
def test_prompt_uses_current_live_setting_over_stale_snapshot(home, monkeypatch, live, server_ms):
    from sleeper_gm import chat
    monkeypatch.setattr(chat.time, "time", lambda: server_ms / 1000)
    snap = json.loads((home / "now.json").read_text())
    snap["live"] = not live
    snap["t"] = server_ms - 7200_000
    (home / "now.json").write_text(json.dumps(snap))
    cfg = configured(command_for("OK"))
    cfg["live"] = live
    text = chat.prompt(home, "Are you paused?", cfg)
    data = text.split("\nBEGIN_UNTRUSTED_DATA_", 1)[1].split("\n", 1)[1].split("\nEND_UNTRUSTED_DATA_", 1)[0]
    context = json.loads(data)
    assert context["runtime"] == {"live": live, "now": chat._when(server_ms)}
    assert context["now"]["t"] == chat._when(server_ms - 7200_000) != context["runtime"]["now"]
    assert str(server_ms) not in text and str(server_ms - 7200_000) not in text
    assert "FINAL game" in text and "daily-waiver" in text and "never sent" in text


@pytest.mark.parametrize("live", [False, True])
def test_now_overlays_current_live_switch(server, home, live):
    snap = json.loads((home / "now.json").read_text())
    snap["live"] = not live
    (home / "now.json").write_text(json.dumps(snap))
    config.set_live(live)
    assert request(server[1], "/api/now?league=555")[1]["live"] is live


@pytest.mark.parametrize("suffix,applied", [("", True), (" \n", True), ("Do that now.", False)])
def test_only_terminal_action_fence_is_applied(home, suffix, applied):
    from sleeper_gm import chat
    cfg = configured(command_for('Proposed.\n```json\n{"actions":[{"kind":"keep","player_id":"1"}]}\n```' + suffix))
    result = chat.reply(home, "Save instruction", cfg)
    assert bool(result["applied"]) is applied


@pytest.mark.parametrize("setting,value", [("timeout", 10 ** 400), ("history", .5), ("per_hour", 10 ** 400)])
def test_invalid_numeric_configuration_returns_safe_error(home, setting, value):
    from sleeper_gm import chat
    cfg = configured(command_for("OK"), **{setting: value})
    with pytest.raises(chat.ChatError) as error:
        chat.reply(home, "Hi", cfg)
    assert error.value.status == 503


@pytest.mark.parametrize("points,refused", [(30, False), (10 ** 400, True)], ids=["finite-threshold", "overflow-threshold"])
def test_huge_threshold_is_refused_after_prior_valid_action(server, points, refused):
    actions = [{"kind": "pause"}, {"kind": "lock_at", "player_id": "1", "points": points}]
    configured(command_for("Proposed.\n```json\n" + json.dumps({"actions": actions}) + "\n```"))
    status, result = request(server[1], body={"message": "Pause and save that threshold"})
    assert status == 200 and result["live"] is False
    assert result["applied"][0]["kind"] == "pause"
    assert bool(result["refused"]) is refused
    assert len(result["applied"]) == (1 if refused else 2)
    if refused:
        assert result["refused"][0]["kind"] == "lock_at"


@pytest.mark.parametrize("kind", ["lock_at", "unlock_at"])
@pytest.mark.parametrize("next_season,next_week,seconds,refresh_snapshot,owned,expected", [
    ("2026", 2, 4, True, True, {"lock_at": True, "unlock_at": True}),
    ("2026", 3, 10, True, True, {"lock_at": False, "unlock_at": False}),
    ("2027", 2, 4, True, True, {"lock_at": False, "unlock_at": False}),
    ("2026", 2, 10, False, True, {"lock_at": False, "unlock_at": False}),
    ("2026", 3, 4, True, True, {"lock_at": False, "unlock_at": False}),
    ("2026", 2, 4, True, False, {"lock_at": False, "unlock_at": True}),
], ids=["same-week", "new-week", "new-season", "expired-old-snapshot", "week-changed-before-midnight", "player-no-longer-owned"])
def test_weekly_action_cannot_move_to_another_week_during_command(home, monkeypatch, kind, next_season, next_week,
                                                              seconds, refresh_snapshot, owned, expected):
    from sleeper_gm import chat, directives
    accepted = expected[kind]
    sunday = datetime(2026, 11, 1, 23, 59, 55, tzinfo=ZoneInfo("America/New_York")).timestamp()
    clock = {"now": sunday}
    monkeypatch.setattr(chat.time, "time", lambda: clock["now"])
    before = json.loads((home / "now.json").read_text())
    before["t"] = int(sunday * 1000)
    (home / "now.json").write_text(json.dumps(before))

    def model(_settings, text):
        data = text.split("\nBEGIN_UNTRUSTED_DATA_", 1)[1].split("\n", 1)[1].split("\nEND_UNTRUSTED_DATA_", 1)[0]
        assert json.loads(data)["now"]["league"] == {"id": "555", "name": "Local league", "season": "2026", "week": 2}
        clock["now"] = sunday + seconds
        current = json.loads(json.dumps(before))
        current["league"].update(season=next_season, week=next_week)
        if refresh_snapshot:
            current["t"] = int(clock["now"] * 1000)
        if not owned:
            current["me"]["slots"] = []
        (home / "now.json").write_text(json.dumps(current))
        action = {"kind": kind, "player_id": "1"}
        if kind == "lock_at":
            action["points"] = 30
        return 'Proposed.\n```json\n' + json.dumps({"actions": [action]}) + '\n```'

    monkeypatch.setattr(chat, "_command", model)
    result = chat.reply(home, "Lock Starter One at 30 this week", configured(["local-model"]))
    assert bool(result["applied"]) is accepted
    assert bool(result["refused"]) is not accepted
    if accepted and kind == "lock_at":
        lock = directives.load(home)[0]
        assert (lock["season"], lock["week"]) == ("2026", 2)
        expiry = datetime.fromtimestamp(lock["expires_at"] / 1000, ZoneInfo("America/New_York"))
        assert expiry.isoformat() == "2026-11-02T00:00:00-05:00"
    else:
        assert directives.load(home) == []


@pytest.mark.parametrize("refreshed_week,accepted", [(2, True), (3, False)])
def test_prompt_uses_the_snapshot_captured_before_prompt_build(home, monkeypatch, refreshed_week, accepted):
    from sleeper_gm import chat
    original_prompt = chat.prompt

    def refresh_during_prompt(*args, **kwargs):
        current = json.loads((home / "now.json").read_text())
        current["league"]["week"] = refreshed_week
        (home / "now.json").write_text(json.dumps(current))
        return original_prompt(*args, **kwargs)

    def model(_settings, text):
        data = text.split("\nBEGIN_UNTRUSTED_DATA_", 1)[1].split("\n", 1)[1].split("\nEND_UNTRUSTED_DATA_", 1)[0]
        assert json.loads(data)["now"]["league"]["week"] == 2
        return 'Proposed.\n```json\n{"actions":[{"kind":"lock_at","player_id":"1","points":30}]}\n```'

    monkeypatch.setattr(chat, "prompt", refresh_during_prompt)
    monkeypatch.setattr(chat, "_command", model)
    result = chat.reply(home, "Lock Starter One at 30 this week", configured(["local-model"]))
    assert bool(result["applied"]) is accepted


@pytest.mark.parametrize("name,catalog,accepted", [
    ("Free Three", {"3": {"full_name": "Free Three"}}, True),
    ("free  three", {"3": {"full_name": "Free Three"}}, True),
    ("Fréé Three", {"3": {"full_name": "Free Three"}}, True),
    ("Free Three", {"3": {"full_name": "Free Three"}, "9": {"full_name": "Free Three", "active": False}}, True),
    ("Free Three", {"3": {"full_name": "Free Three"}, "9": {"full_name": "Free Three"}}, False),
    ("Nobody Here", {"3": {"full_name": "Free Three"}}, False),
])
def test_unlisted_players_resolve_by_unique_name(home, name, catalog, accepted):
    from sleeper_gm import chat, directives
    (home / "players.json").write_text(json.dumps(catalog))
    action = {"kind": "avoid", "name": name}
    cfg = configured(command_for("Noted.\n```json\n" + json.dumps({"actions": [action]}) + "\n```"))
    result = chat.reply(home, "never pick him up", cfg)
    assert bool(result["applied"]) is accepted
    rows = directives.load(home)
    assert [r.get("player_id") for r in rows] == (["3"] if accepted else [])


@pytest.mark.parametrize("key,value,human", [
    ("tip_ms", 1793401200000, True), ("next_processing_ms", 1793344136000, True),
    ("t", 1793256043.98, True), ("score", 1793401200000, False), ("tip_ms", 30, False),
])
def test_prompt_shows_epoch_times_as_eastern_clock(key, value, human):
    from sleeper_gm import chat
    out = chat._human({"x": [{key: value}]})["x"][0][key]
    assert isinstance(out, str) is human
    if human:
        assert out.endswith(" ET") and str(int(value)) not in out
