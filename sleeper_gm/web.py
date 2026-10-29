"""The phone/web app: local state, owner chat and validated local instructions.
It never talks to Sleeper. Chat can change the manager's shared live setting.

It binds to 127.0.0.1 by default. To reach it from your phone keep it private: put it
behind Tailscale (`tailscale serve --bg --https=8448 http://127.0.0.1:8770`) or another
authenticated proxy. Do not expose it to the open internet: it shows your league.
"""
from __future__ import annotations

import glob
import json
import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import chat, config

WEB_DIR = Path(__file__).resolve().parent / "web"
ENDPOINTS = ("leagues", "now", "runs", "season", "chat", "directives")
_locks = {}
_locks_guard = threading.Lock()


def _jsonl(pattern):
    rows = []
    for f in sorted(glob.glob(pattern)):
        with open(f) as fh:
            rows += [json.loads(l) for l in fh if l.strip()]
    return rows


def leagues():
    out = []
    for d in sorted(config.STATE_DIR.glob("*/now.json")):
        try:
            snap = json.loads(d.read_text())
            out.append({"id": snap["league"]["id"], "name": snap["league"]["name"], "t": snap.get("t")})
        except (ValueError, KeyError):
            continue
    return sorted(out, key=lambda x: -(x.get("t") or 0))


def _league_dir(league):
    if not league or not str(league).isdigit():
        return None
    d = config.STATE_DIR / str(league)
    return d if d.is_dir() else None


def now(league):
    d = _league_dir(league)
    if not d or not (d / "now.json").exists():
        return None
    snap = json.loads((d / "now.json").read_text())
    snap["directives"] = chat.active(d, snap)
    snap["live"] = bool(config.load().get("live", False))
    return snap


def runs(league, week=None):
    d = _league_dir(league)
    if not d:
        return None
    rows = _jsonl(str(d / "logs" / "decisions-*.jsonl"))
    if week is None and rows:
        week = rows[-1].get("week")
    return [r for r in rows if week is None or r.get("week") == int(week)]


def season(league):
    d = _league_dir(league)
    if not d:
        return None
    rows = _jsonl(str(d / "review.jsonl"))
    latest = {}
    for r in rows:
        latest[r["week"]] = r
    return [latest[w] for w in sorted(latest) if not latest[w].get("skipped")]


class Handler(SimpleHTTPRequestHandler):
    demo = False

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB_DIR), **kw)

    def log_message(self, fmt, *args):
        pass

    def _send(self, obj, status=200):
        if obj is None:
            obj, status = {"error": "Not found."}, 404
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("referrer-policy", "no-referrer")
        super().end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return super().do_GET()
        try:
            cfg = self._config()
            self._access(cfg)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            name = u.path[len("/api/"):].strip("/")
            if name not in ENDPOINTS:
                raise chat.ChatError(404, "Not found.")
            if name == "leagues":
                return self._send(self._demo_file(name) if self.demo else leagues())
            league, directory = self._resolve(q.get("league"))
            if name in {"chat", "directives"}:
                view = self._demo_view() if self.demo else chat.state(directory, cfg)
                return self._send(view if name == "chat" else {"directives": view["directives"]})
            if self.demo:
                return self._send(self._demo_file(name))
            if name == "now":
                return self._send(now(league))
            if name == "runs":
                if q.get("week") is not None and not q["week"].isdigit():
                    raise chat.ChatError(400, "Invalid week.")
                return self._send(runs(league, q.get("week")))
            return self._send(season(league))
        except chat.ChatError as error:
            self._send({"error": str(error)}, error.status)
        except Exception:
            self._send({"error": "Could not read local state."}, 500)

    def _config(self):
        return json.loads(json.dumps(config.DEFAULTS)) if self.demo else config.load()

    def _access(self, cfg):
        allow = (cfg.get("chat") or {}).get("allow")
        if allow is not None and (not isinstance(allow, list) or any(not isinstance(login, str) or not login for login in allow)
                                  or self.headers.get("Tailscale-User-Login") not in allow):
            raise chat.ChatError(403, "Access denied.")

    def _resolve(self, league):
        if self.demo:
            if league not in (None, "demo"):
                raise chat.ChatError(404, "Unknown league.")
            return "demo", None
        league = league or next((str(row["id"]) for row in leagues()), None)
        directory = _league_dir(league)
        if directory is None or not (directory / "now.json").is_file():
            raise chat.ChatError(404, "Unknown league.")
        return league, directory

    def _body(self):
        origin = self.headers.get("Origin")
        if origin is not None:
            parsed = urlparse(origin)
            if (parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != self.headers.get("Host", "").lower()
                    or parsed.path or parsed.params or parsed.query or parsed.fragment or parsed.username):
                raise chat.ChatError(403, "Origin does not match this app.")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise chat.ChatError(415, "Use application/json.")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise chat.ChatError(400, "Invalid request length.") from None
        if length < 0:
            raise chat.ChatError(400, "Invalid request length.")
        if length > 8192:
            raise chat.ChatError(413, "Request exceeds 8 KB.")
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise chat.ChatError(400, "Invalid JSON.") from None
        if not isinstance(body, dict):
            raise chat.ChatError(400, "The request must be an object.")
        return body

    def _mutation_lock(self, directory):
        key = ("demo", id(self.server)) if self.demo else ("league", str(directory))
        with _locks_guard:
            return _locks.setdefault(key, threading.Lock())

    def _demo_file(self, name):
        return json.loads((WEB_DIR / "demo" / f"{name}.json").read_text())

    def _demo_view(self):
        if not hasattr(self.server, "chat_demo"):
            self.server.chat_demo = {"history": [], "directives": [], "enabled": True,
                                     "live": bool(self._demo_file("now").get("live"))}
            self.server.chat_demo_rate = []
        return self.server.chat_demo

    def _demo_reply(self, message):
        view = self._demo_view()
        timestamp = int(time.time() * 1000)
        recent = [t for t in self.server.chat_demo_rate if t > timestamp - 3600_000]
        if len(recent) >= 30:
            raise chat.ChatError(429, "Chat limit reached. Try again later.")
        self.server.chat_demo_rate = recent + [timestamp]
        chance = self._demo_file("now").get("p_win")
        reply = (f"Demo: our current win chance is {chance:.0%}. " if isinstance(chance, (int, float)) else "Demo: ")
        reply += "The Board shows the saved plan and lock bars. This conversation uses demo data; instructions are not saved."
        view["history"].extend([{"t": timestamp, "role": "user", "text": message, "actions": []},
                                {"t": timestamp, "role": "assistant", "text": reply, "actions": [], "refused": []}])
        return {**view, "reply": reply, "applied": [], "refused": []}

    def do_POST(self):
        try:
            u = urlparse(self.path)
            if u.path not in {"/api/chat", "/api/chat/clear", "/api/directives"}:
                raise chat.ChatError(404, "Not found.")
            cfg = self._config()
            self._access(cfg)
            body = self._body()
            query = parse_qs(u.query)
            _, directory = self._resolve(query.get("league", [None])[0])
            if u.path == "/api/chat":
                if set(body) != {"message"} or not isinstance(body["message"], str) or not body["message"].strip() or len(body["message"]) > 1000:
                    raise chat.ChatError(400, "A message must contain 1 to 1000 characters.")
            elif u.path == "/api/chat/clear":
                if body:
                    raise chat.ChatError(400, "Clear expects an empty object.")
            elif set(body) != {"id"} or not isinstance(body["id"], str) or not 0 < len(body["id"]) <= 128:
                raise chat.ChatError(400, "A directive id is required.")
            lock = self._mutation_lock(directory)
            if not lock.acquire(blocking=False):
                raise chat.ChatError(409, "The GM is still working on this league. Try again when it finishes.")
            try:
                if self.demo:
                    result = self._demo_reply(body["message"].strip()) if u.path == "/api/chat" else self._demo_view()
                    if u.path == "/api/chat/clear":
                        result["history"] = []
                elif u.path == "/api/chat":
                    result = chat.reply(directory, body["message"], cfg)
                elif u.path == "/api/chat/clear":
                    result = chat.clear(directory, cfg)
                else:
                    result = chat.undo(directory, body["id"], cfg)
                self._send(result)
            finally:
                lock.release()
        except chat.ChatError as error:
            self._send({"error": str(error)}, error.status)
        except Exception:
            self._send({"error": "Could not complete the chat request."}, 500)


def serve(host="127.0.0.1", port=8770, demo=False):
    Handler.demo = demo or os.environ.get("SLEEPER_GM_DEMO") == "1"
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"sleeper-gm web on http://{host}:{port}" + (" (demo data)" if Handler.demo else ""))
    httpd.serve_forever()
