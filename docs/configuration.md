# Setup and configuration

Setup details, `routes.json`, `project.json` and the editor package's exchange files. Part of [Worktree Broker for Unity](../README.md).

## Setup

Requirements: Windows only (the broker uses msvcrt file locks, Win32 window APIs and pywin32 performance counters),
Python 3.12 or later (3.13 tested) with `requirements.txt` (`mcp`, `psutil`, `websockets`, `pywin32`), git, uv (for
`uvx`), Unity 6, the `com.coplaydev.unity-mcp` package, the broker's Unity package, and optionally `com.unity.pipeline`
for C# view reload.

The Unity project must be at the root of the git repository (an `Assets` folder at the top). A project in a subfolder
isn't supported yet, and the broker says so.

- **Unity package**: add `com.worktreebroker.unity` to `Packages/manifest.json` (see `unity-package/README.md`).
- **Routes** (optional): `python install.py config` writes `routes.json` from this checkout, or copy
  `routes.example.json`. The `main` route names the editor checkout and its development branch; change the branch there
  when development moves. Without `routes.json` the broker uses one main route, the repository this folder sits in,
  pinned to the branch it was on when the broker started (`broker_state.default_routes()`). Player routes need the
  file. One loader (`broker_state.routes()` / `main_route()`) reads it and gives a clear message when it is invalid or has
  no single route named `main` with `worktree` and `development_branch`.
- **Project** (optional): `python install.py config` writes a minimal `project.json`; fill in more from
  `project.example.json` (see "Project configuration" below). Without it every optional feature is off. Both real files
  (`routes.json`, `project.json`) are in `.gitignore`; only the examples are committed.
- **Chats**: `python install.py claude` (`.mcp.json` and the blocking hook in the main checkout) and
  `python install.py codex` (each worktree's `.codex/config.toml`, never overwritten; `install_worktree_defaults.py`
  does the same). For Codex chat state, `python install_session_hooks.py <worktree>`, then review the hooks once with
  `/hooks` in Codex. Reopen the chat to pick up new tools.
- **A chat that hasn't reloaded its tools**: `python broker_client.py --worktree <path> list`, or
  `python broker_client.py --worktree <path> <tool> --arguments '<json>'`.
- **Register a separately launched player** for the dashboard: `python register_player.py <PID> <worktree>`.
- **Claude Code chat state:** `python install.py claude-hooks` prints the user-level hooks for `~/.claude/settings.json`
  (`--write` merges them, keeping a `.bak`; see the dashboard section). A session started before they were added appears
  after its next event.

## Project configuration

The broker knows nothing about any particular game. Two files next to `broker.py` describe the project, and a feature
whose section is missing switches itself off ("not configured") instead of failing.

**`routes.json`** (optional; template: `routes.example.json`; `python install.py config` writes one) lists the worktrees the broker serves. Without it, the main route is the repository this folder sits in, on its current branch. The route with a
`development_branch` is the main checkout: its `worktree` is the repository (default: the checkout this folder sits in,
for example `<repo>/tools/WorktreeBroker`), and its editor is the one every chat shares.

**`project.json`** (template: `project.example.json`) holds what differs between Unity projects:

| Key | Used by | Meaning |
| --- | --- | --- |
| `name` | dashboard, tool texts, hooks | What the project is called (default: the repository's folder name). Token usage counts transcripts whose folder or cwd contains the repository's folder name. |
| `mcp_server` | `install_worktree_defaults.py`, `block_direct_unity.py` | The broker's MCP server name as registered in `.mcp.json` / `.codex/config.toml` (default `unity_broker`). Tools appear as `mcp__<name>__<tool>`. |
| `editor.capture` (optional) | `ui_live_reload`, `ui_capture` | C# run in the editor (`script eval`) to save a PNG of the running game at `{path}`, `{width}` x `{height}`. Optionally also writes `layout/<capture>.json` beside it for the element and region views. Default: the package's `BrokerCapture.ForBroker`. Override only if the game isn't a single `UIDocument` over `Camera.main`. |
| `editor.style_apply`, `editor.style_revert` (optional) | `ui_live_reload` `styles`, `ui_revert` | C# that swaps the stylesheet at `{asset}` for the saved text in `{file}` in memory, and puts every swapped sheet back. Defaults: the package's `LiveStyles.Apply` and `LiveStyles.RevertAll`. Override only to use your own style code. |
| `unity_mcp` | the editor route, `install_worktree_defaults.py`, `block_direct_unity.py` | `server`: the uvx package spec that pins Unity's MCP server (default `mcpforunityserver==10.3.0`; keep it matching the `com.coplaydev.unity-mcp` package in the project). `uvx`: path to uvx if it isn't on PATH (otherwise PATH, then `~/.local/bin`, are searched). `direct_server` (default `unityMCP`): the name of the direct Unity MCP server that `block_direct_unity.py` blocks and `install_worktree_defaults.py` switches off for Codex. The upstream server gets the proxy, certificate and `UV_*` environment variables. `tools`: which Unity MCP tools chats see: `"lean"` (default: `execute_code`, `read_console`, `refresh_unity`, `manage_editor`, `manage_scene`, `find_gameobjects`, `run_tests`, `get_test_job`, `unity_reflect`, `validate_script`), `"all"`, or a list. Unlisted tools still work if called by name. |
| `lease_idle_minutes` | editor lease | Minutes without an editor call (and none running) after which a lease lapses. Default 5. |
| `integrate.extra_paths` | `integrate` | Folders beyond `Assets`, `Packages` and `ProjectSettings` that an integrate tree needs, for example a compile-check script and test projects. |
| `verify.checks` | `verify` | Named checks, each a list of `steps` (`command`, optional `env`, `requires`, `filter_args`), an optional `parser` (`unity-compile-check`, `dotnet-test` or `lines`), `runs_first` (checks reported before it) and `"slot": false` for fast checks that skip the machine-wide queue. `verify.default` names the default check. |
| `player` | `bot_start`, `register_player.py`, `live-reload/` | The standalone player: `executable`, launch `arguments` and `host_arguments` (`{port}` from the route), the log line that means it's ready (`ready_marker`), and for the standalone patch prototype `bridge_source`, `references`, `hook` and `bridge_assembly`. |
| `process_names.players` | dashboard performance page | Executable names counted as players of this project. |

Templates fill `{name}` slots by plain text replacement, so C# braces in snippets need no escaping. Command slots:
`{main}` the main checkout, `{worktree}` the worktree being checked, `{work}` its `.runs/verify/...` folder,
`{filter}` the test filter, `{broker}` this folder.

**Editor side.** The scripts that run inside Unity ship in `unity-package/` (`com.worktreebroker.unity`; see its
`README.md`). Without the package the features below have nothing to show.

- `CompileReport`: writes each compile's result after every compile (`editor_compile_status`, the dashboard's compile line).
- `LiveStyles`: applies and reverts stylesheets in memory for `ui_live_reload` and `ui_revert`.
- `LiveReload` (runtime): reruns `[MethodReload]` rebuild methods on views that called `LiveReload.Track(this)`.
- `BrokerCapture`: renders the capture and writes the layout facts.
- `EditorIdleGuard`: stops Play mode left unused.
- `BrokerFiles`: the exchange folder, `<project>/Library/WorktreeBroker/`.

The editor and the broker exchange three files in `<project>/Library/WorktreeBroker/`:

- `compile-status.json` (written by the editor) after every compile:
  `{"time": <unix seconds>, "ok": bool, "errors": [{"file", "line", "message", "assembly"}]}`, with `file`
  relative to the project root.
- `editor-guard.jsonl` (written by the editor): the idle Play mode guard's actions, for the dashboard.
- `editor-last-activity` (written by the broker): the guard reads it.

`.runs/activity.jsonl` and the other broker-only files stay in the broker's own `.runs/` folder. `ui_live_reload` needs
`com.unity.pipeline` and `[MethodReload]`-tagged rebuild methods. The `editor` section of `project.json` is optional;
the package supplies the capture and style hooks.
