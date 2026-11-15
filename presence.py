"""Which AI chats are connected, from any MCP client, with no hooks needed.

Every chat starts its own `shared-editor serve`, so each server writes one small file in
`.runs/servers/<pid>.json`: its worktree, the client that started it (MCP's handshake names it:
"claude-code", "codex-mcp-client", "cursor"...), when it last made a tool call and whether a call is running. A
Claude Code chat in one worktree and a Codex chat in another show up the same way. Hooks (`hook.py`) are optional: when
a client has them, they add turn states such as "Needs you", matched to a server by their shared parent process.

A file whose process has gone (or whose pid was reused: the start time is checked) is removed on the next listing.
"""

import json
import os
import time

from broker_state import HERE

CLIENT_NAMES = {
    "claude-code": "Claude Code", "claude": "Claude Code", "claude-ai": "Claude",
    "codex-mcp-client": "Codex", "codex": "Codex",
    "cursor": "Cursor", "cursor-vscode": "Cursor", "windsurf": "Windsurf", "visual studio code": "VS Code",
    "vscode": "VS Code", "zed": "Zed", "gemini-cli-mcp-client": "Gemini CLI", "gemini-cli": "Gemini CLI",
    "cline": "Cline", "continue": "Continue", "goose": "Goose", "shared-editor": "shared-editor call",
}

_record = None
_busy = 0


def client_label(name):
    """A readable client name, e.g. 'codex-mcp-client' -> 'Codex'; unknown names are shown as given."""
    if not name:
        return "AI chat"
    return CLIENT_NAMES.get(name.strip().lower(), name)


def ancestors(pid=None, depth=6):
    """Parent process ids, nearest first. Hooks and servers started by the same chat share one of these."""
    # One process-table snapshot (procs.py, a few ms) instead of psutil's parents(): that rebuilds Windows' whole
    # parent map once per level, ~18 ms each, and the session hook runs on every prompt and stop.
    try:
        import procs

        table = procs.processes()
        chain, current = [], pid or os.getpid()
        while len(chain) < depth:
            info = table.get(current)
            parent = info["ppid"] if info else 0
            # A reused pid can point at an unrelated, newer "parent": a real parent started before its child.
            if not parent or parent not in table or (table[parent]["created"] and info["created"]
                                                     and table[parent]["created"] > info["created"]):
                break
            chain.append(parent)
            current = parent
        return chain
    except Exception:  # noqa: BLE001 - presence is best effort; fall back to psutil
        try:
            import psutil

            return [parent.pid for parent in psutil.Process(pid or os.getpid()).parents()[:depth]]
        except Exception:  # noqa: BLE001
            return []


def _folder():
    return HERE / ".runs" / "servers"


def _path(pid):
    return _folder() / f"{pid}.json"


def _write():
    if _record is None:
        return
    try:
        folder = _folder()
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f"{_record['pid']}.json.tmp"
        temporary.write_text(json.dumps(_record), encoding="utf-8")
        for attempt in range(5):
            try:
                temporary.replace(_path(_record["pid"]))
                return
            except PermissionError:  # Windows: a dashboard is reading the file this instant
                time.sleep(0.02 * (attempt + 1))
    except OSError:
        pass  # presence must never break the server


def register(route_name, worktree):
    """Called once when the server starts."""
    global _record
    try:
        import psutil

        started = psutil.Process().create_time()
    except Exception:  # noqa: BLE001
        started = time.time()
    _record = {"pid": os.getpid(), "started": started, "ancestors": ancestors(), "worktree": str(worktree),
               "route": route_name, "client": None, "client_version": None, "last_call": None, "last_tool": None,
               "busy": False, "calls": 0}
    _write()


def set_client(name, version=None):
    if _record is not None and name and _record.get("client") != name:
        _record.update(client=name, client_version=version)
        _write()


def call_started(tool):
    global _busy
    if _record is None:
        return
    _busy += 1
    _record.update(busy=True, last_tool=tool, last_call=time.time(), calls=_record["calls"] + 1)
    _write()


def call_finished():
    global _busy
    if _record is None:
        return
    _busy = max(0, _busy - 1)
    _record.update(busy=_busy > 0, last_call=time.time())
    _write()


def unregister():
    try:
        if _record is not None:
            _path(_record["pid"]).unlink(missing_ok=True)
    except OSError:
        pass


def current():
    """This process's own record (the server's), or None."""
    return _record


def live():
    """Records of servers that are still running; stale files are removed."""
    folder = _folder()
    records = []
    try:
        import psutil
    except ImportError:
        psutil = None
    for path in folder.glob("*.json") if folder.is_dir() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        alive = True
        if psutil is not None:
            try:
                alive = abs(psutil.Process(record["pid"]).create_time() - record["started"]) < 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
                alive = False
        if alive:
            records.append(record)
        else:
            try:
                path.unlink()
            except OSError:
                pass
    return records
