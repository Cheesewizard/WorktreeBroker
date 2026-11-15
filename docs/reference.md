# Reference

Token cost, tool annotations, troubleshooting and the file list. Part of [Worktree Broker for Unity](../README.md).

## Token cost

Every chat pays for the broker in tokens: the tool definitions sit in its context, and every result it reads is input.
The broker keeps both small:

| What | Cost |
| --- | --- |
| Tool definitions per chat | about 5,900 tokens (29 tools) with the default `unity_mcp.tools: "lean"`; about 28,300 (66 tools) with `"all"` |
| `broker_status` | about 320 tokens: active chats of the last 6 hours, the lease and the build |
| `sync_list` | id, label, worktree, age, first paths and file count per snapshot; `sync_preview` reads the full manifest |
| Results | one compact JSON text, not pretty-printed and not sent twice |
| Captures | see [Cheaper captures](live-reload.md#cheaper-captures-view-region-element) (`view: fit` by default, about 440 tokens) |

A call refused by another chat's lease fails at once when the lease can't end within 30 s, instead of waiting first.
Claude Code loads MCP tool definitions only when a chat searches for them, so there the result sizes matter most; Codex
sends every definition with every request.

## Tool annotations

The broker's own tools carry MCP annotations, so a client that asks before side effects can let reads through without
a prompt. The reads are marked `readOnlyHint`: status, capabilities, `sync_list`, the integrate tools, compile and verify
status, freshness, target and editor resources. `integrate` counts as a read because it only writes git objects and a
scratch folder. `ui_revert` is the one `destructiveHint` tool, because it drops every live override in the shared
editor, other chats' included. All of them are `openWorldHint: false`, since nothing leaves the machine. The Unity MCP
tools keep the annotations Unity MCP gives them. These are hints: whether a client prompts is still up to the client
and its approval settings, and clients use them for approvals rather than putting them in the model's context.

## Troubleshooting

| Message | Meaning and fix |
| --- | --- |
| `Main Unity has no Pipeline server` | The editor isn't open, or the package isn't installed or compiled yet. Open the main editor and wait for it to settle. |
| `has not checked in for a minute` | The editor is closed, frozen, or stuck behind a modal dialog. |
| `is not in Play mode` | Enter Play mode on the screen you want (the broker won't). |
| `is compiling or reloading` | Wait for the compile or domain reload to finish. |
| Reload succeeds but nothing changes | C#: the method isn't tagged in the compiled code, or the view didn't register with the package's reload hook (`LiveReload.Track(this)`); the result's `reloaded` list shows what was swapped. USS: a stylesheet later in the cascade sets the same property with the same or higher specificity. |
| `Cannot access non-public member` | You used `backend: compiled`. Use the default interpreter backend. |
| `The main Unity checkout must be on <branch>` | The main checkout changed branch. Switch it back or update `routes.json`. |
| `The main editor is still busy after 30 seconds: X is running <tool> ...` | That chat's call holds the editor; the message says when the broker cuts it off. Wait and retry. Don't switch to raw Unity MCP; if it keeps failing, tell the user. |
| `<tool> did not finish within N s, so the broker stopped waiting` | Unity didn't answer (often a domain reload mid-call). The editor is free and Unity MCP was restarted; check `worktree_editor_resource` and retry once. Keep `execute_code` under ~20 s. |
| Console windows flash and steal focus | A broker or dashboard process started a console program (`git`, `dotnet`, `python`) without `creationflags=CREATE_NO_WINDOW`. Both run without a console (the dashboard under `pythonw`), so each such child opens a visible window. Every `subprocess` call here must pass the flag |
| A broker call that never touches Unity (`editor_compile_status`, `verify`) hangs for its whole limit | A child program inherited the broker's stdin, which is the MCP pipe with a read always pending; on Windows the child can stall behind that read, and a timed-out `git`'s own child keeps the output pipes open. Every `subprocess` call here must pass `stdin=subprocess.DEVNULL` as well as `CREATE_NO_WINDOW` |
| Play mode stopped by itself | If the project has the idle guard, look for `[EditorIdleGuard]` in the console: the game had stalled, sat unused for 10 minutes, or ran 30 minutes with nobody at the editor. Start it again. |
| `Main editor is leased by ...` | Another chat is doing multi-step work. Retry after the time it gives, or ask the user (the dashboard's **Free the editor** ends a lease). |
| A note says Main Unity's last compile failed | Someone's uncommitted file doesn't compile, and the editor runs the last good build. If it isn't yours, leave it and tell the user. `editor_compile_status` lists every file. |
| Claude Code: `<mcp_server> ... timed out after 30000ms` | Many processes started at once, for example several sessions resuming together. Reconnect from `/mcp`. A quiet start takes 2-7 s. |

## Files

| File | Purpose |
| --- | --- |
| `broker.py` | MCP server: routes, editor proxy and lock, shared tools, player tools. |
| `live_reload_tool.py` | `ui_live_reload` / `ui_revert` / `ui_capture` against Unity's Pipeline server (with the project's editor hooks from `project.json`). |
| `worktree_sync.py` | Snapshots and three-way previews. |
| `integrate.py`, `test_integrate.py` | `integrate`: the combined tree of several worktrees (object-only git); its pytest suite on a throwaway repo. |
| `broker_state.py` | Shared registry for the dashboard, hooks and MCP (worktrees, sessions, runs). |
| `dashboard.py`, `dashboard_ui.py` | The dashboard's pages and its look (palette, styles, cards, meters). |
| `windows.py`, `performance.py`, `gpu_adapters.py` | Window focus, memory and GPU counters. |
| `procs.py` | The whole process table (name, parent, working set, CPU time, start time) in one Windows call. |
| `session_hook.py`, `session_control.py`, `session_refresh.py` | Codex lifecycle hooks and pause/resume/recover. |
| `broker_client.py` | Command-line access to the tools. |
| `activity.py` | The call log (`.runs/activity.jsonl`, with client and pid) and the editor's last-use stamp. |
| `presence.py` | One file per running broker (`.runs/servers/`): client name, parents, busy, last tool. |
| `editor_state.py` | The dashboard's main editor line and its **Free the editor** button. |
| `lease.py` | The editor lease (`.runs/main/lease.json`). |
| `editor_holder.py` | Call time limits and who holds the editor lock (`.runs/<route>/editor-holder.json`). |
| `block_direct_unity.py` | Claude Code hook that refuses raw Unity MCP in the project. |
| `compile_status.py` | Reads the editor's compile result and names the broken files; records Claude Code edits. |
| `verify.py` | The `verify` tool: the configured checks (`project.json` `verify.checks`) for a worktree, two at a time. |
| `capture_view.py` | Shapes UI captures (fit, crop, element, none) and reads their layout facts. |
| `commit.py` | Path-only commits through a private index, one at a time per checkout. |
| `usage.py` | Token usage per worktree and chat from Claude Code and Codex transcripts (the Usage page). |
| `freshness.py` | Worktree drift (behind, overlap with main) and `worktree_update`'s safe merge. |
| `housekeeping.py` | Prunes old run data in the background at broker start (once an hour at most); `python housekeeping.py` forces a pass. |
| `project.json`, `project.example.json` | The project's description (template: `project.example.json`; see [Project configuration](configuration.md#project-configuration)). |
| `routes.json`, `routes.example.json` | The worktrees served and their routes (template: `routes.example.json`). |
| `unity-package/` | The Unity package (`com.worktreebroker.unity`): CompileReport, LiveStyles, LiveReload, BrokerCapture, EditorIdleGuard, BrokerFiles. |
| `install.py` | Setup from this machine's paths: `config`, `claude`, `claude-hooks`, `codex`, `all`. Records the Python it ran with in `.python-path` (gitignored) for `Open Dashboard.cmd`. |
| `install_worktree_defaults.py`, `install_session_hooks.py`, `register_player.py` | Setup helpers (`install_worktree_defaults.py` is `install.py codex`). |
| `LICENSE` | MIT. The Unity package carries a copy (`unity-package/LICENSE.md`). |
| `.github/workflows/tests.yml` | Windows CI: compiles every module and runs the pytest suite. In the broker's own repository it runs as is; inside another repository, copy it to that repository's `.github/workflows/`. |
| `live-reload/` | Prototype standalone-player patcher (Cecil bridge); your project supplies the bridge's C#. |
| `CHANGELOG.md` | Release notes. |
| `README.md`, `docs/` | The overview and quick start; the reference pages (configuration, live reload, shared editor, checks and git tools, dashboard, players, this page). |
| `.runs/`, `.sync/`, `.sessions/` | Local state: runs, captures, snapshots, chat state (gitignored). |
