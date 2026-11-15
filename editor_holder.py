"""Who holds the main editor's call lock right now, and the time limit on every broker call.

The broker runs one editor call at a time (`broker.editor_turn`). Without a time limit, a call caught by a domain reload
makes Unity MCP wait about 30 minutes for an answer that never comes, with the lock held and every other chat told only
"still busy". So:
- every call has a hard limit (`limit_for`); when it runs out the call is cancelled, the lock is released and the broker
  restarts its Unity MCP connection, so the next call starts clean;
- the holder writes `.runs/<route>/editor-holder.json` (client, worktree, tool, since, limit) while it holds the lock, so a
  waiter, the dashboard and `broker_status` can name who is using the editor and when the broker will cut it off.

The lock itself is an OS file lock, so a dead holder can't keep it; the holder file is ignored once its process is gone.
"""

import json
import os
import time

from broker_state import HERE

# Seconds a single tool call may take. Defaults assume execute_code calls are kept short (under ~20 s).
DEFAULT_LIMIT = 240
LIMITS = {
    "run_tests": 900, "get_test_job": 120,
    "refresh_unity": 420, "manage_build": 1800, "manage_packages": 600,
    "verify": 120, "verify_status": 120,  # these poll; the work itself runs in the background
    "ui_capture": 300, "ui_live_reload": 180,
}


def limit_for(tool):
    return LIMITS.get(tool, DEFAULT_LIMIT)


def _path(route_name):
    return HERE / ".runs" / route_name / "editor-holder.json"


def claim(route_name, worktree, tool):
    try:
        import presence

        me = presence.current() or {}
        record = {"pid": os.getpid(), "client": presence.client_label(me.get("client")), "worktree": str(worktree),
                  "tool": tool, "since": time.time(), "limit": limit_for(tool)}
        path = _path(route_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
    except Exception:  # noqa: BLE001 - naming the holder must never break the call
        pass


def release(route_name):
    try:
        path = _path(route_name)
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("pid") == os.getpid():
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def current(route_name="main"):
    """The live holder's record, or None (no holder, or its process has exited)."""
    try:
        record = json.loads(_path(route_name).read_text(encoding="utf-8"))
        import psutil

        if not psutil.pid_exists(record["pid"]):
            return None
        return record
    except (OSError, ValueError, KeyError, ImportError):
        return None


def describe(record, now=None):
    """'Claude Code in MyGame-core-sim is running execute_code (2 min so far; the broker stops it at 4 min)'."""
    now = time.time() if now is None else now
    held = int(now - record["since"])
    name = os.path.basename(record["worktree"].rstrip("\\/"))

    def span(seconds):
        return f"{seconds} s" if seconds < 90 else f"{round(seconds / 60)} min"

    return (f"{record['client']} in {name} is running {record['tool']} ({span(held)} so far; the broker stops it at "
            f"{span(record['limit'])})")
