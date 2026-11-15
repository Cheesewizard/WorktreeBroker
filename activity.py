"""Who used the shared editor, and when.

Every broker tool call is appended to `.runs/activity.jsonl` (time, route, worktree, tool, seconds, ok). Calls that touch
the main Unity editor also refresh `<project>/Library/WorktreeBroker/editor-last-activity`, a one-line timestamp that the editor guard
(an optional project editor script) and the dashboard read to tell "in use" from "left running". No prompts,
arguments or results are recorded.
"""

import asyncio
import functools
import json
import time

from broker_state import EDITOR_FILES, HERE
import editor_holder
import presence

RUNS = HERE / ".runs"
LOG = RUNS / "activity.jsonl"
LAST_EDITOR = EDITOR_FILES / "editor-last-activity"
# Tools that never touch the Unity editor: they don't keep it "in use".
NON_EDITOR = {"broker_status", "worktree_capabilities", "sync_publish", "sync_list", "sync_preview",
              "bot_start", "bot_status", "preview_patch", "editor_release", "editor_compile_status",
              "verify", "verify_status"}
MAX_LOG_BYTES = 2 * 1024 * 1024


def record(route_name, worktree, tool, seconds, ok, error=None):
    try:
        RUNS.mkdir(parents=True, exist_ok=True)
        now = time.time()
        me = presence.current() or {}
        line = json.dumps({"time": round(now, 3), "route": route_name, "worktree": str(worktree), "tool": tool,
                           "seconds": round(seconds, 3), "ok": ok, "client": me.get("client"), "pid": me.get("pid"),
                           **({"error": error} if error else {})})
        if LOG.exists() and LOG.stat().st_size > MAX_LOG_BYTES:
            LOG.replace(LOG.with_suffix(".old.jsonl"))
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if tool not in NON_EDITOR:
            LAST_EDITOR.parent.mkdir(parents=True, exist_ok=True)
            LAST_EDITOR.write_text(f"{now:.3f} {route_name} {tool}\n", encoding="utf-8")
    except OSError:
        pass  # logging must never break a tool call


def mark_editor_use(route_name, tool):
    """Called when an editor call starts, so a long call counts as use while it runs."""
    try:
        LAST_EDITOR.parent.mkdir(parents=True, exist_ok=True)
        LAST_EDITOR.write_text(f"{time.time():.3f} {route_name} {tool}\n", encoding="utf-8")
    except OSError:
        pass


def last_editor_use():
    """(epoch seconds, "route tool") of the last editor call, or (0, "") if none recorded."""
    try:
        stamp, _, rest = LAST_EDITOR.read_text(encoding="utf-8").strip().partition(" ")
        return float(stamp), rest
    except (OSError, ValueError):
        return 0.0, ""


def compact(result):
    """Plain dict/list results as one compact JSON text item. Left to the MCP library, a dict goes out pretty-printed
    (indent=2) and again as structured content, which some clients show to the model as well: both cost the chat tokens
    for nothing. Content items (text, images) and CallToolResults from Unity MCP pass through unchanged."""
    if isinstance(result, dict) or (isinstance(result, list) and not any(hasattr(item, "type") for item in result)):
        from mcp import types

        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":"), ensure_ascii=False,
                                                                 default=str))]
    return result


def logged(route_name, worktree, on_timeout=None):
    """Decorator for an MCP call handler: records every call, even failing ones, and cuts off any call that runs past
    its limit (editor_holder.limit_for). on_timeout (async) runs after a cut-off, e.g. to restart Unity MCP."""
    def wrap(handler):
        @functools.wraps(handler)
        async def call(name, arguments):
            started = time.perf_counter()
            if name not in NON_EDITOR:
                mark_editor_use(route_name, name)
            presence.call_started(name)
            ok, error = False, None
            limit = editor_holder.limit_for(name)
            try:
                try:
                    async with asyncio.timeout(limit):
                        result = await handler(name, arguments)
                except TimeoutError as error:
                    if on_timeout is not None:
                        await on_timeout()
                    raise RuntimeError(
                        f"{name} did not finish within {limit} s, so the broker stopped waiting and freed the editor "
                        "(Unity may have been reloading). The broker restarted its Unity connection: check the editor "
                        "state (worktree_editor_resource) and retry once. Do not switch to Unity MCP directly; if it "
                        "fails again, stop and tell the user.") from error
                ok = True
                return compact(result)
            except Exception as failure:
                # The message only (e.g. "Main editor is leased by ..."), never arguments or results.
                error = (str(failure) or type(failure).__name__)[:200]
                raise
            finally:
                presence.call_finished()
                record(route_name, worktree, name, time.perf_counter() - started, ok, error)
        return call
    return wrap
