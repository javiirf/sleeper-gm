"""Run the manager by itself: every 30 minutes at :10 and :40 (twenty minutes before the
usual NBA tip times). Each run first makes a cheap check and exits when nothing is
happening, and the first run after `brief_at_et` each day sends the daily brief.

Linux with systemd: a user timer (survives logout with `loginctl enable-linger`).
macOS: a launchd agent. Anything else, or --cron: a crontab line.
"""
from __future__ import annotations

import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

NAME = "sleeper-gm"


def _cmd():
    return [sys.executable, "-m", "sleeper_gm.cli", "run", "--live"]


def _systemd_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def _has_systemd():
    return platform.system() == "Linux" and shutil.which("systemctl") and subprocess.run(
        ["systemctl", "--user", "is-system-running"], capture_output=True).returncode in (0, 1)


def _launchd_plist():
    return Path.home() / "Library" / "LaunchAgents" / f"com.{NAME}.plist"


def _systemd_quote(arg):
    """systemd ExecStart= quoting: double quotes, with backslash, quote, % and $ escaped."""
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def _cron_line():
    log = shlex.quote(str(config.STATE_DIR / "cron.log")).replace("%", "\\%")
    cmd = " ".join(shlex.quote(a) for a in _cmd()).replace("%", "\\%")
    return f"10,40 * * * * {cmd} >> {log} 2>&1  # {NAME}"


def _systemd_service(cwd):
    if "\n" in cwd or "\r" in cwd or not Path(cwd).is_absolute():
        raise ValueError("WorkingDirectory must be an absolute path without newlines")
    return ("[Unit]\nDescription=sleeper-gm: Lock-In fantasy basketball manager\nAfter=network-online.target\n\n"
            f"[Service]\nType=oneshot\nWorkingDirectory={cwd}\n"
            f"ExecStart={' '.join(_systemd_quote(a) for a in _cmd())}\nTimeoutStartSec=900\nNice=5\n")


def install(cron=False):
    cwd = str(Path(__file__).resolve().parent.parent)
    if not cron:
        for label, path in (("interpreter", _cmd()[0]), ("working directory", cwd)):
            if any(c in path for c in ("%", "\n", "\r")) or path.endswith("\\"):
                raise ValueError(f"Cannot install scheduler: {label} path contains %, a newline, or ends with a backslash; use --cron")
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not cron and _has_systemd():
        d = _systemd_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{NAME}.service").write_text(_systemd_service(cwd))
        (d / f"{NAME}.timer").write_text(
            "[Unit]\nDescription=sleeper-gm every 30 minutes\n\n"
            "[Timer]\nOnCalendar=*-*-* *:10,40:00\nAccuracySec=30s\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{NAME}.timer"], check=True)
        return (f"installed systemd user timer {NAME}.timer (every :10 and :40).\n"
                "If this machine logs you out, run `loginctl enable-linger` so it keeps running.")
    if not cron and platform.system() == "Darwin":
        p = _launchd_plist()
        p.parent.mkdir(parents=True, exist_ok=True)
        log = str(config.STATE_DIR / "launchd.log")
        with open(p, "wb") as fh:
            plistlib.dump({"Label": f"com.{NAME}", "ProgramArguments": _cmd(), "WorkingDirectory": cwd,
                           "StartCalendarInterval": [{"Minute": 10}, {"Minute": 40}],
                           "StandardOutPath": log, "StandardErrorPath": log}, fh)
        subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
        subprocess.run(["launchctl", "load", str(p)], check=True)
        return f"installed launchd agent {p} (every :10 and :40)"
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    lines = [l for l in cur.splitlines() if f"# {NAME}" not in l] + [_cron_line()]
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
    return "installed crontab line:\n" + _cron_line()


def remove(cron=False):
    out = []
    d = _systemd_dir()
    if (d / f"{NAME}.timer").exists():
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{NAME}.timer"], capture_output=True)
        for f in (f"{NAME}.timer", f"{NAME}.service"):
            (d / f).unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        out.append("removed systemd timer")
    p = _launchd_plist()
    if p.exists():
        subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
        p.unlink()
        out.append("removed launchd agent")
    if shutil.which("crontab"):
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        if f"# {NAME}" in cur:
            subprocess.run(["crontab", "-"], input="\n".join(l for l in cur.splitlines() if f"# {NAME}" not in l) + "\n", text=True)
            out.append("removed crontab line")
    return "\n".join(out) or "nothing was installed"


def status(cron=False):
    if (_systemd_dir() / f"{NAME}.timer").exists():
        r = subprocess.run(["systemctl", "--user", "list-timers", f"{NAME}.timer", "--no-pager"], capture_output=True, text=True)
        return r.stdout.strip()
    if _launchd_plist().exists():
        return f"launchd agent installed: {_launchd_plist()}"
    if shutil.which("crontab") and f"# {NAME}" in subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout:
        return "crontab line installed"
    return "not scheduled: run `sleeper-gm schedule install`"
