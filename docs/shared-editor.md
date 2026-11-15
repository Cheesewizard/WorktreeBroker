# The shared editor

How the main editor is shared: pinning, the lock and time limits, the editor lease, compile status, the activity log and the idle Play mode guard. Part of [Worktree Broker for Unity](../README.md).

## Pinning, the lock and time limits (editor route)

On the main checkout, the broker wraps Unity MCP (by default `mcpforunityserver==10.3.0`, started through `uvx`;
`unity_mcp.server` in `project.json` sets the version):

- `set_active_instance` and `unity_instance` arguments are removed or refused, and `batch_execute` can't smuggle them in.
  Before each call the broker finds the one running editor whose project is this checkout's `Assets` folder and pins it.
- It refuses if the checkout isn't on the configured development branch. It never switches, stashes or updates the
  checkout.
- Calls take the shared lock (`.runs/main/editor.lock`) and wait up to 30 s for it. While a call holds it,
  `.runs/main/editor-holder.json` names the client, worktree, tool and start time (`editor_holder.py`), so a refusal says
  who is using the editor and when it will be freed; the dashboard's editor line shows it too.
- **Every call has a hard limit** (`editor_holder.LIMITS`: 4 min by default, `run_tests` 15, `refresh_unity` 7,
  `manage_build` 30). When one runs out, the broker cancels it, frees the lock and restarts its Unity MCP connection
  (`LazyUpstream.restart`), and the caller gets an error saying so.
  Without the limit, a call caught by a domain reload would hold the lock for Unity MCP's own ~30 min timeout.
- Claude Code can't call raw Unity MCP in the project: the project hook `.claude/settings.json` ->
  `block_direct_unity.py` refuses `mcp__unityMCP__*` and points at the broker (the desktop app gives every project a
  direct `unityMCP`). Codex has it switched off in `.codex/config.toml`.
- `worktree_editor_resource` reads `mcpforunity://editor/state` or `mcpforunity://project/info` for a read-only check.

The lock covers one call. For a sequence of calls (enter Play mode, step, capture), take a **lease** (below). Long
calls (compiles, test runs) can still make others give up after 30 s. Claude Code sessions don't go through the broker
unless they're configured to (the project `.mcp.json` does that).

**Fast start.** Unity MCP starts in the background (`LazyUpstream` in `broker.py`): starting it through `uvx` takes
about 20 s, and Claude Code drops an MCP server that hasn't answered in 30 s. The broker answers `initialize` at
once (2-7 s, mostly importing the `mcp` package), lists tools from the copy cached at the last good start
(`.runs/main/upstream-tools.json`), and holds tool calls until Unity MCP is up (at most 120 s). If many sessions
start at the same moment it can still miss Claude Code's 30 s; reconnect it from `/mcp`.

## Editor lease (`editor_lease`, `editor_release`)

A lease reserves Main Unity across several calls so no other chat's calls run in between (`lease.py`). Any chat can take
one, from the main checkout or a worktree (the UI tools use the same editor).

- `editor_lease {reason, minutes}` takes it, or renews it if you already hold it. A grant lasts 1-20 minutes (default
  5). A chain of renewals stops at 30 minutes; then the same chat must wait 60 s before taking it again, so a waiting
  chat gets its turn.
- While it is held, other chats' editor calls wait up to 30 s, then fail with the holder, the reason and the time left.
  Their own `editor_lease` is refused the same way.
- `editor_release` ends it. It also ends by itself when it runs out, or when the holder's broker process (its chat)
  ends, so a crashed chat never leaves the editor reserved.
- A lease also lapses when its holder makes no editor call for `lease_idle_minutes` (`project.json`, default 5) and has no
  call running. Each of the holder's editor calls marks the lease used at its start and end (`lease.touch()` in
  `editor_turn`). The "leased by" message says how long until it lapses if the holder stays idle. This frees the editor
  when a chat finishes without calling `editor_release`. Restart running brokers after updating the broker.
- The dashboard shows the holder on the main editor line, and **Free the editor** ends any lease.

Use it for multi-step Play mode work. Single calls don't need it.

## Compile status (`editor_compile_status`)

Every chat shares one main checkout and one editor, so one chat's unfinished file breaks the build for all of them.
The package's `WorktreeBroker.Editor.CompileReport` (see "Editor side" in [Project configuration](configuration.md#project-configuration)) writes each
compile's result to `<project>/Library/WorktreeBroker/compile-status.json` (`ok`, then each error's file, line, message
and assembly). While the last compile failed:

- every Main Unity call (editor tools and the UI tools) carries a note at the end of its result. It lists the broken
  files, whether each is uncommitted, when it changed and which AI session last edited it, and tells the chat not to
  fix or revert someone else's work in progress without asking;
- `editor_compile_status` returns the full list;
- the dashboard shows a red line under the main editor line, for example "Main doesn't compile: 3 errors in X.cs
  (uncommitted)".

"Last edited by" comes from a Claude Code `PostToolUse` hook on Edit, Write, MultiEdit and NotebookEdit
(`~/.claude/settings.json`, async, like the other hooks). It runs `session_hook.py`, which appends the session id and
file to `.sessions/edits.jsonl`, but only for `.cs`, `.asmdef`, `.uss` and `.uxml` files. Codex edits aren't recorded
yet, so for them the note only has git status and the change time.

## Activity log and the editor guard

A shared editor left in Play mode (on a menu, or behind a half-finished capture) blocks compiles and imports for every
chat. The activity log records editor use, and the guard stops Play mode nobody is using.

**Activity log.** Every broker tool call is appended to `.runs/activity.jsonl`: time, route, worktree, tool, seconds
and ok, but never arguments or results. The file rotates at 2 MB. Calls that touch the main editor also refresh
`<project>/Library/WorktreeBroker/editor-last-activity`, a one-line timestamp. Broker-only tools (`broker_status`,
`sync_*`, `bot_*` and so on) don't count as editor use.

JSON state files (leases, presence, caches, run manifests) are written atomically (temporary file, then replace), with a
retry while the dashboard has the file open. Temporary files are never left behind.

**Housekeeping** (`housekeeping.py`). Every broker prunes old run data in a background thread when it starts, at most once
an hour (marker `.runs/housekeeping.stamp`). `python housekeeping.py` forces a pass. What is kept:

- captures: the newest 60 per route and anything from the last day, with their layout JSON;
- verify work folders used in the last 7 days;
- sync previews for 3 days;
- published snapshots for 14 days (their `refs/worktree-broker/snapshots/<id>` refs are deleted with them);
- player runs for 7 days, unless still running;
- session files, except Closed ones older than a day and any untouched for 7 days.

**Editor guard** (the package's `WorktreeBroker.Editor.EditorIdleGuard`, inside Unity; see "Editor side" in
[Project configuration](configuration.md#project-configuration)). It only stops **Play mode**. It never
closes the editor, saves, or touches anything outside Play mode, because closing the editor would only make the next
caller wait for a cold start. It acts only when nobody has focused the editor window for the whole wait:

- **Stalled:** playing, not paused, and no new frame for 5 minutes. If run-in-background was off (the usual reason an
  unfocused game stops ticking), it turns that on first and gives the game another minute.
- **Idle:** no broker call touched the editor for 10 minutes, counted from Play mode's start at the earliest. A paused
  game (a capture, which steps paused Play mode, leaves it paused between steps) counts too.
- **Too long:** Play mode has run 30 minutes with nobody at the editor, whatever the broker traffic. This catches a chat
  polling a stuck capture: each poll counts as use and steps frames, so the two rules above never fire. A console
  warning goes out at 25 minutes, so a working chat can capture what it needs first.

Each action goes to the Unity console as an `[EditorIdleGuard]` warning, which agents polling the console see, and to
`<project>/Library/WorktreeBroker/editor-guard.jsonl` for the dashboard. Turn it off or on from **Tools > Worktree
Broker > Idle Play Mode Guard**, and the settings are EditorPrefs (`WorktreeBroker.EditorIdleGuard.Enabled`,
`.IdleMinutes`, `.StallMinutes`, `.MaxUnfocusedMinutes`). An agent mid-task that sees its
Play mode stopped should check the console for the guard's line, then start again.
