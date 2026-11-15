# Changelog

Worktree Broker for Unity.

## 0.1.0 (2026-11-15)

The first public release. Windows only; tested with Unity 6000.4.

- MCP server (`broker.py`) with three routes. The **editor** route is the main checkout: a lean set of Unity MCP tools,
  pinned to its `Assets` path and its development branch, with one lock for every call. The **source** route is any other
  worktree. The **player** route is a worktree that runs a built player.
- Editor safety: a hard time limit on each call, Unity MCP restarted after a hung call, an editor lease for multi-step
  work, an idle Play mode guard, and a hook that blocks direct Unity MCP calls.
- `ui_live_reload`, `ui_revert`, `ui_capture`: stylesheets and `[MethodReload]` view methods applied to the running game
  in memory, plus captures (full, region, element or layout facts only) with the editor unfocused.
- `verify` / `verify_status`: configurable checks run on a worktree, with compile and `dotnet test` output parsers.
- `editor_compile_status`: the main editor's last compile, with the broken files named.
- `commit_paths` / `commit.py`: commits that include only the named paths, through a private index.
- `sync_publish` / `sync_preview`, `integrate`: snapshots, three-way previews and a combined tree of several worktrees,
  all in git objects only.
- `worktree_freshness` / `worktree_update`: drift reports and a safe merge.
- Dashboard: worktrees, chat state (Claude Code and Codex hooks), editor holder, compile line, memory, token use.
- Unity package `com.worktreebroker.unity` 0.1.0: CompileReport, LiveStyles, LiveReload, BrokerCapture,
  EditorIdleGuard, BrokerFiles.
- Project-agnostic: project specifics live in `project.json` and `routes.json` (templates: `*.example.json`). Both are
  optional: without `routes.json` the main route is the checkout the broker sits in, on its current branch.
- `install.py` (`config`, `claude`, `claude-hooks`, `codex`, `all`) writes every config from the Python running it and
  the broker's own location, so no path is typed by hand. `Open Dashboard.cmd` finds the installer's `pythonw`, then
  `py`, then `pythonw` on PATH.
- `commit.py --untrack` stops tracking a file in the same private-index commit.
- MIT licence; Windows CI (`.github/workflows/tests.yml`, Python 3.12 and 3.13).
- A standalone player patch prototype (`live-reload/`); the project supplies the bridge's C#.
