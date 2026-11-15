"""Local state shared by the dashboard, lifecycle hooks, and MCP."""

import json
from hashlib import sha256
from pathlib import Path
import subprocess
import time
from uuid import uuid4



HERE = Path(__file__).resolve().parent
RUNS = HERE / ".runs"
SESSIONS = HERE / ".sessions"
CONTROLS = HERE / ".controls"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


ROUTES_HELP = ("Run `python install.py config` (it writes routes.json and project.json from this checkout), or copy "
               "routes.example.json to routes.json next to broker.py and set the main route's worktree (your project's "
               "checkout) and development_branch.")

_DEFAULT_ROUTES = None


def default_routes():
    """The routes used when there is no routes.json: one main route, the repository this broker folder sits in, pinned
    to the branch its main checkout is on now, so a new install runs with no config files. routes.json is only
    needed for player routes or to pin a branch other than the current one."""
    global _DEFAULT_ROUTES
    if _DEFAULT_ROUTES is None:
        try:
            # Installed as a git submodule (tools/WorktreeBroker in the game's repository), the broker's own checkout
            # is not the project: the superproject is.
            superproject = subprocess.check_output(
                ["git", "-C", str(HERE), "rev-parse", "--show-superproject-working-tree"],
                text=True, timeout=5, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).strip()
            common = subprocess.check_output(
                ["git", "-C", superproject or str(HERE), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                text=True, timeout=5, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).strip()
            main = Path(common).resolve().parent
            branch = subprocess.check_output(
                ["git", "-C", str(main), "symbolic-ref", "-q", "--short", "HEAD"],
                text=True, timeout=5, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).strip()
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError(f"No routes.json in {HERE}, and this folder isn't inside a git checkout on a branch, "
                               f"so the main checkout can't be worked out. {ROUTES_HELP}") from None
        _DEFAULT_ROUTES = [{"name": "main", "kind": "editor", "worktree": main.as_posix(),
                            "development_branch": branch, "default": True}]
    return [dict(route) for route in _DEFAULT_ROUTES]


def routes():
    """routes.json as a list, or default_routes() when there is none; a clear message when it is broken."""
    path = HERE / "routes.json"
    try:
        value = read_json(path)
    except FileNotFoundError:
        return default_routes()
    except ValueError as error:
        raise RuntimeError(f"routes.json is not valid JSON ({error}). {ROUTES_HELP}") from None
    if not isinstance(value, list):
        raise RuntimeError(f"routes.json must be a JSON list of routes. {ROUTES_HELP}")
    return value


def main_route():
    """The main checkout's route: the one named "main", which must also name the development branch."""
    found = [route for route in routes() if route.get("name") == "main"]
    if len(found) != 1 or not found[0].get("development_branch") or not found[0].get("worktree"):
        raise RuntimeError(f"routes.json needs exactly one route named \"main\" with worktree and development_branch. {ROUTES_HELP}")
    return found[0]


def _main_route():
    try:
        return main_route()
    except RuntimeError:
        return {}


def _project():
    """project.json: what differs between Unity projects (see project.example.json). Missing file: nothing optional on."""
    try:
        return read_json(HERE / "project.json")
    except FileNotFoundError:
        return {}
    except ValueError as error:
        raise RuntimeError(f"project.json in {HERE} is not valid JSON ({error}). Fix it, or delete it to run with "
                           "defaults (`python install.py config` writes a fresh one).") from None


# Everything project-specific comes from config, never from code. routes.json's main route gives the repository
# ("worktree"; default: the checkout this broker folder sits in, tools/WorktreeBroker). project.json gives the rest:
# the name the dashboard shows (default: the repository's folder name), the MCP server name, the editor C# hooks, the
# verify checks and the player. A feature whose section is missing is reported as not configured.
_MAIN = _main_route()
PROJECT = _project()
REPOSITORY = Path(_MAIN.get("worktree") or HERE.parent.parent).resolve()
PROJECT_NAME = PROJECT.get("name") or REPOSITORY.name
MCP_SERVER = PROJECT.get("mcp_server") or "unity_broker"
# The direct Unity MCP server the broker replaces (blocked by block_direct_unity.py, switched off for Codex).
DIRECT_UNITY_MCP = PROJECT.get("unity_mcp", {}).get("direct_server") or "unityMCP"
# The folder the editor package (unity-package/) and the broker exchange files through: compile-status.json and
# editor-guard.jsonl from the editor, editor-last-activity from the broker. Under Library, so never committed, and found
# from the main checkout, so the broker folder can live anywhere.
EDITOR_FILES = REPOSITORY / "Library" / "WorktreeBroker"


def fill(template, **values):
    """Put values into a config template's {name} slots. Not str.format, so C# braces in snippets need no escaping."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def write_json(path, value):
    """Atomic write. On Windows the replace fails while another process (the dashboard, every 2 s) has the file open,
    so retry briefly, and never leave the temporary file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    try:
        for attempt in range(20):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.025)
    finally:
        temporary.unlink(missing_ok=True)


_WORKTREE_CACHE = {}
WORKTREE_CACHE_SECONDS = 10


def worktrees(repository=REPOSITORY):
    """The repository's git worktrees. Cached for 10 s: the dashboard asks every 2 s, the git call is about half of each
    refresh, and worktrees change a few times a day. Callers get copies, so they can't edit the cache."""
    key = str(repository)
    cached = _WORKTREE_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < WORKTREE_CACHE_SECONDS:
        return [dict(row) for row in cached[1]]
    rows = _list_worktrees(repository)
    _WORKTREE_CACHE[key] = (time.monotonic(), rows)
    return [dict(row) for row in rows]


def _list_worktrees(repository):
    output = subprocess.check_output(
        ["git", "-C", str(repository), "worktree", "list", "--porcelain", "-z"],
        text=True, encoding="utf-8", timeout=5, stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    rows = []
    all_routes = routes()
    main = next((Path(route["worktree"]).resolve() for route in all_routes
                 if route.get("development_branch")), None)
    for record in output.strip("\0").split("\0\0"):
        fields = dict(item.partition(" ")[::2] for item in record.split("\0"))
        if "worktree" not in fields:
            continue
        path = Path(fields["worktree"]).resolve()
        branch = fields.get("branch", "").removeprefix("refs/heads/")
        name = path.parent.name if path.parent.parent.name == "worktrees" else path.name
        rows.append({
            "path": str(path), "name": name, "branch": branch or fields.get("HEAD", "")[:8],
            "is_main": path == main,
        })
    return rows


def player_running(info):
    import psutil  # lazy: the session hook imports this module and must start fast

    try:
        return psutil.Process(info["pid"]).create_time() == info["process_started_at"]
    except psutil.NoSuchProcess:
        return False


def run_status(directory):
    info = read_json(directory / "run.json")
    info["running"] = player_running(info)
    log = Path(info["log"])
    try:
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 16000))
            info["log_tail"] = stream.read().decode("utf-8", errors="replace").splitlines()[-35:]
    except FileNotFoundError:
        info["log_tail"] = []
    return info


def session_rows():
    for file in SESSIONS.glob("*.json"):
        session = read_json(file)
        marker_path = control_path(session["id"])
        if marker_path.exists():
            marker = read_json(marker_path)
            if session.get("turn_id") == marker["turn_id"]:
                session["state"] = {
                    "Working": "Pausing", "Interrupted": "Paused",
                }.get(session["state"], session["state"])
        yield session


def control_path(session_id):
    return CONTROLS / (sha256(session_id.encode()).hexdigest() + ".json")


def argument(arguments, name):
    try:
        index = next(i for i, value in enumerate(arguments) if value.casefold() == name.casefold())
        return arguments[index + 1]
    except (StopIteration, IndexError):
        return None


def snapshot(rows=None):
    import psutil
    from windows import visible_windows

    rows = worktrees() if rows is None else rows
    known = {Path(row["path"]).resolve(): dict(row, windows=[], sessions=[], servers=[]) for row in rows}
    for item in (HERE / ".sync" / "runtimes").glob("*/manifest.json"):
        runtime = read_json(item)
        path = Path(runtime["worktree"]).resolve()
        if (path / "Assets").is_dir():
            known[path] = {
                "path": str(path), "name": path.name, "branch": "Disposable preview",
                "windows": [], "sessions": [], "servers": [],
            }
    for session in session_rows():
        path = Path(session.get("feature_worktree", session["worktree"])).resolve()
        row = known.get(path)
        if row is None and session.get("external_workspace"):
            row = known.setdefault(path, {
                "path": str(path), "name": path.name, "branch": "Chat workspace",
                "windows": [], "sessions": [], "servers": [],
            })
        if row is not None:
            row["sessions"].append(session)

    windows = visible_windows()
    # Only read command lines of Unity processes: reading them for every process (often ~1,000) takes many seconds
    # and starves the dashboard's UI thread.
    try:
        import procs
        unity = [(pid, info["created"]) for pid, info in procs.processes().items() if info["name"].lower() == "unity.exe"]
    except OSError:
        unity = [(item.pid, None) for item in psutil.process_iter(["name"], ad_value=None)
                 if (item.info["name"] or "").lower() == "unity.exe"]
    for pid, created in unity:
        try:
            process = psutil.Process(pid)
            process.info = {"create_time": process.create_time()}
            args = process.cmdline() or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        project = argument(args, "-projectPath")
        if not project or "-batchmode" in args or "-assetImportWorker" in args:
            continue
        row = known.get(Path(project).resolve())
        if row is not None and process.pid in windows:
            row["windows"].append(dict(
                windows[process.pid], kind="Editor", started_at=process.info["create_time"],
            ))

    for path in RUNS.glob("*/*/run.json"):
        run = read_json(path)
        row = known.get(Path(run["worktree"]).resolve())
        if row is not None and run["pid"] in windows and player_running(run):
            row["windows"].append(dict(
                windows[run["pid"]], kind="Player", started_at=run["process_started_at"],
                run_id=run["run_id"],
            ))

    import presence
    import lease

    for server in presence.live():
        row = known.get(Path(server["worktree"]).resolve())
        if row is not None:
            row["servers"].append(server)
    try:
        held = lease.current()
    except Exception:  # noqa: BLE001
        held = None
    for row in known.values():
        row["chats"] = chats(row["servers"], row["sessions"], held["holder_pid"] if held else None)
    return list(known.values())


STATE_PRIORITY = ["Needs you", "Working", "Working? (quiet)", "Your turn", "Connected", "Stopped", "Unknown",
                  "Closed"]
SOURCE_LABELS = {"claude": "Claude Code", "codex": "Codex"}


def chats(servers, sessions, lease_pid=None, now=None):
    """One client-neutral list per worktree: every running broker (any MCP client, from presence.py) with its hook
    session's state when one matches through a shared parent process, plus hook sessions that have no broker.
    Each chat: {id, label, state, updated_at, detail, holds_lease, session}."""
    import presence

    now = time.time() if now is None else now
    result, used = [], set()
    for server in sorted(servers, key=lambda s: s.get("started", 0)):
        near = set(server.get("ancestors", [])[:3])
        match = next((s for s in sessions if s["id"] not in used and near & set(s.get("ancestors", []))), None)
        if match:
            used.add(match["id"])
        hook_state = display_state(match, now) if match else None
        if server.get("busy"):
            state = "Needs you" if hook_state == "Needs you" else "Working"
            detail = f"running {server.get('last_tool') or 'a tool'}"
        elif hook_state in {"Needs you", "Working", "Your turn"}:
            state, detail = hook_state, ""
        else:
            state, detail = "Connected", ""
        last = max(server.get("last_call") or server.get("started", 0), (match or {}).get("updated_at", 0))
        if not detail:
            detail = "no tool calls yet" if not server.get("last_call") else f"last tool: {server.get('last_tool')}"
        result.append({"id": f"pid:{server['pid']}", "label": presence.client_label(server.get("client")),
                       "state": state, "updated_at": last, "detail": detail,
                       "holds_lease": lease_pid == server["pid"], "session": match})
    for session in visible_sessions(sessions, now):
        if session["id"] in used:
            continue
        state = display_state(session, now)  # it may be working without the shared tools: keep its state
        label = session.get("title") or SOURCE_LABELS.get(session.get("source"), "AI chat")
        result.append({"id": f"session:{session['id']}", "label": label, "state": state,
                       "updated_at": session.get("updated_at", 0), "detail": "not connected to the broker",
                       "holds_lease": False, "session": session})
    return result


def ai_status(sessions, now=None):
    """The most urgent state among a worktree's sessions ("Needs you" beats "Working" beats ...), with a count.
    Takes hook sessions, or chats from chats() (which already carry a state)."""
    if sessions and "label" in sessions[0]:
        states = [item["state"] for item in sessions]
    else:
        states = [display_state(item, now) for item in visible_sessions(sessions, now)]
    if not states:
        return "No AI session"
    best = min(states, key=STATE_PRIORITY.index)
    count = states.count(best)
    return best if count == 1 else f"{best} ({count})"


def project_root_for(cwd):
    """The project worktree containing cwd, or None (hooks run for every project)."""
    if not cwd:
        return None
    try:
        top, common = subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"],
            text=True, timeout=3, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).splitlines()[:2]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    # Every worktree of the project shares the main checkout's .git directory: one git call, no worktree listing.
    if Path(common.strip()).resolve() != (REPOSITORY / ".git").resolve():
        return None
    return Path(top.strip()).resolve()


HOOK_STATES = {
    "SessionStart": "Idle", "UserPromptSubmit": "Working", "Stop": "Finished",
    "Interrupt": "Interrupted", "SessionEnd": "Closed",
    # Claude Code: a permission prompt or a question waiting on the user.
    "Notification": "Needs you",
}


def record_session(payload, root, source="codex"):
    event = payload.get("hook_event_name")
    session = payload.get("session_id")
    if event not in HOOK_STATES or not session or not payload.get("cwd"):
        return
    cwd = Path(payload["cwd"]).resolve()
    if not cwd.is_relative_to(root):
        return
    key = sha256(session.encode()).hexdigest()
    destination = SESSIONS / f"{key}.json"
    known = read_json(destination) if destination.exists() else {}
    write_json(destination, {
        **known,
        "id": session, "worktree": str(root), "state": HOOK_STATES[event],
        "source": known.get("source", source),
        "turn_id": payload.get("turn_id"),
        "ancestors": _ancestors(),
        "updated_at": time.time(),
    })


def _ancestors():
    try:
        import presence

        return presence.ancestors()
    except Exception:  # noqa: BLE001 - a hook must never fail
        return []


# What the dashboard shows. Raw states come from hooks (above) and from Codex refreshes (session_refresh.py).
QUIET_WORKING_SECONDS = 2 * 3600
# A chat "working" with no news for this long ended without its Stop hook (closed window, crash): call it stopped.
STALE_WORKING_SECONDS = 6 * 3600
HIDE_CLOSED_SECONDS = 3600


def display_state(session, now=None):
    """One plain word for a session: Working, Needs you, Your turn, Stopped, Closed or Unknown."""
    now = time.time() if now is None else now
    raw = session.get("state", "")
    age = now - session.get("updated_at", 0)
    if raw in {"Working", "Pausing"}:
        if age >= STALE_WORKING_SECONDS:
            return "Stopped"
        return "Working" if age < QUIET_WORKING_SECONDS else "Working? (quiet)"
    if raw == "Needs you":
        return "Needs you"
    if raw in {"Finished", "Idle", "Paused"}:
        return "Your turn"
    if raw in {"Interrupted", "Failed", "Unfinished in log", "Active when checked", "Turn open in log"}:
        return "Stopped"
    if raw == "Closed":
        return "Closed"
    return "Unknown"


def visible_sessions(sessions, now=None):
    """Sessions worth listing: everything except ones closed more than a day ago."""
    now = time.time() if now is None else now
    return [item for item in sessions
            if not (item.get("state") == "Closed" and now - item.get("updated_at", 0) > HIDE_CLOSED_SECONDS)]
