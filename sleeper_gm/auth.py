"""The Sleeper session token. It grants full access to the account, so it is only ever
read from SLEEPER_TOKEN or a 0600 file, never printed, logged or sent anywhere but sleeper.com.

Getting it: log into sleeper.com in a browser, open DevTools > Application > Local Storage >
https://sleeper.com, copy the value of `token`, then run `sleeper-gm login` and paste it.
Or, with a Chromium started with --remote-debugging-port and a sleeper.com tab logged in,
`sleeper-gm login --from-browser [--cdp http://127.0.0.1:9222]` reads it from that tab.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.request

from . import config


JWT = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_last = None


def _clean(tok):
    tok = (tok or "").strip()
    if tok.startswith('"'):
        tok = json.loads(tok).strip()
    if not JWT.match(tok):
        raise RuntimeError("that is not a Sleeper token (expected a JWT: three base64url parts)")
    return tok


def _cached():
    return _last


def token():
    global _last
    env = os.environ.get("SLEEPER_TOKEN")
    try:
        raw = env if env else config.TOKEN_FILE.read_text()
    except FileNotFoundError:
        raise RuntimeError("no Sleeper token: run `sleeper-gm login` (or set SLEEPER_TOKEN)") from None
    _last = _clean(raw)
    return _last


def claims(tok=None):
    """The token's public claims (user_id, display_name, exp). Not a secret by themselves."""
    p = (token() if tok is None else _clean(tok)).split(".")[1]
    try:
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except ValueError:
        raise RuntimeError("that is not a Sleeper token (its claims do not decode)") from None


def save(tok):
    tok = _clean(tok)
    c = claims(tok)  # fails loudly on something that is not a Sleeper JWT, before touching the old file
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.TOKEN_FILE.with_name(".token.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, tok.encode())
    finally:
        os.close(fd)
    os.replace(tmp, config.TOKEN_FILE)
    os.chmod(config.TOKEN_FILE, 0o600)
    return c


def from_browser(cdp="http://127.0.0.1:9222"):
    import websocket  # optional dependency: pip install "sleeper-gm[browser]"
    from urllib.parse import urlparse
    def matching(t):
        try:
            url = urlparse(t.get("url", ""))
            return t.get("type") == "page" and url.scheme == "https" and url.hostname == "sleeper.com" and url.port in (None, 443)
        except ValueError:
            return False

    tabs = [t for t in json.load(urllib.request.urlopen(f"{cdp}/json")) if matching(t)]
    if not tabs:
        urllib.request.urlopen(urllib.request.Request(f"{cdp}/json/new?https://sleeper.com/login", method="PUT"))
        raise RuntimeError("opened sleeper.com/login in that browser: log in, then run this again")
    for tab in tabs:
        ws = websocket.create_connection(tab["webSocketDebuggerUrl"], suppress_origin=True, timeout=10)
        try:
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": "localStorage.getItem('token')", "returnByValue": True}}))
            while True:
                r = json.loads(ws.recv())
                if r.get("id") == 1:
                    break
        finally:
            ws.close()
        tok = ((r.get("result") or {}).get("result") or {}).get("value")
        if tok:
            return save(tok)
    raise RuntimeError("no sleeper.com tab is logged in")


def login_interactive():
    if sys.stdin.isatty():
        import getpass
        tok = getpass.getpass("Paste your Sleeper token (input hidden): ")
    else:
        tok = sys.stdin.read()
    return save(tok)
