"""Tell the user what the manager did. Every channel is optional; a failed message never
fails a run."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo


def plan_line(row):
    """One brief line, also used by status; all times are league-day Eastern."""
    plan = row.get("plan")
    if plan is None:
        return ""
    parts = []
    for look in plan["looks"]:
        name = look["name"].split()[-1]
        if look.get("tip_ms") is not None:
            dt = datetime.fromtimestamp(look["tip_ms"] / 1000, ZoneInfo("America/New_York"))
            hour = str(dt.hour % 12 or 12) + (f":{dt.minute:02}" if dt.minute else "")
            when = dt.strftime("%a ") + hour + ("pm" if dt.hour >= 12 else "am")
        else:
            when = datetime.fromisoformat(look["date"]).strftime("%a")
        bar = "final" if look["terminal"] else f"lock at {look['lock_above']:.1f}+"
        parts.append(f"{name} {when} ({bar})")
    fallback = "lock this score" if plan.get("locking") else "hold; no further looks"
    return f"{row['slot']}: " + (", then ".join(parts) if parts else fallback)


def crowd_line(signal):
    """A separate crowd opinion, never the manager's recommendation."""
    if not signal or signal.get("n", 0) <= 0:
        return ""
    share = signal.get("lock_share")
    if share is None:
        return "Sleeper chat: no lock/pass votes"
    votes = signal["lock"] + signal["pass"]
    return f"Sleeper chat: {int(share * 100 + 0.5)}% lock ({votes} {'vote' if votes == 1 else 'votes'})"


def send(cfg, text, log=None):
    """Try every configured channel independently; print when none delivered. Returns True if any did."""
    from .api import redact
    text = redact(text)
    n = cfg.get("notify") or {}
    sent = False

    def attempt(name, fn):
        nonlocal sent
        try:
            fn()
            sent = True
        except Exception as e:
            if log:
                log({"notify_error": f"{name}: {redact(repr(e))}"})

    def command():
        r = subprocess.run(n["command"], input=text.encode(), shell=True, timeout=30, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"exit {r.returncode}")

    def telegram():
        tg = os.environ["SLEEPER_GM_TELEGRAM_TOKEN"]
        body = json.dumps({"chat_id": n["telegram_chat_id"], "text": text[:4000], "disable_web_page_preview": True}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tg}/sendMessage", data=body,
                                                      headers={"content-type": "application/json"}), timeout=15)

    def ntfy():
        urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{n['ntfy_topic']}", data=text.encode()), timeout=15)

    if n.get("command"):
        attempt("command", command)
    if os.environ.get("SLEEPER_GM_TELEGRAM_TOKEN") and n.get("telegram_chat_id"):
        attempt("telegram", telegram)
    if n.get("ntfy_topic"):
        attempt("ntfy", ntfy)
    if not sent:
        print(text)
    return sent
