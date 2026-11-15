"""The shared editor at a glance, for the dashboard: Play mode, last broker use, last guard action."""

import json
import time

import activity
from broker_state import EDITOR_FILES

GUARD_LOG = EDITOR_FILES / "editor-guard.jsonl"


def _last_guard_action():
    try:
        with GUARD_LOG.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 4096))
            lines = handle.read().decode("utf-8", errors="replace").strip().splitlines()
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None


def read():
    """{"reachable", "play_mode", "compiling", "last_use", "last_use_by", "guard"} without blocking more than ~8 s."""
    state = {"reachable": False, "play_mode": None, "compiling": False}
    state["last_use"], state["last_use_by"] = activity.last_editor_use()
    state["guard"] = _last_guard_action()
    try:
        import compile_status
        import lease
        from live_reload_tool import main_worktree

        import editor_holder

        state["lease"] = lease.current()
        state["holder"] = editor_holder.current("main")
        state["compile"] = compile_status.summary(main_worktree())
        status = compile_status.read_status()
        state["compiled_at"] = status["time"] if status else None
        state["compile_files"] = ([compile_status.describe_file(row)
                                   for row in compile_status.broken_files(main_worktree(), status)]
                                  if status and state["compile"] else [])
    except Exception:  # noqa: BLE001 - the dashboard just shows less
        state.setdefault("lease", None)
        state.setdefault("compile", None)
        state.setdefault("compile_files", [])
    try:
        from live_reload_tool import Pipeline, main_worktree

        pipeline = Pipeline(main_worktree())
        reply = pipeline.request("/api/exec", {"command": "editor status", "parameters": {}}, timeout=8)
        result = reply.get("result") if isinstance(reply, dict) else None
        if isinstance(result, dict):
            state.update(reachable=True, play_mode=result.get("playMode"),
                         compiling=bool(result.get("compiling") or result.get("domainReloadInProgress")))
    except Exception:  # noqa: BLE001 - editor closed, busy or reloading: the dashboard just says so
        pass
    return state


def stop_play_mode():
    """Free the editor: end any chat's lease and stop Play mode (the editor stays open)."""
    import lease
    from live_reload_tool import Pipeline, main_worktree

    ended = lease.release(force=True)

    reply = Pipeline(main_worktree()).request("/api/exec", {"command": "editor.playmode stop", "parameters": {}}, timeout=15)
    if not isinstance(reply, dict):
        raise RuntimeError("the editor gave no answer (it may be reloading scripts); try again in a moment")
    if not reply.get("success"):
        raise RuntimeError(str(reply.get("errorDetails") or reply.get("error") or reply))
    activity.record("dashboard", "", "free_editor", 0, True)
    result = str(reply.get("result") or "")
    stopped = "The editor was not in Play mode" if "Already" in result else "Stopped Play mode"
    return f"{stopped}{' and ended the lease' if ended else ''}: the editor is free."


def describe(state, play_since, now=None):
    """One plain line, e.g. 'Main editor: in Play mode 12 min · last broker use 3 min ago · guard stopped it 20:49 (idle)'."""
    now = time.time() if now is None else now

    def ago(stamp):
        seconds = max(0, int(now - stamp))
        return "just now" if seconds < 60 else f"{seconds // 60} min ago" if seconds < 3600 else f"{seconds // 3600} h ago"

    if not state["reachable"]:
        head = "Main editor: not answering (closed, busy or reloading)"
    elif state["compiling"]:
        head = "Main editor: compiling"
    elif state["play_mode"] in {"playing", "paused"}:
        minutes = int((now - play_since) // 60) if play_since else 0
        head = f"Main editor: in Play mode{' (paused)' if state['play_mode'] == 'paused' else ''} {minutes} min"
    else:
        head = "Main editor: free (not in Play mode)"
    parts = [head]
    held = state.get("lease")
    if held:
        left = max(0, int(held["expires"] - now))
        parts.append(f"leased by {held['holder']} ({held['reason']}, {left // 60} min left)")
    holder = state.get("holder")
    if holder:
        import editor_holder

        parts.append("in use: " + editor_holder.describe(holder, now))
    if state["last_use"]:
        parts.append(f"last broker use {ago(state['last_use'])}")
    guard = state.get("guard")
    if guard and now - guard.get("time", 0) < 6 * 3600:
        when = time.strftime("%H:%M", time.localtime(guard["time"]))
        kind = {"stop": "stopped Play mode", "warn": "warned it will stop Play mode"}.get(
            guard.get("action"), "turned run-in-background on")
        reason = guard.get("reason", "").split(":")[0]
        parts.append(f"guard {kind} at {when} ({reason})")
    return " · ".join(parts)
