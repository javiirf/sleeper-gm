"""Local owner instructions. The runner never treats expired weekly locks as active."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


def load(directory, season=None, week=None, now_ms=None):
    try:
        rows = json.loads((Path(directory) / "directives.json").read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    active = []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in {"keep", "avoid", "lock_at", "note"}:
            continue
        expires = row.get("expires_at")
        if expires is not None and (not isinstance(expires, (int, float)) or expires <= now_ms):
            continue
        if row["kind"] == "lock_at":
            if expires is None or row.get("season") is None or row.get("week") is None:
                continue
            if season is not None and str(row["season"]) != str(season):
                continue
            if week is not None and str(row["week"]) != str(week):
                continue
        active.append(row)
    return active


def fingerprint(directory):
    try:
        return hashlib.sha256((Path(directory) / "directives.json").read_bytes()).hexdigest()
    except OSError:
        return ""
