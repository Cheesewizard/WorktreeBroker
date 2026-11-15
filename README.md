# Worktree Broker for Unity

The Worktree Broker for Unity is an MCP server, a small Unity package and a desktop dashboard. Together they let several
AI coding chats, each in its own git worktree of one Unity project, share **one** open Unity editor without getting in
each other's way. Only the main checkout opens Unity. Each worktree chat gets a narrow, locked set of tools that check
and show its work in that editor, and nothing it does there writes to the main checkout's files.

Status: 0.1.0, the first public release. Tested on Windows 11 with Unity 6000.4. Expect rough edges on other setups.

## Who it's for

It's for a developer who runs **several Claude Code or Codex chats at once** on one Unity project, each in its own git
worktree, and can't afford one editor per worktree. A Unity 6 editor often needs 2-3 GB plus most of a gigabyte per asset
import worker, and a fresh worktree's first import is slow, so a few editors can exhaust a 32 GB machine.

### What it's good for

- **Live UI reloads from a worktree.** A worktree chat edits a `.uss` stylesheet, or the body of a C# view method tagged
  `[MethodReload]`, then calls `ui_live_reload`. The running game in the main editor picks up the change in memory and the
  chat gets a screenshot back, usually in 0.25 to 2 seconds of editor time. `ui_revert` undoes it. The main checkout's
  files are never written.
- **Captures.** `ui_capture` renders the game's UI at any resolution while the editor is unfocused or the desktop is
  locked. It can crop to a region or a named element, or return only the layout facts (bounds, classes, fonts, text),
  which costs far fewer tokens than a full image.
- **An editor lease for multi-step work.** Each editor call takes a lock. `editor_lease` holds the editor for a sequence
  (enter Play mode, step frames, capture, exit), so another chat's call can't land halfway through. It runs out on its own
  when idle.
- **Verify and compile checks without a second editor.** `verify` runs your configured checks (for example a compile of
  every assembly against Unity's DLLs, or `dotnet test`) on a worktree, two at a time across the machine, and returns
  parsed errors. `editor_compile_status` reports the main editor's last compile and names the broken files. You supply the
  checks: the broker runs them and parses their output.
- **Safe commits in a shared checkout.** `commit_paths` (or `python commit.py -m "..." -- <paths>`) commits only the
  paths you name, through a private index, one commit at a time per checkout. Another chat's staged files are never swept
  in. It never pushes.
- **The integrate preview.** `integrate` merges several worktrees' saved state onto main using git objects only (no
  checkout, no index, no branch is moved) and reports conflicts. `ui_live_reload` with `integration` then shows the
  combined UI in the main editor.
- **Worktree drift.** `worktree_freshness` says how far a worktree is behind main and whether its changes overlap main's
  uncommitted work. `worktree_update` does a safe merge or refuses.
- **The dashboard.** A Tk window shows every worktree, which chat is running or waiting for you, who holds the editor,
  the last compile, Unity and player memory, and token use per worktree and chat, read from local transcripts.

### What it's NOT good for

- **One developer with one chat.** Use Unity's own MCP server directly. The broker only pays off when several chats
  compete for one editor.
- **Projects that aren't Unity.** Everything is built around a Unity editor, Unity MCP and Unity's Pipeline package.
- **macOS and Linux.** It's Windows only for now: `msvcrt` file locks, `NtQuerySystemInformation`, `taskkill`, Win32 window
  APIs and pywin32 counters.
- **Big scene, prefab or asset edits from a worktree.** Only two things reach the running editor live: stylesheets that
  already exist in the main project, and bodies of already-tagged C# methods reloaded through Pipeline. New files, new
  fields, signature changes, UXML, scenes, prefabs and imported assets need a normal import or compile in the main
  checkout, which means merging the worktree's branch first.
- **Two editors in Play mode at once.** There is one editor, and one chat at a time drives Play mode (through the lease).
  For networked play, test with built players instead (the `player` route can run one).
- **Worktree changes only reach the main editor through the broker's routes.** Those are live reload, captures, `verify`
  (which runs outside the editor), sync snapshots and the integrate preview. Everything else reaches it the normal way:
  merged into the main checkout's development branch. A worktree chat has no general editor control; only the main
  checkout's chat gets the full Unity MCP tool set.
- **A Unity project in a subfolder of the repository.** `Assets` has to be at the repository root.

## Requirements

- Windows 10 or 11.
- Python 3.12 or later (3.13 tested) with `pip install -r requirements.txt` (`mcp` 1.29, `psutil`, `websockets`, `pywin32`).
- git, and [uv](https://docs.astral.sh/uv/) for `uvx`, which starts Unity's MCP server (`mcpforunityserver==10.3.0`).
- Unity 6 (tested on 6000.4) with the Unity project at the repository root, the `com.coplaydev.unity-mcp` package (keep
  it matching the pinned server) and this repository's `unity-package/` (`com.worktreebroker.unity`).
- Optional: `com.unity.pipeline` (0.10.0-exp.1 or later) for C# view reload. Without it, only stylesheets reload.
- An MCP client: Claude Code or Codex. Other MCP clients can call the tools, but chat state on the dashboard comes from
  Claude Code and Codex hooks.

## Quick start

Every path below comes from your own machine: `install.py` writes the configs from the Python that runs it and from
where this folder sits, and never needs editing by hand. Run it with the Python you installed `requirements.txt` into.

1. Put this folder in your Unity repository's main checkout, for example at `tools/WorktreeBroker`, and run
   `python -m pip install -r requirements.txt`. A git submodule keeps it updatable:
   `git submodule add https://github.com/Cheesewizard/WorktreeBroker.git tools/WorktreeBroker`. The broker then treats
   the superproject (your game) as the project, and changes to the broker itself are committed inside the submodule.
2. Add the Unity package to `Packages/manifest.json`:
   `"com.worktreebroker.unity": "file:../tools/WorktreeBroker/unity-package"` (see `unity-package/README.md`).
3. `python install.py config`. It writes `routes.json` (the main route is the checkout this folder is in, pinned to the
   branch it's on now) and a minimal `project.json`, and keeps files that already exist. Both are optional: without
   `routes.json` the broker works out the same main route by itself, and without `project.json` it runs with defaults.
4. Register the broker with your chats. Both commands point at the **main checkout's** copy of `broker.py`, so every
   worktree runs the same broker code:
   - Claude Code: `python install.py claude` writes the broker into the main checkout's `.mcp.json`, and the hook that
     refuses direct Unity MCP calls into `.claude/settings.json`. Only the broker's own entries are replaced.
   - Codex: `python install.py codex` writes each worktree's `.codex/config.toml` (broker on, direct Unity MCP off) and
     keeps any that exist.
5. Open the main checkout in Unity, then start a chat (or reopen a running one) and call `worktree_capabilities`. It
   says which route the chat is on and how to check its work there.
6. Optional: `python install.py claude-hooks` prints the Claude Code hooks that give the dashboard each chat's state
   (`--write` merges them into `~/.claude/settings.json` and keeps a `.bak` copy). Then double-click
   `Open Dashboard.cmd`: it uses the `pythonw` next to the Python that ran the installer, then the `py` launcher, then
   `pythonw` on PATH.
7. Optional: add your own `verify` checks to `project.json` ([Bring your own compile check](docs/checks-and-git.md#bring-your-own-compile-check), and
   `project.example.json`).

[Setup and configuration](docs/configuration.md) has the details, [Reference](docs/reference.md#token-cost) says what
the broker costs each chat in context, and "Known limitations" below lists what isn't done yet.

## The problem it solves

A Unity project is developed by several AI chats in parallel, each in its own git worktree so they don't overwrite each
other's files. The obvious way to let each chat check its work in Unity is to open one editor per worktree, but each
editor costs gigabytes of memory and a slow first import, so that stops scaling after a few worktrees.

The broker keeps **one** editor (the main checkout named in `routes.json`) and gives every chat a safe, narrow way
to use it:

| Problem | What the broker does |
| --- | --- |
| Too many Unity editors | Only the main checkout opens Unity. Worktree chats get tools instead of an editor. |
| Calls going to the wrong editor | Every editor call is pinned to the main checkout's exact `Assets` path, and the checkout must be on its configured development branch. |
| Two chats driving the editor at once | Editor calls take a shared lock, so one chat's calls never land in the middle of another's. |
| Worktrees can't see their UI changes | `ui_live_reload` applies a worktree's stylesheets and view code to the running game and returns a screenshot, in well under a second of editor time. The main checkout's files are never written. |
| Sharing unfinished work between worktrees | `sync_publish` / `sync_preview` snapshot saved edits and preview a three-way merge without touching either checkout or its git index. |
| Seeing several worktrees' UI at once | `integrate` merges many worktrees' saved state onto main in git objects only; `ui_live_reload` with `integration` shows the combined result. |
| Running a bot match from a worktree | `bot_start` / `bot_status` run and watch a standalone player with memory checks. |
| Knowing what all the chats and windows are doing | A live dashboard lists worktrees, AI chat state, Unity windows and memory. |

## How it is wired

The broker is an MCP server (`broker.py`). Each worktree's Codex config (`.codex/config.toml`) starts it with
`--worktree <path>` and turns direct Unity MCP **off** for that worktree, so the broker is the only way in. What a
session can do depends on its **route** (`routes.json`):

| Route | Worktree | Tools |
| --- | --- | --- |
| `editor` (`main`) | The main checkout named in `routes.json` | Every Unity MCP tool (pinned and locked), plus the shared tools below. |
| `source` | Any other worktree of the project (registered or not) | Shared tools: `ui_live_reload`, `ui_revert`, `ui_capture`, sync, status. No other editor control. |
| `player` | A worktree with a player route | `bot_start`, `bot_status`, `preview_patch`, plus sync and status. |

Shared tools on every route: `broker_status`, `worktree_capabilities`, `worktree_target`, `sync_publish`, `sync_list`,
`sync_preview`, `integrate`, `integrate_status`, `integrate_list`. `ui_live_reload`, `ui_revert` and `ui_capture` are on the editor and source routes.

Start any session by calling `worktree_capabilities`: it says which route you're on and how to verify your work there.

## Documentation

| Page | What's in it |
| --- | --- |
| [Setup and configuration](docs/configuration.md) | Setup details, `routes.json`, `project.json` and every key, the editor package's exchange files |
| [UI live reload and captures](docs/live-reload.md) | `ui_live_reload`, `ui_revert`, `ui_capture`, what a view needs for C# reload, cheaper capture views |
| [The shared editor](docs/shared-editor.md) | Pinning, the lock and time limits, the editor lease, compile status, the activity log, the idle Play mode guard |
| [Checks and git tools](docs/checks-and-git.md) | `verify` and bringing your own compile check, worktree drift, `commit_paths`, `integrate`, sync snapshots |
| [The dashboard](docs/dashboard.md) | Pages, chat states, the editor line, machine headroom, the Usage page |
| [Bot matches](docs/players.md) | The player route: `bot_start`, `bot_status`, `preview_patch` |
| [Reference](docs/reference.md) | Token cost, tool annotations, troubleshooting, every file |
| [Unity package](unity-package/README.md) | The editor side: install, parts, Pipeline toggles |
| [Standalone player patch](live-reload/README.md) | The prototype player patcher and its bridge |

## Known limitations

- Every chat's broker on the main route starts its own Unity MCP server through `uvx`: about 300 MB per chat, counting uv
  and the Python helpers. Sharing one server over HTTP is possible (the server supports `--transport http`) but isn't
  done yet.
- Windows only: file locks use `msvcrt`, the process table uses `NtQuerySystemInformation`, hung children are ended with
  `taskkill`, and the dashboard uses Win32 window APIs and pywin32 counters. A port needs a replacement for each.
- The Unity project must be at the repository root (`Assets` at the top). A project in a subfolder is refused with a
  message.
- Live reload covers stylesheets that already exist in the main project and bodies of `[MethodReload]` methods already
  compiled into the main editor. Everything else a worktree changes reaches the main editor only once it is merged into
  the main checkout.
- `verify` runs checks you configure. The broker ships no compile check of its own (see [Bring your own compile check](docs/checks-and-git.md#bring-your-own-compile-check)).
- The test suite is small (`python -m pytest`): `integrate`, `install.py`, `commit.py --untrack` and the default routes.
  The other tools have no automated tests yet.
