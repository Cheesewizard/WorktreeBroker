"""Worktree-bound Unity MCP and player tools for Windows, for one Unity project (see project.json)."""

import argparse
import asyncio
import ctypes
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import msvcrt
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import psutil
from broker_state import MCP_SERVER, PROJECT, PROJECT_NAME, routes as configured_routes, argument, fill, ai_status, player_running, read_json, run_status, snapshot, worktrees, write_json

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
import worktree_sync
import activity
import editor_holder
import lease
import presence
import compile_status
import verify as verify_tool
import live_reload_tool
import integrate as integrate_tool
import freshness
import commit as commit_tool


HERE = Path(__file__).resolve().parent
INSTANCES = "mcpforunity://instances"
EDITOR_RESOURCES = {"mcpforunity://editor/state", "mcpforunity://project/info"}
NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}


class MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("memory_load", ctypes.c_ulong),
        ("total_physical", ctypes.c_ulonglong),
        ("available_physical", ctypes.c_ulonglong),
        ("total_page_file", ctypes.c_ulonglong),
        ("available_page_file", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong),
        ("available_virtual", ctypes.c_ulonglong),
        ("available_extended_virtual", ctypes.c_ulonglong),
    ]


def memory():
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError()
    return {
        "available_ram_mib": status.available_physical // (1024 * 1024),
        "available_commit_mib": status.available_page_file // (1024 * 1024),
    }


def git(path, *args):
    return subprocess.check_output(
        ["git", "-C", str(path), *args], stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flash
    ).strip()


def load_route(worktree):
    worktree = Path(worktree).resolve()
    runtime = worktree_sync.runtime_route(worktree)
    if runtime:
        raise RuntimeError("The broker only connects to Main Unity. Inspect this preview as a source overlay.")
    root = Path(git(worktree, "rev-parse", "--show-toplevel")).resolve()
    matches = [
        route for route in configured_routes() if Path(route["worktree"]).resolve() == root
    ]
    if not matches and any(Path(row["path"]).resolve() == root for row in worktrees()):
        key = sha256(str(root).casefold().encode()).hexdigest()[:16]
        matches = [{"kind": "source", "name": f"source-{key}"}]
    if not (root / "Assets").is_dir():
        raise RuntimeError(f"{root} has no Assets folder at its root. The broker needs the Unity project at the top of the "
                           "git repository (a project in a subfolder isn't supported yet).")
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one configured Unity worktree for {root}, found {len(matches)} in routes.json.")
    route = matches[0]
    if route["kind"] == "editor" and route["name"] != "main":
        raise RuntimeError("Only the main route may connect to Unity Editor")
    if route.get("development_branch"):
        route["head_path"] = str(Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "HEAD")))
        check_development_branch(route)
    return root, route


def check_development_branch(route):
    expected = route.get("development_branch")
    if not expected:
        return
    head = Path(route["head_path"]).read_text(encoding="ascii").strip()
    if head != f"ref: refs/heads/{expected}":
        raise RuntimeError(f"The main Unity checkout must be on {expected}. Update the main route in routes.json when changing development branches (without a routes.json, the broker pins the branch the checkout was on when it started: restart it).")


def matching_editor(root, instances):
    assets = root / "Assets"
    matches = [
        item for item in instances
        if item.get("status") == "running"
        and item.get("path")
        and Path(item["path"]).resolve() == assets
    ]
    if not matches:
        raise RuntimeError(f"The main Unity editor isn't connected (closed, starting or reloading; no editor for {assets}). "
                           "Wait about a minute and retry once; if it still fails, tell the user rather than retrying.")
    if len(matches) > 1:
        raise RuntimeError(f"{len(matches)} Unity editors have {assets} open; close the extra one.")
    return matches[0]["id"]


async def editor_id(upstream, root):
    result = await upstream.read_resource(INSTANCES)
    payload = json.loads(result.contents[0].text)
    return matching_editor(root, payload["instances"])


PLAYER = PROJECT.get("player", {})
NO_BYPASS = (f'Do not call Unity MCP directly (mcp__unityMCP__*): {PROJECT_NAME} requires the broker. Wait and retry; '
             'if the editor stays busy, stop and tell the user what this message says.')


# MCP tool annotations, so a client that asks before side effects (by readOnlyHint / destructiveHint) can let the reads
# through without a prompt. All broker tools are local (openWorldHint false). Hints, not a guarantee: each client
# decides what to do with them. integrate is read-only because it only writes git objects and a scratch folder.
READ_ONLY_TOOLS = {"broker_status", "worktree_capabilities", "sync_list", "integrate", "integrate_status",
                   "integrate_list", "editor_compile_status", "verify_status", "worktree_freshness", "worktree_target",
                   "worktree_editor_resource", "bot_status"}
DESTRUCTIVE_TOOLS = {"ui_revert"}  # drops every live override in the shared editor, other chats' included
IDEMPOTENT_TOOLS = READ_ONLY_TOOLS | {"ui_revert", "editor_release", "editor_lease", "sync_preview"}


def tool(name, description, schema=NO_ARGS):
    annotations = types.ToolAnnotations(readOnlyHint=name in READ_ONLY_TOOLS,
                                        destructiveHint=name in DESTRUCTIVE_TOOLS,
                                        idempotentHint=name in IDEMPOTENT_TOOLS, openWorldHint=False)
    return types.Tool(name=name, description=description, inputSchema=schema, annotations=annotations)


@asynccontextmanager
async def editor_turn(path, leased=True, tool="a tool", worktree=None):
    """One editor call at a time; with leased, also wait behind another chat's editor lease (lease.py). The holder is
    named in editor-holder.json while it holds the lock (editor_holder.py)."""
    route_name = path.parent.name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        holder = None
        try:
            async with asyncio.timeout(30):
                while True:
                    holder = lease.blocking_lease() if leased else None
                    if holder is not None and lease.seconds_left(holder) > 30:
                        # It can't end within this call's wait, so say so now instead of after 30 s.
                        raise TimeoutError
                    if holder is None:
                        lock.seek(0)
                        try:
                            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            pass
                    await asyncio.sleep(0.1 if holder is None else 0.5)
        except TimeoutError as error:
            if holder is not None:
                raise RuntimeError(f"Main editor is leased by {lease.describe(holder)}. Retry after that, or ask the "
                                   "user (the dashboard's Free the editor button ends a lease).") from error
            busy = editor_holder.current(route_name)
            who = editor_holder.describe(busy) if busy else "another chat's call is running"
            raise RuntimeError(f"The main editor is still busy after 30 seconds: {who}. {NO_BYPASS}") from error
        editor_holder.claim(route_name, worktree or "?", tool)
        if leased:
            lease.touch()  # the holder is using its lease, so it doesn't lapse as idle (lease.py)
        try:
            yield
        finally:
            if leased:
                lease.touch()
            editor_holder.release(route_name)
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def capabilities(root, route):
    verification = {
        "editor": "For views, use ui_live_reload on tagged [MethodReload] rebuild methods (sub-second). Use Fast Script Reload for other supported C# body edits in Play Mode. Use Unity compilation or asset import for structural or asset changes.",
        "source": "Keep editing here. For views, tag rebuild methods [MethodReload] and use ui_live_reload to see your saved file in Main Unity's running game (sub-second, no checkout writes). Publish saved edits and use sync_preview to inspect another snapshot. Other Unity Editor access is reserved for Main Unity.",
        "player": "bot_start launches the configured build and applies the saved startup patch when configured. bot_status reports the run and patch results.",
    }
    return {
        "worktree": str(root),
        "target": route["kind"],
        "development_branch": route.get("development_branch"),
        "edit_policy": "Edit saved files in this worktree directly. A Git commit is optional.",
        "verification": verification[route["kind"]],
        "addressables_required": False,
        # Drift from the development line, so a chat knows at the start whether its base is old (freshness.py).
        "freshness": freshness.check(root)["summary"] if route["kind"] == "source" else None,
    }


def dashboard_status():
    """broker_status for an AI chat: who is working where, and the shared editor's lease and build. The dashboard's raw
    snapshot (every session with its process ancestry, days of idle records) runs to ~13,600 tokens; this keeps active
    chats from the last 6 hours and drops internals (~300-1,000 tokens)."""
    now = time.time()
    rows = snapshot()

    def ago(stamp):
        minutes = int((now - (stamp or now)) // 60)
        return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"

    worktrees_out = []
    for row in rows:
        chats = [chat for chat in row["chats"]
                 if chat.get("state") not in ("Closed", "Stopped") and now - (chat.get("updated_at") or 0) < 6 * 3600]
        chats.sort(key=lambda chat: -(chat.get("updated_at") or 0))
        worktrees_out.append({
            "name": row["name"], "branch": row["branch"], **({"main": True} if row.get("is_main") else {}),
            "ai": ai_status(row["chats"]),
            "chats": [{"who": chat.get("label", "AI chat"), "state": chat.get("state"),
                       **({"doing": chat["detail"]} if chat.get("detail") else {}), "last": ago(chat.get("updated_at")),
                       **({"holds_lease": True} if chat.get("holds_lease") else {})} for chat in chats[:8]],
            **({"more_chats": len(chats) - 8} if len(chats) > 8 else {}),
        })
    held = lease.current()
    try:
        build = compile_status.summary(live_reload_tool.main_worktree())
    except Exception:  # noqa: BLE001 - status is best effort
        build = None
    return {"editor": {"lease": lease.describe(held) if held else None, "build": build or "compiles (or unknown)"},
            "worktrees": worktrees_out}


def state_tools():
    return [
        tool("broker_status", f"List {PROJECT_NAME} worktrees and live AI and window state shared with the dashboard."),
        tool("worktree_capabilities", "Get this session's exact edit path and supported preview and reload workflow."),
        tool("sync_publish", "Publish saved Unity edits in these paths without changing the checkout or Git index.",
             {"type": "object", "properties": {
                 "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                 "label": {"type": "string"},
             }, "required": ["paths"], "additionalProperties": False}),
        tool("sync_list", "List worktree snapshots published for this repository."),
        tool("integrate",
             "Show the COMBINED result of several in-flight worktrees without touching any of them (object-only git: "
             "temporary indexes, merge-tree, commit-tree). Each worktree's HEAD plus saved changes is merged onto the base; "
             "a worktree that conflicts is skipped and listed. Returns an id and a folder with the combined files; view it "
             "with ui_live_reload {integration: id, styles/source: ...} or verify=compile. Waits up to 50 s, then returns a "
             "run_id for integrate_status.",
             {"type": "object", "properties": {
                 "worktrees": {"anyOf": [{"const": "all"}, {"type": "array", "items": {"type": "string"}, "minItems": 1}],
                               "description": 'Worktree paths, or "all" (every worktree except the main checkout).'},
                 "base": {"type": "string", "description": "Git revision to build on (default: the main checkout's development branch)."},
                 "include_uncommitted": {"type": "boolean", "description": "Include each worktree's saved, uncommitted changes (default true)."},
                 "verify": {"enum": ["none", "compile"], "description": "compile: also compile the combined tree (about 3 minutes)."},
             }, "required": ["worktrees"], "additionalProperties": False}),
        tool("integrate_status", "Wait (up to 50 s) for an integrate run started by this chat and return its result when done.",
             {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"],
              "additionalProperties": False}),
        tool("integrate_list", "List recent integrations (id, base, included/skipped counts, changed files, folder)."),
        tool("sync_preview", "Preview another snapshot alongside the selected saved edits in this worktree.",
             {"type": "object", "properties": {
                 "snapshot_id": {"type": "string"},
                 "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
             }, "required": ["snapshot_id", "paths"], "additionalProperties": False}),
    ]


MAIN_EDITOR_LOCK = HERE / ".runs" / "main" / "editor.lock"


def ui_tools():
    return [
        tool("ui_live_reload",
             "Show this worktree's saved view changes in Main Unity's running game: .uss stylesheets (applied in memory, "
             "never written to the main project) and/or a view .cs (Pipeline method reload; its [MethodReload] rebuild "
             "methods rerun). Returns a capture plus new console warnings/errors. Main Unity must already be in Play mode "
             "on the screen you are working on. Pass integration (an id from integrate) to resolve the files inside that "
             "combined tree of several worktrees instead of this one. Everything is undone when Play mode ends or by ui_revert.",
             live_reload_tool.RELOAD_SCHEMA),
        tool("ui_revert", "Undo every live stylesheet override and C# reload in Main Unity (back to the main project's files).",
             live_reload_tool.REVERT_SCHEMA),
        tool("ui_capture", "Capture Main Unity's running game (HUD and board) without reloading anything.",
             live_reload_tool.CAPTURE_SCHEMA),
    ]


async def thread_holding_lock(function, *args):
    """Run blocking editor work in a thread; if the call is cancelled (the tool's time limit), keep holding the editor
    lock until the thread really ends, so the next chat's calls can't interleave with work still talking to Unity.
    The thread's own HTTP timeouts (live_reload_tool, at most 90 s each) bound the wait; after 180 s more it is left."""
    work = asyncio.ensure_future(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        await asyncio.wait([work], timeout=180)
        raise


async def call_ui_tool(name, arguments, root, route_name):
    """Runs under the main editor's lock so it never interleaves with another session's editor calls."""
    async with editor_turn(MAIN_EDITOR_LOCK):
        load_route(live_reload_tool.main_worktree())
        if name == "ui_live_reload":
            summary, image = await thread_holding_lock(
                live_reload_tool.live_reload, root, route_name, arguments.get("source"), arguments.get("styles", ()),
                arguments.get("backend", "interpreter"), arguments.get("capture", True),
                arguments.get("width", 1280), arguments.get("height", 720), arguments.get("integration"),
                arguments.get("view", "fit"), arguments.get("region"), arguments.get("element"))
        elif name == "ui_revert":
            summary, image = await thread_holding_lock(live_reload_tool.revert_all), None
        else:
            summary, image = await thread_holding_lock(
                live_reload_tool.capture_only, route_name,
                arguments.get("width", 1280), arguments.get("height", 720),
                arguments.get("view", "fit"), arguments.get("region"), arguments.get("element"))
    return with_compile_note(live_reload_tool.as_content(types, summary, image))


UI_TOOLS = {"ui_live_reload", "ui_capture", "ui_revert"}
LEASE_TOOLS = {"editor_lease", "editor_release"}


def lease_tools():
    return [
        tool("editor_lease",
             "Reserve Main Unity for a multi-step job (enter Play mode, step, capture...) so no other chat's calls run "
             "in between. Call again to renew. Each grant lasts 1-20 minutes (default 5); 30 minutes in a row at most. "
             "Other chats wait, then get told who holds it and until when. Release it with editor_release as soon as "
             "you are done. The lease ends by itself when it runs out, when this chat closes, or when this chat makes no "
             f"editor call for {lease.IDLE_SECONDS / 60:g} minutes.",
             {"type": "object", "properties": {
                 "minutes": {"type": "number", "minimum": 1, "maximum": lease.MAX_GRANT_MINUTES},
                 "reason": {"type": "string", "description": "What you are doing, shown to other chats and the user."},
             }, "required": ["reason"], "additionalProperties": False}),
        tool("editor_release", "End this chat's Main Unity lease so other chats can use the editor."),
    ]


def compile_tools():
    return [tool("editor_compile_status",
                 "Did Main Unity's last compile succeed? If not: every broken file, its errors, whether it is uncommitted, "
                 "when it changed and which AI session last edited it. Main Unity calls also carry a short note while the "
                 "build is broken.")]


def verify_tools():
    checks = verify_tool.checks()
    if not checks:
        return []
    listed = "; ".join(f"'{name}'" + (f" ({spec['description']})" if spec.get("description") else "")
                       for name, spec in checks.items())
    properties = {"checks": {"type": "array", "items": {"enum": list(checks)}, "minItems": 1}}
    if any(step.get("filter_args") for spec in checks.values() for step in spec.get("steps", [])):
        properties["test_filter"] = {"type": "string", "description": "Passed to the checks that take a test filter."}
    return [
        tool("verify",
             f"Check this worktree's saved files without the shared editor. Checks: {listed}. Default "
             f"'{verify_tool.default_check()}'. Waits up to 50 s, then returns a run_id for verify_status. Returns "
             "pass/fail with the first errors or failing tests.",
             {"type": "object", "properties": properties, "additionalProperties": False}),
        tool("verify_status", "Wait (up to 50 s) for a verify run started by this chat and return its result when done.",
             {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"],
              "additionalProperties": False}),
    ]


async def call_verify_tool(name, arguments, root):
    if name == "verify":
        run_id = verify_tool.start(root, arguments.get("checks"), arguments.get("test_filter"))
    else:
        run_id = arguments["run_id"]
    return await asyncio.to_thread(verify_tool.wait, run_id, 50)


VERIFY_TOOLS = {"verify", "verify_status"}
FRESHNESS_TOOLS = {"worktree_freshness", "worktree_update"}


def commit_tools():
    return [tool("commit_paths",
                 "Commit ONLY these paths in this checkout, safely while other chats share it: one commit at a time, built "
                 "in a private index (another chat's staged files are never included or unstaged), HEAD moved by "
                 "compare-and-swap. Ask the user before committing. Never commits everything; never pushes.",
                 {"type": "object", "properties": {
                     "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                               "description": "Files or folders, relative to the checkout root."},
                     "message": {"type": "string"},
                     "dry_run": {"type": "boolean"},
                 }, "required": ["paths", "message"], "additionalProperties": False})]


def freshness_tools():
    return [
        tool("worktree_freshness",
             "How far this worktree has drifted from the development line: commits behind, main's uncommitted files you "
             "can't see, and DRIFT: files you changed that main has also changed since your base (committed, or "
             "uncommitted with the AI session editing them). Check it before building on a file or trusting a capture."),
        tool("worktree_update",
             "Bring this worktree up to date: merge the development branch in (a merge, never a rebase or reset). It first "
             "checks for conflicts without touching anything; on a conflict, or when the merge would change a file you "
             "have uncommitted edits in, nothing is changed and the files are listed. include_uncommitted also merges a "
             "snapshot of main's uncommitted work as its own commit (other sessions' unfinished edits: not yours to hand "
             "back). dry_run reports what would happen.",
             {"type": "object", "properties": {
                 "include_uncommitted": {"type": "boolean"},
                 "dry_run": {"type": "boolean"},
             }, "additionalProperties": False}),
    ]


def with_drift_note(result, root):
    """Append the worktree's drift warning (behind or overlapping main) to a source route's result."""
    note = freshness.note(root)
    if not note:
        return result
    if isinstance(result, dict):
        return {**result, "drift": note}
    item = types.TextContent(type="text", text=note)
    if isinstance(result, types.CallToolResult):
        return result.model_copy(update={"content": [*result.content, item]})
    if isinstance(result, list):
        return [*result, item]
    return result


def compile_report():
    main = live_reload_tool.main_worktree()
    status = compile_status.read_status()
    if status is None:
        return {"known": False, "note": "No compile recorded yet (the editor writes one after its next compile)."}
    rows = compile_status.broken_files(main, status)
    return {"known": True, "ok": not rows, "compiled_at": time.strftime("%H:%M:%S", time.localtime(status["time"])),
            "files": [compile_status.describe_file(row) for row in rows]}


def with_compile_note(result):
    """Append the broken-build note to a tool result (a CallToolResult or a content list) while Main doesn't compile."""
    try:
        note = compile_status.warning(live_reload_tool.main_worktree())
    except Exception:  # noqa: BLE001 - a note must never break the call it rides on
        note = None
    if not note:
        return result
    item = types.TextContent(type="text", text=note)
    if isinstance(result, types.CallToolResult):
        return result.model_copy(update={"content": [*result.content, item]})
    if isinstance(result, list):
        return [*result, item]
    return result


def call_lease_tool(name, arguments, root, route_name):
    if name == "editor_release":
        return {"released": lease.release()}
    who = presence.client_label((presence.current() or {}).get("client"))
    holder = f"{who} in {Path(root).name} (broker {os.getpid()})"
    granted = lease.acquire(holder, root, arguments.get("reason", ""), arguments.get("minutes", 5))
    return {"leased": True, "holder": granted["holder"], "expires": time.strftime("%H:%M:%S", time.localtime(granted["expires"])),
            "note": "Release with editor_release when done."}


INTEGRATE_TOOLS = {"integrate", "integrate_status", "integrate_list"}


async def call_integrate_tool(name, arguments):
    if name == "integrate_list":
        return await asyncio.to_thread(integrate_tool.list_integrations)
    run_id = integrate_tool.start(**arguments) if name == "integrate" else arguments["run_id"]
    return await asyncio.to_thread(integrate_tool.wait, run_id, 50)


SYNC_TOOLS = {
    "sync_publish": worktree_sync.publish,
    "sync_list": worktree_sync.list_published,
    "sync_preview": worktree_sync.preview,
}


def create_source_server(root, route):
    server = Server(f"{MCP_SERVER}-source")

    @server.list_tools()
    async def list_tools():
        note_client(server)
        return [*state_tools(), *ui_tools(), *lease_tools(), *compile_tools(), *verify_tools(), *freshness_tools(), *commit_tools(),
                tool("worktree_target", "Show the worktree bound to this source-only session.")]

    @server.call_tool()
    @activity.logged(route["name"], root)
    async def call(name, arguments):
        note_client(server)
        if name in INTEGRATE_TOOLS:
            return await call_integrate_tool(name, arguments)
        if name in SYNC_TOOLS:
            return await asyncio.to_thread(SYNC_TOOLS[name], root, **arguments)
        if name == "broker_status":
            return await asyncio.to_thread(dashboard_status)
        if name == "worktree_capabilities":
            return await asyncio.to_thread(capabilities, root, route)
        if name == "worktree_freshness":
            return await asyncio.to_thread(freshness.check, root, False)
        if name == "worktree_update":
            return await asyncio.to_thread(freshness.sync, root, **arguments)
        if name in UI_TOOLS:
            result = await call_ui_tool(name, arguments, root, route["name"])
            return await asyncio.to_thread(with_drift_note, result, root)
        if name in LEASE_TOOLS:
            return await asyncio.to_thread(call_lease_tool, name, arguments, root, route["name"])
        if name == "editor_compile_status":
            return await asyncio.to_thread(compile_report)
        if name == "commit_paths":
            return await asyncio.to_thread(commit_tool.commit, root, arguments["paths"], arguments["message"],
                                           arguments.get("dry_run", False))
        if name in VERIFY_TOOLS:
            result = await call_verify_tool(name, arguments, root)
            return await asyncio.to_thread(with_drift_note, result, root)
        if name == "worktree_target":
            return {"worktree": str(root), "kind": "source", "unity_instance": None}
        raise RuntimeError("This is a source-only worktree. Only Main Unity has editor tools.")

    return server


def apply_preview_patch(root, runs, run_id, source, artwork):
    if Path(run_id).name != run_id or run_id in {"", ".", ".."}:
        raise ValueError("Invalid run ID")
    run = read_json(runs / run_id / "run.json")
    if Path(run["worktree"]).resolve() != root or not player_running(run):
        raise RuntimeError("The selected preview is not running in this worktree")
    process = psutil.Process(run["pid"])
    patch = argument(process.cmdline(), "-broker-patch")
    if not patch:
        raise RuntimeError("The selected player was launched without the preview receiver")
    source_path = (root / source).resolve(strict=True)
    artwork_path = (root / artwork).resolve(strict=True)
    for path, extension in ((source_path, ".cs"), (artwork_path, ".png")):
        if not path.is_relative_to(root) or path.suffix.lower() != extension:
            raise ValueError(f"Expected a {extension} file inside this worktree")
    patch_path = Path(patch).resolve()
    if patch_path.exists():
        raise RuntimeError("This preview run already consumed its one patch")
    result = subprocess.run([
        sys.executable, str(HERE / "live-reload" / "live_reload.py"), "patch",
        "--player", process.exe(), "--patch-file", str(patch_path),
        "--source", str(source_path), "--artwork", str(artwork_path),
    ], capture_output=True, text=True, timeout=110, stdin=subprocess.DEVNULL,
       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr).strip())
    return {"run_id": run_id, "output": result.stdout.strip()}


def create_player_server(root, route):
    server = Server(f"{MCP_SERVER}-player")
    runs = HERE / ".runs" / route["name"]

    @server.list_tools()
    async def list_tools():
        note_client(server)
        return [
            *state_tools(),
            tool("worktree_target", "Show the exact worktree this agent is bound to and system memory."),
            tool("bot_start", f"Launch {PLAYER.get('description', 'the configured player')} from this worktree's existing build."),
            tool("bot_status", "Read this worktree's latest match logs and run identity."),
            tool(
                "preview_patch", "Apply this worktree's C# and artwork to a registered preview run.",
                {"type": "object", "properties": {
                    "run_id": {"type": "string"}, "source": {"type": "string"},
                    "artwork": {"type": "string"},
                }, "required": ["run_id", "source", "artwork"], "additionalProperties": False},
            ),
        ]

    @server.call_tool()
    @activity.logged(route["name"], root)
    async def call(name, arguments):
        note_client(server)
        if name in INTEGRATE_TOOLS:
            return await call_integrate_tool(name, arguments)
        if name in SYNC_TOOLS:
            return await asyncio.to_thread(SYNC_TOOLS[name], root, **arguments)
        if name == "broker_status":
            return await asyncio.to_thread(dashboard_status)
        if name == "worktree_capabilities":
            return capabilities(root, route)
        if name == "preview_patch":
            async with editor_turn(runs / "preview-patch.lock", leased=False):
                return await asyncio.to_thread(apply_preview_patch, root, runs, **arguments)
        if name == "worktree_target":
            return {"worktree": str(root), "kind": "player", **memory()}
        if name == "bot_status":
            latest = max(runs.glob("*/run.json"), key=lambda path: path.stat().st_mtime, default=None)
            if latest is None:
                raise RuntimeError("No match has been started for this worktree")
            return run_status(latest.parent)
        if name != "bot_start":
            raise RuntimeError(f"Unknown player tool: {name}")
        return await asyncio.to_thread(start_match, root, route, runs)

    return server


def start_match(root, route, runs):
    runs.mkdir(parents=True, exist_ok=True)
    with (runs / "launch.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("Another AI session is starting this worktree's match") from error
        return launch_match(root, route, runs)


def wait_for_spectator(log, process):
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("The player exited before its first bot turn")
        if log.exists() and PLAYER.get("ready_marker", "") in log.read_text(encoding="utf-8", errors="replace"):
            return
        time.sleep(0.15)
    raise RuntimeError(f"The player's ready line ({PLAYER.get('ready_marker')!r}) did not appear within 55 seconds")


def launch_match(root, route, runs):
    for run in runs.glob("*/run.json"):
        if player_running(read_json(run)):
            raise RuntimeError("This worktree already has a running match")
    headroom = memory()
    if headroom["available_ram_mib"] < 2048 or headroom["available_commit_mib"] < 4096:
        raise RuntimeError(f"Free at least 2 GiB RAM and 4 GiB commit headroom before launching: {headroom}")
    if not PLAYER.get("executable"):
        raise RuntimeError("project.json has no player section: bot_start is not configured for this project.")
    player = (root / route["player"]).resolve(strict=True)
    startup_patch = route.get("startup_patch")
    if (not player.is_relative_to(root) and not startup_patch) or player.name.lower() != PLAYER["executable"].lower():
        raise RuntimeError(f"Invalid worktree player: {player}")
    if startup_patch:
        receiver = (player.parent / f"{player.stem}_Data" / "Managed"
                    / f"{PLAYER.get('bridge_assembly', 'WorktreeBroker.PlayerBridge')}.dll")
        if not receiver.is_file():
            raise RuntimeError("The configured player does not have the startup patch receiver")
        for name in ("source", "artwork"):
            source = (root / startup_patch[name]).resolve(strict=True)
            if not source.is_relative_to(root):
                raise RuntimeError("Startup patch files must be inside the worktree")

    run_id = uuid4().hex
    directory = runs / run_id
    directory.mkdir(parents=True)
    log = directory / "player.log"
    values = {"port": route.get("port", "")}
    arguments = [str(player), "-logFile", str(log), *(fill(part, **values) for part in PLAYER.get("arguments", []))]
    if startup_patch:
        arguments += ["-broker-patch", str(directory / "Patch.dll")]
    else:
        arguments += [fill(part, **values) for part in PLAYER.get("host_arguments", [])]
    with open(os.devnull, "r+b") as null:
        process = subprocess.Popen(
            arguments, cwd=player.parent, stdin=null, stdout=null, stderr=null,
        )
    info = {
        "run_id": run_id, "pid": process.pid, "worktree": str(root),
        "process_started_at": psutil.Process(process.pid).create_time(),
        "build_provenance": "Existing player data. Exact build revision has not been verified.",
        "player": str(player), "log": str(log),
    }
    write_json(directory / "run.json", info)
    if startup_patch:
        try:
            wait_for_spectator(log, process)
            info["startup_patch"] = apply_preview_patch(root, runs, run_id, **startup_patch)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            info["startup_patch_error"] = str(error)
        write_json(directory / "run.json", info)
    return info


def create_editor_server(upstream, root, route):
    server = Server(f"{MCP_SERVER}-editor")
    lock_path = HERE / ".runs" / route["name"] / "editor.lock"

    @server.list_tools()
    async def list_tools():
        note_client(server)
        upstream_tools = await upstream.tool_list()
        tools = [
            *state_tools(),
            *ui_tools(),
            *lease_tools(),
            *compile_tools(),
            *verify_tools(),
            *commit_tools(),
            tool("worktree_target", "Resolve this AI session's exact Unity editor target."),
            tool(
                "worktree_editor_resource",
                "Read Unity project info or editor state for this worktree.",
                {"type": "object", "properties": {"uri": {"enum": sorted(EDITOR_RESOURCES)}},
                 "required": ["uri"], "additionalProperties": False},
            ),
        ]
        for upstream_tool in upstream_tools:
            if upstream_tool.name == "set_active_instance" or not offered(upstream_tool.name):
                continue
            schema = dict(upstream_tool.inputSchema)
            properties = dict(schema.get("properties", {}))
            properties.pop("unity_instance", None)
            schema["properties"] = properties
            schema["required"] = [
                field for field in schema.get("required", []) if field != "unity_instance"
            ]
            schema["additionalProperties"] = False
            tools.append(upstream_tool.model_copy(update={"inputSchema": schema}))
        return tools

    @server.call_tool()
    @activity.logged(route["name"], root, on_timeout=upstream.restart)
    async def call(name, arguments):
        note_client(server)
        if name in INTEGRATE_TOOLS:
            return await call_integrate_tool(name, arguments)
        if name in SYNC_TOOLS:
            return await asyncio.to_thread(SYNC_TOOLS[name], root, **arguments)
        if name == "broker_status":
            return await asyncio.to_thread(dashboard_status)
        if name == "worktree_capabilities":
            return capabilities(root, route)
        if name in UI_TOOLS:
            return await call_ui_tool(name, arguments, root, route["name"])
        if name in LEASE_TOOLS:
            return await asyncio.to_thread(call_lease_tool, name, arguments, root, route["name"])
        if name == "editor_compile_status":
            return await asyncio.to_thread(compile_report)
        if name == "commit_paths":
            return await asyncio.to_thread(commit_tool.commit, root, arguments["paths"], arguments["message"],
                                           arguments.get("dry_run", False))
        if name in VERIFY_TOOLS:
            return await call_verify_tool(name, arguments, root)
        blocking = lease.blocking_lease()
        if blocking is not None and lease.seconds_left(blocking) > 30:
            # Refuse before waiting for Unity MCP to start (~18 s on a chat's first editor call).
            raise RuntimeError(f"Main editor is leased by {lease.describe(blocking)}. Retry after that, or ask the "
                               "user (the dashboard's Free the editor button ends a lease).")
        session = await upstream.session()
        async with editor_turn(lock_path, tool=name, worktree=root):
            check_development_branch(route)
            target = await editor_id(session, root)
            if name == "worktree_target":
                return {"worktree": str(root), "kind": "editor", "unity_instance": target}
            if name == "set_active_instance" or "unity_instance" in arguments:
                raise RuntimeError("Editor selection is locked to this worktree")
            if name == "batch_execute":
                for command in arguments["commands"]:
                    if command.get("tool") in {"set_active_instance", "batch_execute"} or (
                        "unity_instance" in (command.get("params") or {})
                    ):
                        raise RuntimeError("Batch commands cannot change the worktree editor target")
            pin = await session.call_tool("set_active_instance", {"instance": target})
            if pin.isError or any(
                isinstance(item, types.TextContent)
                and '"success":false' in "".join(item.text.split())
                for item in pin.content
            ):
                raise RuntimeError(f"Unity MCP refused to pin {target}: {pin.content}")
            if name == "worktree_editor_resource":
                uri = arguments["uri"]
                if uri not in EDITOR_RESOURCES:
                    raise RuntimeError(f"Unsupported resource: {uri}")
                result = await session.read_resource(uri)
                return [types.TextContent(type="text", text=item.text) for item in result.contents]
            result = await session.call_tool(name, arguments)
            return await asyncio.to_thread(with_compile_note, result)  # git work: never on the event loop

    return server


# Unity's own MCP server (MCP for Unity) runs through uvx. project.json "unity_mcp": "server" pins the server package
# (keep it matching the com.coplaydev.unity-mcp package in the project), "uvx" names the executable if it isn't on PATH.
UNITY_MCP = PROJECT.get("unity_mcp", {})

# Which Unity MCP tools a chat sees. Every tool definition sits in each chat's context: all 45 cost ~24,800 tokens per
# chat, chats use a handful, and execute_code can do anything in the editor.
# project.json "unity_mcp": {"tools": "all"} offers everything; a list offers exactly those. Unlisted tools still work
# if called by name.
LEAN_UNITY_TOOLS = ["execute_code", "read_console", "refresh_unity", "manage_editor", "manage_scene", "find_gameobjects",
                    "run_tests", "get_test_job", "unity_reflect", "validate_script"]


def offered(name):
    wanted = UNITY_MCP.get("tools", "lean")
    if wanted == "all":
        return True
    return name in (LEAN_UNITY_TOOLS if wanted == "lean" else wanted)


def find_uvx():
    configured = UNITY_MCP.get("uvx")
    candidates = [configured] if configured else []
    candidates += [shutil.which("uvx"), str(Path.home() / ".local" / "bin" / ("uvx.exe" if os.name == "nt" else "uvx"))]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError("uvx was not found: install uv (https://docs.astral.sh/uv/) or set project.json unity_mcp.uvx.")


def upstream_env():
    """A small environment for Unity's MCP server: enough for uvx to find its cache and Python, nothing else."""
    keep = {"SystemRoot", "SYSTEMDRIVE", "PATH", "PATHEXT", "USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA",
            "PROGRAMDATA", "TEMP", "TMP", "COMSPEC",
            # proxies and certificates, so the first uvx download works behind a corporate proxy
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"}
    return {key: value for key, value in os.environ.items()
            if key.upper() in {name.upper() for name in keep} or key.upper().startswith("UV_")}


class LazyUpstream:
    """Unity's own MCP server, started in the background.

    Starting it through uvx took ~20 s, more on a busy machine, and the broker used to wait for it before answering:
    Claude Code gives an MCP server 30 s to connect and dropped the broker. Now the broker answers at once; tool calls
    wait for Unity's server (up to CONNECT_SECONDS), and the first list_tools uses the tool list cached from the last
    successful start so it doesn't wait either.
    """

    CONNECT_SECONDS = 120
    CACHE = HERE / ".runs" / "main" / "upstream-tools.json"

    def __init__(self, params):
        self.params = params
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.client = None
        self.tools = None
        self.error = None
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.run())
        return self.task

    async def restart(self):
        """After a call was cut off: Unity MCP may still be waiting on a reply that will never come (a domain reload
        mid-call), so drop that connection and start a fresh one. Later calls wait for it as on a first start."""
        old = self.task
        self.client, self.error = None, None
        self.ready.clear()
        self.stop.set()
        if old is not None:
            try:
                await asyncio.wait_for(old, 10)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001 - replacing it anyway
                old.cancel()
        self.stop = asyncio.Event()
        self.start()

    async def run(self):
        try:
            async with stdio_client(self.params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    self.tools = (await client.list_tools()).tools
                    self.client = client
                    self.save_cache()
                    self.ready.set()
                    await self.stop.wait()
        except Exception as error:  # noqa: BLE001 - reported to the next caller instead of killing the broker
            self.error = error
            self.ready.set()

    def save_cache(self):
        try:
            self.CACHE.parent.mkdir(parents=True, exist_ok=True)
            self.CACHE.write_text(json.dumps([item.model_dump(mode="json") for item in self.tools]), encoding="utf-8")
        except OSError:
            pass

    def cached_tools(self):
        try:
            return [types.Tool.model_validate(item) for item in json.loads(self.CACHE.read_text(encoding="utf-8"))]
        except (OSError, ValueError):
            return None

    async def session(self):
        try:
            await asyncio.wait_for(self.ready.wait(), self.CONNECT_SECONDS)
        except TimeoutError as error:
            raise RuntimeError(f"Unity MCP did not start within {self.CONNECT_SECONDS} s") from error
        if self.client is None:
            raise RuntimeError(f"Unity MCP failed to start: {self.error}")
        return self.client

    async def tool_list(self):
        if self.tools is not None:
            return self.tools
        cached = self.cached_tools()
        if cached is not None:
            return cached
        await self.session()  # first run on this machine: nothing cached yet
        return self.tools


def note_client(server):
    """Record which MCP client started this broker (its handshake's clientInfo), for the dashboard."""
    try:
        info = server.request_context.session.client_params.clientInfo
        presence.set_client(info.name, info.version)
    except Exception:  # noqa: BLE001 - outside a request, or an old client
        pass


async def serve(server):
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        try:
            lease.release()  # a closing chat never leaves the editor reserved
        except Exception:  # noqa: BLE001 - shutting down
            pass
        presence.unregister()


def _quietly(function):
    try:
        function()
    except Exception:  # noqa: BLE001 - housekeeping must never take a broker down
        pass


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", default=os.getcwd())
    parser.add_argument("--start-bot", action="store_true")
    args = parser.parse_args()
    root, route = load_route(args.worktree)
    if not args.start_bot:
        presence.register(route["name"], root)  # any MCP client shows on the dashboard, hooks or not
        # Prune old run data (at most hourly, see housekeeping.py) without delaying the MCP handshake.
        import housekeeping
        import threading
        threading.Thread(target=lambda: _quietly(housekeeping.run), daemon=True).start()
    if route["kind"] == "player":
        if args.start_bot:
            print(json.dumps(start_match(root, route, HERE / ".runs" / route["name"])))
            return
        await serve(create_player_server(root, route))
        return
    if args.start_bot:
        raise RuntimeError("--start-bot requires a player worktree")
    if route["kind"] == "source":
        await serve(create_source_server(root, route))
        return
    if route["kind"] != "editor":
        raise RuntimeError(f"Unknown route kind: {route['kind']}")
    params = StdioServerParameters(command=find_uvx(), args=["--from", UNITY_MCP.get("server", "mcpforunityserver==10.3.0"),
                                   "mcp-for-unity", "--transport", "stdio"],
                                   env=upstream_env(), cwd=str(root))
    upstream = LazyUpstream(params)
    upstream.start()
    try:
        await serve(create_editor_server(upstream, root, route))
    finally:
        upstream.stop.set()
        try:
            await asyncio.wait_for(upstream.task, 10)
        except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
            upstream.task.cancel()


if __name__ == "__main__":
    asyncio.run(main())


