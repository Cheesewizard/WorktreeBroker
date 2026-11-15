"""Prune the broker's run data so it stops growing forever.

Without it, captures, verify builds, sync previews and session files pile up (hundreds of MB within a week), the
dashboard re-reads every session file every 2 s, and git refs pin every published snapshot. This keeps what is recent
and deletes the rest:

| What | Kept |
| --- | --- |
| captures (`.runs/<route>/captures`, with their `layout/*.json`) | newest 60 per route, and anything from the last day |
| verify work folders (`.runs/verify/*`) | used in the last 7 days |
| sync previews (`.sync/previews/*`) | last 3 days |
| published snapshots (`.sync/published/*` and their `refs/worktree-broker/snapshots/<id>`) | last 14 days |
| player runs (`.runs/<route>/<run id>/`) | last 7 days, and any still running |
| session files (`.sessions/*.json`) | everything but Closed ones older than a day and any untouched for 7 days |

`run()` is cheap and throttled to once an hour (a marker file), so every broker can call it at start. Nothing outside
the broker's own folders is touched except the snapshot refs it created itself.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

from broker_state import HERE, REPOSITORY

RUNS, SYNC, SESSIONS = HERE / ".runs", HERE / ".sync", HERE / ".sessions"
MARKER = RUNS / "housekeeping.stamp"
HOUR, DAY = 3600, 86400
KEEP_CAPTURES = 60


def _age(path, now):
    try:
        return now - path.stat().st_mtime
    except OSError:
        return 0


def _remove(path):
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return 1
    except OSError:
        return 0


def _captures(now):
    removed = 0
    for folder in RUNS.glob("*/captures"):
        shots = sorted(folder.glob("*.png"), key=lambda path: path.stat().st_mtime, reverse=True)
        for shot in shots[KEEP_CAPTURES:]:
            if _age(shot, now) > DAY:
                removed += _remove(shot)
                (folder / "layout" / (shot.stem + ".json")).unlink(missing_ok=True)
    return removed


def _old_children(folder, max_age, now, keep=lambda path: False):
    removed = 0
    for child in folder.glob("*") if folder.is_dir() else ():
        if child.is_dir() and _age(child, now) > max_age and not keep(child):
            removed += _remove(child)
    return removed


def _published(now):
    removed = 0
    for folder in (SYNC / "published").glob("*") if (SYNC / "published").is_dir() else ():
        manifest = folder / "manifest.json"
        try:
            created = json.loads(manifest.read_text(encoding="utf-8")).get("created_at", 0)
        except (OSError, ValueError):
            created = 0
        if now - (created or now - _age(folder, now)) > 14 * DAY:
            subprocess.run(["git", "-C", str(REPOSITORY), "update-ref", "-d", f"refs/worktree-broker/snapshots/{folder.name}"],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            removed += _remove(folder)
    return removed


def _running(run_folder):
    try:
        from broker_state import player_running

        return player_running(json.loads((run_folder / "run.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        return False


def _player_runs(now):
    removed = 0
    for run in RUNS.glob("*/*/run.json"):
        folder = run.parent
        if _age(folder, now) > 7 * DAY and not _running(folder):
            removed += _remove(folder)
    return removed


def _sessions(now):
    removed = 0
    for file in SESSIONS.glob("*.json"):
        try:
            record = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
        updated = record.get("updated_at") or (now - _age(file, now))
        if (record.get("state") == "Closed" and now - updated > DAY) or now - updated > 7 * DAY:
            removed += _remove(file)
    return removed


def run(force=False, now=None):
    """Prune once an hour at most. Returns {what: items removed}, or None when skipped by the throttle."""
    now = time.time() if now is None else now
    if not force and MARKER.exists() and _age(MARKER, now) < HOUR:
        return None
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(str(now), encoding="utf-8")
    return {
        "captures": _captures(now),
        "verify": _old_children(RUNS / "verify", 7 * DAY, now),
        "previews": _old_children(SYNC / "previews", 3 * DAY, now),
        "published": _published(now),
        "player_runs": _player_runs(now),
        "sessions": _sessions(now),
    }


if __name__ == "__main__":
    print(json.dumps(run(force=True), indent=2))
