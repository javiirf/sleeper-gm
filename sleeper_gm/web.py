"""The phone/web app: static files from web/ plus read-only JSON over the state the manager
writes. It never talks to Sleeper and has no write endpoints.

It binds to 127.0.0.1 by default. To reach it from your phone keep it private: put it
behind Tailscale (`tailscale serve --bg --https=8448 http://127.0.0.1:8770`) or another
authenticated proxy. Do not expose it to the open internet: it shows your league.
"""
from __future__ import annotations

import glob
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config

WEB_DIR = Path(__file__).resolve().parent / "web"
ENDPOINTS = ("leagues", "now", "runs", "season")


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
    return json.loads((d / "now.json").read_text()) if d and (d / "now.json").exists() else None


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

    def _send(self, obj):
        if obj is None:
            self.send_error(404, "not found")
            return
        body = json.dumps(obj, default=str).encode()
        self.send_response(200)
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
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        name = u.path[len("/api/"):].strip("/")
        if name not in ENDPOINTS:
            return self.send_error(404, "not found")
        if self.demo:
            return self._send(json.loads((WEB_DIR / "demo" / f"{name}.json").read_text()))
        if name == "leagues":
            return self._send(leagues())
        league = q.get("league") or next((l["id"] for l in leagues()), None)
        if name == "now":
            return self._send(now(league))
        if name == "runs":
            return self._send(runs(league, q.get("week")))
        if name == "season":
            return self._send(season(league))
        self.send_error(404, "not found")


def serve(host="127.0.0.1", port=8770, demo=False):
    Handler.demo = demo or os.environ.get("SLEEPER_GM_DEMO") == "1"
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"sleeper-gm web on http://{host}:{port}" + (" (demo data)" if Handler.demo else ""))
    httpd.serve_forever()
