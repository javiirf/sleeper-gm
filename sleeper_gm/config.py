"""Where things live, and the user's settings. Nothing personal is ever stored in the repo:
config and token sit in ~/.config/sleeper-gm, logs and lineup history in
~/.local/state/sleeper-gm, downloaded data in ~/.cache/sleeper-gm (XDG paths, overridable
with SLEEPER_GM_CONFIG_DIR / SLEEPER_GM_STATE_DIR / SLEEPER_GM_CACHE_DIR)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path


def _xdg(var, default):
    return Path(os.environ.get(var) or Path.home() / default)


CONFIG_DIR = Path(os.environ.get("SLEEPER_GM_CONFIG_DIR") or _xdg("XDG_CONFIG_HOME", ".config") / "sleeper-gm")
STATE_DIR = Path(os.environ.get("SLEEPER_GM_STATE_DIR") or _xdg("XDG_STATE_HOME", ".local/state") / "sleeper-gm")
CACHE_DIR = Path(os.environ.get("SLEEPER_GM_CACHE_DIR") or _xdg("XDG_CACHE_HOME", ".cache") / "sleeper-gm")
CONFIG_FILE = CONFIG_DIR / "config.json"
TOKEN_FILE = CONFIG_DIR / "token"
_live_lock = threading.Lock()

DEFAULTS = {
    "leagues": "auto",              # "auto" = every in-season Lock-In NBA league you own, or a list of league ids
    "live": False,                  # writes happen only when this is true AND the run asks for --live
    "moves": True,                  # free-agent adds, swaps and waiver claims
    "max_claim_writes_per_day": 6,   # submit, update and cancel attempts per league, ET day
    "max_claims": 3,                # bot claims pending per league, separate from immediate moves
    "max_transactions_per_day": 2,  # per league, IR-forced drops included
    "protect_top": 8,               # never drop your best N players (by rest-of-season value)
    "protect_ros": 30.0,            # never drop anyone worth this many fantasy points per game
    "brief_at_et": "09:30",         # first run after this (US Eastern) each day sends the daily brief
    "web_url": None,                # your private link to the phone app, added to the brief
    "chat": {"command": None, "timeout": 90, "history": 24, "per_hour": 30},
    "notify": {
        "command": None,            # a command that gets the message on stdin, e.g. a script of yours
        "telegram_chat_id": None,   # with SLEEPER_GM_TELEGRAM_TOKEN in the environment
        "ntfy_topic": None,         # https://ntfy.sh/<topic>
    },
}


def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_FILE.exists():
        user = json.loads(CONFIG_FILE.read_text())
        notify = {**cfg["notify"], **(user.get("notify") or {})}
        chat = {**cfg["chat"], **(user.get("chat") or {})}
        cfg.update(user)
        cfg["notify"] = notify
        cfg["chat"] = chat
    return cfg


def save(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=CONFIG_DIR, prefix="config-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(cfg, indent=2) + "\n")
            stream.flush()
            os.replace(temporary, CONFIG_FILE)
        finally:
            temporary.unlink(missing_ok=True)


def set_live(live):
    """The CLI and owner chat share one persisted live switch."""
    if not isinstance(live, bool):
        raise ValueError("live must be a boolean")
    with _live_lock:
        cfg = load()
        cfg["live"] = live
        save(cfg)
    return live


def league_state(league_id):
    d = STATE_DIR / str(league_id)
    (d / "logs").mkdir(parents=True, exist_ok=True)
    return d
