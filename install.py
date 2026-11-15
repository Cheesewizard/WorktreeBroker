"""Set the broker up on this machine, with this machine's own paths.

Each command writes its configs from the Python running it (`sys.executable`) and this folder's own location, so they
work for whoever runs it and nobody types a path by hand. Nothing existing is overwritten except the broker's own entries, which are replaced in place.

    python install.py config         routes.json and project.json, if missing, worked out from this checkout
    python install.py claude         the main checkout's .mcp.json (broker server) and .claude/settings.json
                                     (the hook that blocks direct Unity MCP)
    python install.py claude-hooks   prints the user-level Claude Code hooks for the dashboard's chat state;
                                     --write merges them into ~/.claude/settings.json (a .bak copy is kept)
    python install.py codex          each worktree's .codex/config.toml (broker on, direct Unity MCP off); keeps
                                     existing files
    python install.py all            config, claude and codex, then prints the hooks

Run it with the Python you installed requirements.txt into. The broker always runs from the main checkout's copy of this
folder, so every worktree's chat runs the same broker code.
"""

import argparse
import json
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
PYTHON = Path(sys.executable).resolve()
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd")


def _pythonw():
    candidate = PYTHON.with_name("pythonw.exe")
    return candidate if candidate.is_file() else PYTHON


def _remember_python():
    """Open Dashboard.cmd starts the dashboard with the pythonw next to the Python that ran the installer."""
    (HERE / ".python-path").write_text(str(_pythonw()), encoding="utf-8")


def _read(path):
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as error:
        sys.exit(f"{path} is not valid JSON ({error}); fix it first, nothing was written.")


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path}")


def _quote(path):
    return f"\"{Path(path).as_posix()}\""


def config():
    import broker_state

    routes = HERE / "routes.json"
    if routes.exists():
        print(f"Kept {routes}")
    else:
        main = {key: value for key, value in broker_state.default_routes()[0].items() if key != "default"}
        _write(routes, [main])
        print(f"  main checkout {main['worktree']}, development branch {main['development_branch']}")
    project = HERE / "project.json"
    if project.exists():
        print(f"Kept {project}")
    else:
        _write(project, {"name": broker_state.REPOSITORY.name, "mcp_server": "unity_broker",
                         "unity_mcp": {"server": "mcpforunityserver==10.3.0", "direct_server": "unityMCP"}})
        print("  add verify checks, a player and editor hooks later; see project.example.json")


def _state():
    import broker_state

    main = broker_state.main_route()
    return Path(main["worktree"]).resolve(), broker_state.MCP_SERVER, broker_state.DIRECT_UNITY_MCP


def claude():
    repository, server, direct = _state()
    mcp_path = repository / ".mcp.json"
    mcp = _read(mcp_path)
    mcp.setdefault("mcpServers", {})[server] = {"command": str(PYTHON), "args": [str(HERE / "broker.py")]}
    _write(mcp_path, mcp)
    settings_path = repository / ".claude" / "settings.json"
    settings = _read(settings_path)
    hook = {"type": "command", "command": f"{_quote(PYTHON)} {_quote(HERE / 'block_direct_unity.py')}", "timeout": 10}
    pre = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    pre[:] = [entry for entry in pre
              if not any("block_direct_unity.py" in item.get("command", "") for item in entry.get("hooks", []))]
    pre.append({"matcher": f"mcp__{direct}__.*", "hooks": [hook]})
    _write(settings_path, settings)


def _session_hooks():
    command = f"{_quote(PYTHON)} {_quote(HERE / 'session_hook.py')} --source claude"
    hook = {"type": "command", "command": command, "timeout": 10, "async": True}
    hooks = {event: [{"hooks": [dict(hook)]}] for event in HOOK_EVENTS}
    # Who last edited which file, so a compile error can name the chat (compile_status.py).
    hooks["PostToolUse"] = [{"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [dict(hook)]}]
    return hooks


def claude_hooks(write):
    hooks = _session_hooks()
    if not write:
        print(json.dumps({"hooks": hooks}, indent=2))
        print("\nMerge this into ~/.claude/settings.json, or rerun with --write to do it (a .bak copy is kept).")
        return
    path = Path.home() / ".claude" / "settings.json"
    settings = _read(path)
    if path.is_file():
        shutil.copyfile(path, path.with_name("settings.json.bak"))
    existing = settings.setdefault("hooks", {})
    for event, entries in hooks.items():
        kept = [entry for entry in existing.get(event, [])
                if not any("session_hook.py" in item.get("command", "") for item in entry.get("hooks", []))]
        existing[event] = kept + entries
    _write(path, settings)


def codex():
    import broker_state

    for row in broker_state.worktrees():
        root = Path(row["path"])
        if not (root / "Assets").is_dir():
            continue
        target = root / ".codex" / "config.toml"
        if target.exists():
            print(f"Kept {target}")
            continue
        target.parent.mkdir(exist_ok=True)
        arguments = json.dumps([str(HERE / "broker.py"), "--worktree", str(root)])
        target.write_text(
            f"[mcp_servers.{broker_state.DIRECT_UNITY_MCP}]\nenabled = false\n\n"
            f"[mcp_servers.{broker_state.MCP_SERVER}]\n"
            f"command = {json.dumps(str(PYTHON))}\nargs = {arguments}\nstartup_timeout_sec = 60\n",
            encoding="utf-8")
        print(f"Wrote {target}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("config", "claude", "claude-hooks", "codex", "all"))
    parser.add_argument("--write", action="store_true", help="claude-hooks: merge into ~/.claude/settings.json")
    args = parser.parse_args()
    _remember_python()
    if args.command in ("config", "all"):
        config()
    if args.command in ("claude", "all"):
        claude()
    if args.command in ("codex", "all"):
        codex()
    if args.command in ("claude-hooks", "all"):
        claude_hooks(args.write and args.command == "claude-hooks")
    if args.command in ("claude", "codex", "all"):
        print("Reopen running chats so they load the broker.")


if __name__ == "__main__":
    main()
