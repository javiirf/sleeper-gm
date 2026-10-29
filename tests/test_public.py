"""Offline tests for the public surface: config, token handling, league discovery, the
schedule's cheap 'anything to do' check, and the read-only web server."""
import base64
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from sleeper_gm import api, auth, cli, config, web


def fake_jwt(uid=123, name="someone", exp=4102444800):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'HS256'})}.{enc({'user_id': uid, 'display_name': name, 'exp': exp})}.sig"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(config, "TOKEN_FILE", tmp_path / "cfg" / "token")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.delenv("SLEEPER_TOKEN", raising=False)
    return tmp_path


def test_config_defaults_are_safe(home):
    cfg = config.load()
    assert cfg["live"] is False and cfg["leagues"] == "auto"


def test_config_merges_user_values(home):
    config.save({"live": True, "notify": {"ntfy_topic": "x"}})
    cfg = config.load()
    assert cfg["live"] is True and cfg["notify"]["ntfy_topic"] == "x" and cfg["notify"]["command"] is None


@pytest.mark.parametrize("raw", [lambda t: t, lambda t: json.dumps(t)])  # localStorage stores it JSON-quoted
def test_token_saved_0600_and_unquoted(home, raw):
    tok = fake_jwt()
    c = auth.save(raw(tok))
    assert c["user_id"] == 123
    assert auth.token() == tok
    assert (config.TOKEN_FILE.stat().st_mode & 0o777) == 0o600


def test_env_token_wins(home, monkeypatch):
    auth.save(fake_jwt(uid=1))
    monkeypatch.setenv("SLEEPER_TOKEN", fake_jwt(uid=2))
    assert auth.claims()["user_id"] == 2


def test_garbage_token_is_refused(home):
    with pytest.raises(Exception):
        auth.save("not-a-token")
    assert not config.TOKEN_FILE.exists()


@pytest.mark.parametrize("status,mode,mine,expect", [
    ("in_season", 1, True, True),
    ("in_season", 2, True, False),      # not Lock-In: skipped, never guessed
    ("pre_draft", 1, True, False),
    ("in_season", 1, False, False),     # not on that league
])
def test_discover_filters(home, monkeypatch, status, mode, mine, expect):
    auth.save(fake_jwt(uid=42))
    leagues = [{"league_id": "900", "name": "L", "status": status, "settings": {"game_mode": mode}}]
    rosters = [{"roster_id": 3, "owner_id": "42" if mine else "7"}]

    def rest(path, fresh=False):
        if path.startswith("/v1/state"):
            return {"season": "2026", "league_season": "2026", "leg": 2}
        if "/leagues/nba/" in path:
            return leagues
        if path.endswith("/rosters"):
            return rosters
        raise AssertionError(path)
    monkeypatch.setattr(api, "rest", rest)
    found = cli.discover(config.load())
    assert (found == [("900", 3, "L")]) is expect


@pytest.mark.parametrize("games,last_ms,expect", [
    ([{"status": "pre_game", "start_time": 10 ** 13}], 0, False),                      # nothing for days
    ([{"status": "pre_game", "start_time": 1_000 + 3600_000}], 0, True),             # tips within 3 h
    ([{"status": "in_game", "start_time": 0}], 0, True),
    ([{"status": "complete", "start_time": 5 * 3600_000}], 0, True),                 # finished since last run
    ([{"status": "complete", "start_time": 5 * 3600_000}], 20 * 3600_000, False),    # already handled
])
def test_due_is_cheap_and_correct(home, monkeypatch, games, last_ms, expect):
    config.STATE_DIR.mkdir(parents=True)
    from datetime import datetime
    from sleeper_gm import week
    today = datetime.now(week.ET).strftime("%Y-%m-%d")
    (config.STATE_DIR / "run.json").write_text(json.dumps({"last_full_ms": last_ms, "brief_date": today}))
    monkeypatch.setattr(api, "rest", lambda path, fresh=False: {"season": "2026", "leg": 2})
    monkeypatch.setattr(api, "scores", lambda season, week: games)
    go, brief = cli.due(config.load(), 1_000)
    assert go is expect and brief is False


def test_brief_is_due_once_a_day(home, monkeypatch):
    cfg = config.load()
    cfg["brief_at_et"] = "00:00"
    go, brief = cli.due(cfg, 0)
    assert go and brief


def _serve(monkeypatch, demo=False):
    monkeypatch.setattr(web.Handler, "demo", demo)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_web_serves_state_read_only(home, monkeypatch):
    d = config.league_state("555")
    (d / "now.json").write_text(json.dumps({"t": 1, "league": {"id": "555", "name": "Test"}, "p_win": 0.4}))
    with open(d / "logs" / "decisions-2026-10-28.jsonl", "w") as f:
        f.write(json.dumps({"t": 1, "week": 2, "p_win": 0.3}) + "\n" + json.dumps({"t": 2, "week": 3, "p_win": 0.5}) + "\n")
    httpd, base = _serve(monkeypatch)
    try:
        get = lambda p: json.load(urllib.request.urlopen(base + p))
        assert get("/api/leagues")[0]["id"] == "555"
        assert get("/api/now?league=555")["p_win"] == 0.4
        assert [r["week"] for r in get("/api/runs?league=555")] == [3]
        assert [r["week"] for r in get("/api/runs?league=555&week=2")] == [2]
        for bad in ("/api/now?league=../../etc", "/api/nope"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(base + bad)
        req = urllib.request.Request(base + "/api/now?league=555", method="POST", data=b"{}")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(req)
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("padding,cached", [(0, True), (220, True), (0, False), (220, False)])
def test_http_redacts_before_truncating(home, monkeypatch, padding, cached):
    import io
    tok = fake_jwt(name="x" * 600)
    monkeypatch.setattr(auth, "_last", tok if cached else None)
    body = ("x" * padding + " rejected " + tok).encode()
    def fail(*a, **kw):
        raise urllib.error.HTTPError("https://sleeper.com/graphql", 401, "bad", {}, io.BytesIO(body))
    monkeypatch.setattr(urllib.request, "urlopen", fail)
    monkeypatch.setattr(api, "_pace", lambda: None)
    with pytest.raises(api.SleeperError) as error:
        api._http("https://sleeper.com/graphql", tries=1)
    assert tok[:40] not in str(error.value)
    assert "<token>" in str(error.value)


@pytest.mark.parametrize("urls,tokens,expected,visited", [
    (["https://sleeper.com", "https://sleeper.com:443/leagues"], [None, "token"], True, [0, 1]),
    (["https://sleeper.com", "https://sleeper.com/leagues"], ["token", None], True, [0]),
    (["https://sleeper.com", "https://sleeper.com/leagues"], [None, None], False, [0, 1]),
    (["https://sleeper.com:8443", "https://sleeper.com"], ["token", "token"], True, [1]),
    (["http://sleeper.com", "https://sleeper.com.evil", "https://sleeper.com:bad"], ["token"] * 3, False, []),
])
def test_browser_checks_origin_and_each_tab(home, monkeypatch, urls, tokens, expected, visited):
    import io
    import websocket
    tabs = [{"type": "page", "url": u, "webSocketDebuggerUrl": str(i)} for i, u in enumerate(urls)]
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(json.dumps(tabs).encode()))
    connections, closed = [], []
    class Socket:
        def __init__(self, i): self.i = i
        def send(self, message):
            assert json.loads(message)["params"]["expression"] == "localStorage.getItem('token')"
        def recv(self):
            return json.dumps({"id": 1, "result": {"result": {"value": fake_jwt() if tokens[self.i] else None}}})
        def close(self): closed.append(self.i)
    def connect(url, **kw):
        connections.append(int(url))
        return Socket(int(url))
    monkeypatch.setattr(websocket, "create_connection", connect)
    if expected:
        assert auth.from_browser()["user_id"] == 123
    else:
        with pytest.raises(RuntimeError): auth.from_browser()
        assert not config.TOKEN_FILE.exists()
    assert connections == visited == closed


@pytest.mark.parametrize("folder", ["plain", "space and 'quote'", "percent%path"])
def test_cron_escapes_log_percent(home, monkeypatch, folder):
    from sleeper_gm import schedule
    import shlex
    monkeypatch.setattr(config, "STATE_DIR", home / folder)
    monkeypatch.setattr(schedule, "_cmd", lambda: ["/bin/a % b", "x'y"])
    line = schedule._cron_line()
    for i, char in enumerate(line):
        if char == "%": assert line[i - 1] == "\\"
    words = shlex.split(line.replace("\\%", "%"))
    assert words[5:7] == ["/bin/a % b", "x'y"]
    assert words[words.index(">>") + 1] == str(home / folder / "cron.log")


@pytest.mark.parametrize("folder", ["plain", "with spaces", "with'quote", "bad\npath"])
def test_systemd_unit_paths(home, monkeypatch, folder):
    from sleeper_gm import schedule
    cwd = str(home / folder)
    monkeypatch.setattr(schedule, "_cmd", lambda: ["/bin/echo", 'a "quote" $value %thing'])
    if "\n" in folder:
        with pytest.raises(ValueError): schedule._systemd_service(cwd)
    else:
        unit = schedule._systemd_service(cwd)
        assert f"WorkingDirectory={cwd}\n" in unit
        assert 'ExecStart="/bin/echo" "a \\"quote\\" $$value %%thing"\n' in unit


@pytest.mark.parametrize("folder", ["plain", "with spaces", "with'quote"])
def test_systemd_analyze_verifies_generated_unit(tmp_path, monkeypatch, folder):
    from sleeper_gm import schedule
    import shutil
    import subprocess
    analyzer = shutil.which("systemd-analyze")
    if not analyzer: pytest.skip("systemd-analyze is not installed")
    monkeypatch.setattr(schedule, "_cmd", lambda: ["/bin/true"])
    cwd = tmp_path / folder
    cwd.mkdir()
    unit = tmp_path / "sleeper-gm-test.service"
    unit.write_text(schedule._systemd_service(str(cwd)))
    result = subprocess.run([analyzer, "verify", str(unit)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _node(script):
    import shutil
    import subprocess
    if not shutil.which("node"): pytest.skip("node is not installed")
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("value", ['<span><img src=x onerror="alert(1)"></span>', '<img src=x onerror="alert(1)">', 'plain & <text>'])
def test_action_explanations_escape_all_data(value):
    from pathlib import Path
    source = (Path(web.__file__).with_suffix("") / "app.js").read_text()
    helpers = source[source.index("  function esc("):source.index("  var fTime")]
    why = source[source.index("  function whyHtml("):source.index("  function renderMoves(")]
    rendered = _node(helpers + why + '\nconsole.log(JSON.stringify(whyHtml(' + json.dumps({"kind": "needs", "text": value}) + ')));')
    import html
    assert rendered == "<p>" + html.escape(value).replace("&#x27;", "&#39;") + ".</p>"


@pytest.mark.parametrize("name,deleted", [("gm-shell-old", True), ("gm-data-old", True), ("gm-shell-v1", False), ("other-app", False), ("xgm-old", False)])
def test_service_worker_only_deletes_own_old_caches(name, deleted):
    from pathlib import Path
    source = (Path(web.__file__).with_suffix("") / "sw.js").read_text()
    script = """
const handlers = {}, removed = [];
const self = {addEventListener: (name, cb) => handlers[name] = cb, clients: {claim: () => {}}};
const caches = {keys: async () => [CACHE_NAME], delete: async name => removed.push(name)};
""".replace("CACHE_NAME", json.dumps(name)) + source + """
handlers.activate({waitUntil: p => p.then(() => console.log(JSON.stringify(removed)))});
"""
    assert _node(script) == ([name] if deleted else [])


@pytest.mark.parametrize("previous,last_age,fail", [(False, None, False), (True, None, False), (True, 1199, False), (False, 1200, False), (True, 1201, True)])
def test_history_sync_is_bounded_and_throttled(home, monkeypatch, previous, last_age, fail):
    from sleeper_gm.data import Store
    st = Store(home / "cache")
    if previous:
        st.db.execute("INSERT INTO games (season, game_id) VALUES ('2025','old')")
    config.STATE_DIR.mkdir()
    path = config.STATE_DIR / "run.json"
    state = {"brief": {"a": "today"}}
    if last_age is not None: state["last_sync"] = 10000 - last_age
    path.write_text(json.dumps(state))
    monkeypatch.setattr(cli.time, "time", lambda: 10000)
    calls = []
    def sync(season, weeks):
        calls.append((season, list(weeks)))
        if fail: raise api.SleeperError("offline")
    monkeypatch.setattr(st, "sync", sync)
    reads = []
    def rest(path, **kw):
        reads.append(path)
        return {"leg": 7}
    monkeypatch.setattr(api, "rest", rest)
    warning = cli.ensure_history(st, "2026")
    skipped = last_age is not None and last_age < 1200
    assert calls == ([] if skipped else ([] if previous else [("2025", list(range(1, 26)))]) + [("2026", [7])])
    assert bool(reads) is (not skipped)
    assert bool(warning) is fail
    saved = json.loads(path.read_text())
    assert saved["brief"] == {"a": "today"}
    assert saved.get("last_sync") == (state.get("last_sync") if skipped or fail else 10000)
    st.db.close()


@pytest.mark.parametrize("briefs,expected", [({"a": "today"}, (True, True)), ({"a": "today", "b": "yesterday"}, (True, True)), ({"a": "today", "b": "today"}, (False, False))])
def test_due_checks_every_managed_league(home, monkeypatch, briefs, expected):
    config.STATE_DIR.mkdir()
    (config.STATE_DIR / "run.json").write_text(json.dumps({"leagues": ["a", "b"], "brief": briefs}))
    monkeypatch.setattr(cli, "_today", lambda: "today")
    monkeypatch.setattr(api, "rest", lambda *a, **kw: {"season": "2026", "leg": 7})
    monkeypatch.setattr(api, "scores", lambda *a: [])
    cfg = config.load()
    cfg["brief_at_et"] = "00:00"
    assert cli.due(cfg, 0) == expected


def _cli_run_setup(monkeypatch):
    from types import SimpleNamespace
    from sleeper_gm import review
    from sleeper_gm.data import Store
    monkeypatch.setattr(cli, "store", lambda: Store(config.CACHE_DIR))
    monkeypatch.setattr(api, "rest", lambda *a, **kw: {"season": "2026"})
    monkeypatch.setattr(cli, "discover", lambda cfg: [("a", 1, "A"), ("b", 1, "B")])
    monkeypatch.setattr(cli, "ensure_history", lambda *a: "cached warning")
    monkeypatch.setattr(review, "backfill", lambda *a: [])
    return SimpleNamespace(live=False, force=True, brief=True, league=None)


@pytest.mark.parametrize("failure", ["context", "warning", "run", None])
def test_cmd_run_isolates_each_league(home, monkeypatch, capsys, failure):
    from sleeper_gm import cycle
    args = _cli_run_setup(monkeypatch)
    class Context:
        def __init__(self, cfg, lid, *args):
            self.lid = lid
            if lid == "a" and failure == "context": raise OSError("context failed")
        def append(self, kind, entry):
            if self.lid == "a" and failure == "warning": raise OSError("log failed")
    ran = []
    def run(ctx, brief):
        if ctx.lid == "a" and failure == "run": raise OSError("run failed")
        ran.append(ctx.lid)
        return {"p_win": 0.5, "acted": [], "notified": True}
    monkeypatch.setattr(cycle, "Ctx", Context)
    monkeypatch.setattr(cycle, "run_league", run)
    monkeypatch.setattr(cli.notify, "send", lambda *a: False)
    cli.cmd_run(args)
    result = json.loads(capsys.readouterr().out)
    assert ran == (["a", "b"] if failure is None else ["b"])
    assert ("error" in result["results"][0]) is (failure is not None)
    assert result["results"][-1]["league"] == "B"


@pytest.mark.parametrize("delivered", [True, False])
def test_cmd_run_marks_only_delivered_briefs(home, monkeypatch, delivered):
    from sleeper_gm import cycle
    args = _cli_run_setup(monkeypatch)
    monkeypatch.setattr(cycle, "run_league", lambda ctx, brief: {"p_win": 0.5, "acted": [], "notified": delivered or ctx.league_id == "b"})
    cli.cmd_run(args)
    saved = json.loads((config.STATE_DIR / "run.json").read_text())
    assert saved["leagues"] == ["a", "b"]
    assert saved["brief"] == ({"a": cli._today(), "b": cli._today()} if delivered else {"b": cli._today()})


@pytest.mark.parametrize("skipped", [True, False])
def test_season_omits_skipped_reviews(home, skipped):
    path = config.league_state("77") / "review.jsonl"
    row = {"week": 2, "skipped": skipped}
    path.write_text(json.dumps(row) + "\n")
    assert web.season("77") == ([] if skipped else [row])
