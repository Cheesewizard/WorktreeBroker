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
7. Optional: add your own `verify` checks to `project.json` ("Bring your own compile check" below, and
   `project.example.json`).

"Setup" and "Project configuration" below have the details. "Token cost" says what the broker costs each chat in context,
and "Known limitations" lists what isn't done yet.

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

## Feature: UI live reload (`ui_live_reload`, `ui_revert`, `ui_capture`)

See a view change on screen in well under a second of editor time, from any worktree, in the one shared editor. Both
halves of a UI Toolkit view reload: its **stylesheets** (`.uss`, where the look lives) and its **C# rebuild methods**
(where structure and state-driven classes live).

**What happens on a call**

1. The broker checks every file is a saved `.uss` or `.cs` inside *your* worktree.
2. It takes the main editor's lock, checks the main checkout's branch, and finds Unity's Pipeline server
   (`Library/Pipeline/.unity-pipeline-port`: loopback port and token).
3. It refuses unless the editor is idle and in Play mode (playing or paused). It never starts or stops Play mode
   itself, because that would disturb whoever set up the screen.
4. **Stylesheets** (`styles`): the package's editor-only `WorktreeBroker.Editor.LiveStyles` (the default
   `editor.style_apply`) compiles each file's text into the main project's stylesheet at the same `Assets/...` path, **in
   memory**, with UI Toolkit's own importer, then restyles every panel. About 0.25-0.45 s warm. The main project's `.uss`
   file is never written.
5. **C#** (`source`): Unity recompiles just the `[MethodReload]` methods in your file, from your worktree's path, and
   swaps them into the running game. 0.1-0.2 s warm, about 1.8 s the first time in a session. The package's runtime
   `WorktreeBroker.LiveReload` then reruns those rebuild methods on the live views.
6. The capture hook (`editor.capture`; the package's `WorktreeBroker.Editor.BrokerCapture.ForBroker` by default) renders
   the UI, and you get back the results, any new console warnings or errors, the PNG path and the image itself.

A broken stylesheet comes back as an error with UI Toolkit's own line-numbered messages (for example
`Unknown property 'colr' (did you mean 'color'?)`), and that stylesheet is restored from the main project's file, so a
broken file never stays on screen. A failed C# reload (a compile error, or a private member with `backend: compiled`)
comes back as an error with Unity's reason.

**Arguments**

| Argument | Default | Meaning |
| --- | --- | --- |
| `styles` | none | Saved `.uss` files, relative to your worktree (same `Assets/...` path as in the main project). Applied first. |
| `source` | none | Saved `.cs` file, relative to your worktree, e.g. `Assets/<Project>/Presentation/Hud/MainMenuView.cs`. |
| `backend` | `interpreter` | For `source`. `interpreter`: the body may use private fields and UI Toolkit freely. `compiled`: full C#, public members only. |
| `capture` | `true` | Return a screenshot afterwards. |
| `width`, `height` | 1280 x 720 | Screenshot size. |

Give `styles`, `source`, or both.

`ui_revert` puts everything back: stylesheets are re-imported from the main project's files, and every C# reload is
dropped (back to the compiled code) without a domain reload. Stylesheet overrides are also reverted automatically when
Play mode ends; C# reloads end with Play mode by themselves. `ui_capture` (optional `width`, `height`) takes a screenshot
without changing anything.

**Stylesheets first.** The look belongs in USS: anything a C# method sets inline overrides the stylesheets. So tune colours, sizes, spacing and positions in `.uss` with `styles`, and use tagged C# only
for structure and for which classes an element carries. A tagged method that sets layout numbers inline is a smell.

**What a view needs for C# reload (one-time, in the project's code; `LiveReload.Track` is the package's hook)**

```csharp
using WorktreeBroker;

public MyView()
{
    // ...build the view...
    LiveReload.Track(this);            // the package's hook; editor-only, stripped from player builds
}

[Unity.Pipeline.MethodReload.MethodReload]
public void ApplyState()               // public, void, no parameters, safe to run twice
{
    panel.EnableInClassList("my-panel--compact", isCompact);   // classes here, numbers in USS
}
```

The tag has to be in the code the editor compiled **before** entering Play mode, so tag the method (and get it into the
main checkout, or have the main editor recompile) before you start iterating. Stylesheets need nothing: any `.uss`
that already exists in the main project can be reloaded.

**Workflow from a worktree chat**

1. Make sure the main editor is in Play mode on the screen you're changing. Ask whoever is at the editor, or use your
   project's own way of opening a screen (a test scene or fixture). The broker won't change Play mode for you.
2. Edit the `.uss` (and, if needed, the tagged method) in your worktree and save.
3. Call `ui_live_reload` with the files. Look at the returned image.
4. Repeat. When it looks right, call `ui_revert`; the change goes through your branch as usual.

**What it can't do**

- A stylesheet must already exist in the main project. A brand-new `.uss` file, or a new `@import`, needs a normal
  import in the main editor. `@import` lines in your file are ignored: the main project's import list is kept, and the
  result warns when yours differs. `url()` paths resolve against the main project's copy, so new images in your
  worktree aren't seen.
- UXML isn't reloaded.
- For C#, only bodies of tagged methods change. New fields, new tagged methods and signature changes need a normal
  compile in the main editor. Fast Script Reload still covers untagged method bodies there.
- The interpreter can't call every .NET API (for example `System.IO.File`). Unity reports an unsupported call as a
  warning when you reload and as an error when it runs.
- Overrides are shared: until `ui_revert` or the end of Play mode, other chats' captures show your changes too.
- The main checkout must be on the branch in `routes.json`. A tag added only on a feature branch exists in the editor
  only after the main checkout has it.
- If a call hits its time limit, the broker keeps the editor lock until the worker thread really ends (bounded), so
  another chat's calls never interleave with work still talking to Unity.
- Timing: the Unity side is 0.25-2 s. A broker that's already running (a normal MCP session) adds little.
  `broker_client.py` from a terminal pays Python's start and the `mcp` import (about 4 s, more on a busy machine).

**Player builds.** Live reload is editor-only. Install `com.unity.pipeline` with both of its runtime toggles off, so
player builds contain no Pipeline runtime, compiler, interpreter or command server. Don't turn the toggles on for builds
you share. To check a build, look in its `<Player>_Data/Managed` folder: with the toggles off, the only Pipeline file is a
small attributes DLL.

### Cheaper captures (`view`, `region`, `element`)

A chat pays for every image by its pixels (about width x height / 750 tokens for Claude) and keeps paying while it stays
in context: a 1280 x 720 capture costs about 1,200 tokens, a 1080p frame about 2,760. The capture is still rendered at `width` x `height`, since layout depends on it; only what comes
back changes (`capture_view.py`):

| `view` | Returns | About |
| --- | --- | --- |
| `fit` (default) | the whole frame scaled to 768 px on its long edge | 440 tokens |
| `crop` | `region` [x, y, w, h] in capture pixels, or the first UI element matching `element`, at full resolution, 16 px padding | a 828 x 206 button row: 227 |
| `full` | the frame as rendered | 1,229 at 720p |
| `none` | no image (the PNG path is still in the result) | 0 |

Every capture also writes `layout/<capture>.json` beside it, if the capture hook does that (the package's
`BrokerCapture.WriteLayout` does). With
`element` (a substring of a name, class or type), the matching visible elements come back as text: bounds in capture
pixels, text and font size. That often answers "did it move, wrap or fit" without an image. A crop that matches nothing
lists the visible classes.

## Feature: the shared editor (editor route)

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

## Feature: editor lease (`editor_lease`, `editor_release`)

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

## Feature: compile status (`editor_compile_status`)

Every chat shares one main checkout and one editor, so one chat's unfinished file breaks the build for all of them.
The package's `WorktreeBroker.Editor.CompileReport` (see "Editor side" under "Project configuration") writes each
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

## Feature: verify a worktree (`verify`, `verify_status`)

A worktree chat can check its own saved files without the shared editor (`verify.py`). The checks are whatever the
project's `project.json` lists under `verify.checks` (see "Project configuration"): each is a named list of command
steps run against the worktree, with an optional parser for the output (`unity-compile-check`, `dotnet-test` or
`lines`), and `verify.default` names the check that runs when none is given. A check can be marked to run first, or to
skip the machine-wide queue when it is fast. Build output goes to `.runs/verify/<worktree>/...`, never into the worktree.
A project with no `verify` section has this tool switched off.

At most two verifies run at once on the machine; a third waits for a slot. Only one verify runs per worktree at a time
(they share the work folder); a second one for the same worktree waits. A check that exceeds its time limit is killed with
its whole process tree (`taskkill /T`), and MSBuild node reuse is off for checks, so no build node outlives its run. `verify` waits up to 50 s, then returns a
`run_id`; `verify_status {run_id}` waits up to 50 s again. Codex gives an MCP tool 60 s by default, so nothing times
out. The result is pass/fail with the first 40 errors or failing tests, plus the path of the full log.

### Bring your own compile check

The broker runs checks; it ships none, because a compile outside the editor depends on the project's assemblies, Unity
version and packages. Any command works. Pick the parser that matches its output:

- `unity-compile-check`: one line per assembly, `ok   <assembly>` (`ok` and three spaces) or `FAIL <assembly>`, and csc
  errors whose file paths run through `.../gen/<assembly>/src/<file>(<line>,<col>): error CSxxxx: <message> [`. The
  result lists the failed assemblies and the first 40 errors.
- `dotnet-test`: `dotnet test` output (totals, failing tests, build errors).
- `lines` (default): anything else. The result is the first 40 lines of output and the exit code decides pass or fail.

A simple way to write one: generate a `.csproj` per `.asmdef` in the worktree that references the main checkout's
`Library/ScriptAssemblies/*.dll` and the installed editor's `Data/Managed/UnityEngine/*.dll`, run `dotnet build` on each,
and print the lines above. Point the step at the main checkout's copy (`{main}/...`) so older worktrees run the current
checker, and list it under `requires` so the check is skipped, not failed, where the script doesn't exist. The reference
project's checker works this way and takes about 2-3 minutes for 13 assemblies.

### Example: three checks

A typical setup (see `project.example.json`):

- `compile` (default): a compile check like the one above, run with the main checkout's copy of the script.
- `tests`: `dotnet test` on the project's test project, with `filter_args` so a chat can pass `test_filter` (a dotnet
  `--filter`, for example `FullyQualifiedName~Save`) to run a slice.
- `lint`: a fast project rule (for example "no inline styles in view code"), marked `"slot": false` and listed in the
  compile check's `runs_first`, so it is reported with every compile without blocking it.

## Feature: worktree drift (`worktree_freshness`, `worktree_update`)

A worktree starts from the last commit, while the main checkout may carry uncommitted work and newer commits. A chat can
then compile, test or live-reload an older copy of a file than the one in main, or redesign something another chat has
already changed. `freshness.py` measures two kinds of drift:

- **behind**: commits on the development branch the worktree doesn't have, and how many files main has uncommitted
  (work no worktree can see until it is committed);
- **overlap (DRIFT)**: files this worktree changed (committed or not) that main also changed since the worktree's base,
  committed or still uncommitted, with the AI session that last edited main's copy when the edit hook recorded it.
  Overlap is the dangerous case: live-reloading or merging this worktree's version undoes or contradicts the other work.

Where it shows: `worktree_capabilities` carries the summary (so a chat sees it at the start); `verify`, `verify_status`
and the UI tools add a `drift` note while there is overlap, or the base is 10+ commits or 24 h old; `worktree_freshness`
gives the full report. It costs about 1.5-2.5 s (git runs in parallel, without rename detection) and is cached for 60 s. Per-worktree wiring (`.codex/`,
`.claude/`, `.mcp.json`) is ignored.

`worktree_update` merges the development branch into the worktree. Safety, in order:

1. a detached HEAD or a merge in progress is refused;
2. `git merge-tree --write-tree` checks for conflicts on trees only (no file, index or ref is touched); a conflict changes
   nothing and lists the files: they are where two sessions disagree, so a person decides which version wins;
3. if the merge would change a file the worktree has uncommitted edits in, it is refused (no stash, so nothing ends up
   hidden): commit those in the worktree first;
4. otherwise `git merge --no-edit` (a merge commit, never a rebase or reset); other uncommitted files are left alone.

`include_uncommitted: true` also merges a snapshot of main's uncommitted files, built with a temporary index (main's own
index and files are untouched) and committed on top of the branch tip with a message saying it is other sessions'
unfinished work, not this branch's. Use it only to test against main as it is right now; don't hand that commit back.
`dry_run: true` reports what would happen. Command line: `python freshness.py <worktree>` prints the report.

## Feature: commits that can't race (`commit_paths`, `commit.py`)

Two `git commit` runs at once in one checkout race on its shared index: one fails with "cannot lock ref 'HEAD'", or the
index is left built from the old commit. And `git add` plus `git commit` sweeps in whatever another chat staged.

`commit.py` commits only the named paths: one commit at a time per checkout (a lock in the git dir), built in a private
index (`read-tree HEAD`, `add -A -- <paths>`, `write-tree`, `commit-tree`), so the shared index and other chats' staged
files are never read for the commit. HEAD moves by compare-and-swap (`update-ref HEAD <new> <old>`); if another commit
lands in between, it rebuilds on the new HEAD (3 tries). Then only the committed paths are refreshed in the shared index
and the post-commit hook runs (Git LFS). A repository with pre-commit, prepare-commit-msg or commit-msg hooks is refused
rather than skipping them. It never commits everything and never pushes. It refuses `.`, the repository root, paths
outside the repository and git pathspec magic (`:...`), so one call can't sweep in other chats' work.

```
python tools/WorktreeBroker/commit.py -m "Subject" -- path/one path/two
python tools/WorktreeBroker/commit.py -F message.txt --dry-run -- path
python tools/WorktreeBroker/commit.py -m "Stop tracking local config" --untrack routes.json -- .gitignore
```

`--untrack <path>` (repeatable, command line only) stops tracking a file but keeps it on disk, like `git rm --cached`,
inside the same private-index commit, so nothing is staged in the shared index first.

Through the broker: `commit_paths {paths, message, dry_run}` on every route.

## Feature: combined view of several worktrees (`integrate`, `integrate_status`, `integrate_list`)

`integrate` answers "what do all the in-flight worktrees look like together?" without touching any of them (`integrate.py`).

1. For each worktree it takes the saved state as a git tree: HEAD plus, with `include_uncommitted` (default), its saved
   tracked and untracked changes under `Assets`, `Packages`, `ProjectSettings` and `integrate.extra_paths`, captured
   through a temporary index (as `sync_publish` does). The tree is wrapped in a commit whose parent is that HEAD.
2. Starting at `base` (default: the main route's development branch) it folds the commits with `git merge-tree
   --write-tree`. A worktree that conflicts is **skipped** and reported with its conflicted paths; the rest continue.
3. The final commit is pinned at `refs/worktree-broker/integrations/<id>`, and its code and UI files (`.cs`, `.uss`,
   `.uxml`, `.asmdef`, `.json`, ...) are extracted to `.runs/integrate/<id>/tree/` with `manifest.json` (included with
   head/commit/changed paths, skipped with conflicts, final commit, files changed against base). Submodule files and
   `Library/ScriptAssemblies` (a junction) are borrowed from the main checkout so the tree compiles.
   Only the newest 5 integrations are kept.

**Safety:** object-only git (temporary index, `merge-tree`, `commit-tree`, `archive`, `update-ref` under
`refs/worktree-broker/integrations/` only). No worktree, index, branch or checkout is written, and `test_integrate.py`
checks every worktree's HEAD and status are unchanged.

| Argument | Default | Meaning |
| --- | --- | --- |
| `worktrees` | required | List of worktree paths, or `"all"` (every worktree except the main checkout). |
| `base` | main's development branch | Any git revision. |
| `include_uncommitted` | `true` | Include each worktree's saved changes, not only its commits. |
| `verify` | `none` | `compile`: also compile the combined tree (waits behind the two verify slots). |

It waits up to 50 s and then returns a `run_id` for `integrate_status`. `integrate_list` shows recent ids.

**View the result:** `ui_live_reload {integration: "<id>", styles: ["Assets/..."], source: "Assets/..."}` resolves those
paths inside the combined tree and applies them in Main Unity's running game (same validation, same lock), so several
worktrees' screens show overlaid on main. Or `verify compile` through `integrate {verify: "compile"}`.

Example: `integrate {worktrees: "all"}` -> `{id: "20261110-231500-ab12", included: [...], skipped: [{worktree, conflicts: [...]}], changed_files: 14}`.

## Feature: activity log and the editor guard

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

**Editor guard** (the package's `WorktreeBroker.Editor.EditorIdleGuard`, inside Unity; see "Editor side" under
"Project configuration"). It only stops **Play mode**. It never
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

## Feature: sharing saved edits between worktrees (sync)

1. In the publishing worktree, call `sync_publish` with a label and the project-relative paths to share (include `.meta`
   files). The broker snapshots the saved contents, including untracked and deleted files, through a temporary git index
   and pins the tree under `refs/worktree-broker/snapshots/<id>`.
2. In another worktree, `sync_list` shows the snapshots. `sync_preview` with a snapshot id and your own paths makes a
   three-way merge.
3. A ready preview has an `incoming.diff` and a folder of the merged files under `.sync/previews/<id>`. Its `manifest.json`
   lists writes and deletions. A conflict lists the paths and writes nothing.

Neither checkout, its index nor the development branch is touched. Previews are files to read; they don't run in Unity.

## Feature: bot matches from a worktree (player route)

`bot_start` launches the prepared player configured in `routes.json`, using `project.json` `player.executable` with its
configured arguments, for an automated match. It first checks for at
least 2 GiB of free RAM and 4 GiB of commit headroom, and that no match is already running for this worktree. With a
`startup_patch`, it applies the configured C# and PNG once the first bot turn starts. `bot_status` returns the run, the
process state and the last log lines. `preview_patch` applies one C# + PNG patch to a running preview (one per run).
Runs and logs are under `.runs/<route>/`. To keep a match running after the chat ends, run
`python broker.py --worktree <path> --start-bot` from a terminal.

The patch receiver is a prototype (`live-reload/`): one fixed entry point and one patch per run. `PreparePlayer.cs`
takes the hook and the bridge's source from `project.json`; `live-reload/README.md` shows the shape of a bridge.
Unity's Pipeline player reload could replace it in future.

## Feature: the dashboard

Run `python dashboard.py` or double-click `Open Dashboard.cmd`.

**Layout.** A docked page rail on the left (Overview, Worktrees, Activity, Machine, Usage), a header with three status chips (editor, chats,
memory; click one to open its page) and a status bar. Badges on the rail count chats that need you or are working,
calls that failed or were cut off in the last hour, and low memory. Styling is `dashboard_ui.py` (one palette, ttk
styles on Tk's `clam` theme, cards, pills, meters; no third-party theme). The window is DPI aware, so pixel sizes go
through `dashboard_ui.px` to scale with the display.

- **Overview.** The shared editor as a card: its state (Free, Leased, In use, In Play mode, Compiling, Not answering),
  the call running now with a bar towards its time limit, the lease with a bar towards its expiry, the last broker call
  and the guard's last action, plus **Free the editor**. Beside it the main checkout's compile (the broken files, if
  any). Below, every live chat (and any not closed in the last 6 h) across worktrees, and machine headroom: RAM,
  commit and GPU meters that turn amber and red, the Unity editors and players using memory, and a low-memory warning.
  Double-click a chat to open it on the Worktrees page.
- **Worktrees.** The table below, with a name/branch filter and **Show idle worktrees** (off by default: a worktree is
  idle without a Unity window or a chat in the last day; closed chats show only with it on). Chats carry a short id
  (`Claude Code · pid 50228`, `Claude Code · 3f2a`). A details pane shows the selection with **Jump to window**, **Open
  folder**, **Start player** (and Pause / Resume / Recover with a Codex control host).
- **Activity.** The call log (`.runs/activity.jsonl`) and the guard's actions (`editor-guard.jsonl`, in `<project>/Library/WorktreeBroker/`), newest first:
  time, chat, worktree, tool, duration and result. Slow calls (60 s+) are amber; failed calls are red with the error
  message (the log now keeps it, never arguments or results); calls the broker cut off at their limit say so. Filter by
  editor calls, problems or guard actions; the summary gives the last hour's calls, failures and longest call.
- **Machine.** Memory and per-GPU cards, capacity for more editors and players, every Unity process with its helpers
  and GPU memory, and the largest process groups.

**Worktrees page.** One row per worktree of the project, with its AI chats under it, named by their MCP client (Claude Code,
Codex, Cursor, ...); a Claude chat and a Codex chat can work side by side in different worktrees. The most urgent rows
sort to the top:

| State | Colour | Meaning |
| --- | --- | --- |
| **Needs you** | bold amber | A Claude Code session is waiting on a permission prompt or a question. |
| **Working** | green | A turn is running. After 2 hours with no news it shows **Working? (quiet)**, and after 6 hours **Stopped** (the chat ended without its Stop hook). |
| **Your turn** | blue | The last turn finished; the chat is waiting for your next message. |
| **Connected** | teal | The chat's broker is running but no hook says more (any MCP client; no hooks needed). |
| **Stopped** | grey | The turn was interrupted, failed, or never recorded an end. |
| **Closed** | grey | The session ended (hidden after an hour). |

A worktree row shows its most urgent chat state, with a count when several share it. Rows with a chat that needs
you or is working open automatically. "Last activity" is relative ("3 min ago"). **Click any column header to sort by
it, and again to reverse**; an arrow marks the current sort (default: AI session, most urgent first). Double-click a row with a Unity window (or press **Jump to window**) to bring that window forward. It
checks first that the window still belongs to the same process.

Under the title, the **main editor line** says whether the shared editor is free: *free (not in Play mode)*,
*in Play mode N min*, *compiling*, or *not answering* (closed, busy or reloading). It adds when a broker call last used
the editor and the guard's last action. It turns amber when Play mode has gone 10 minutes without broker use.
**Free the editor** stops Play mode in the main editor (the same Pipeline call an agent would make) and leaves the
editor open. The editor answers on its main thread, which an unfocused editor ticks slowly (replies take 2-4 s), so this
line is read on its own thread every 5 s and never delays the worktree rows.

The footer says **Updated N s ago (refresh took X s)**, so stale data is never mistaken for live data. A refresh
normally takes 0.1-0.5 s.

**Where chat state comes from.** Two places, merged per worktree (`broker_state.chats`). Every broker process writes
`.runs/servers/<pid>.json` (`presence.py`): the client name from the MCP handshake (`clientInfo`), its parent processes,
the worktree, and whether a tool call is running. That alone lists a chat as **Connected** or **Working**, from any
client, with or without hooks; the file is removed when the broker exits and ignored once its process is gone. Hooks
(optional) write one small file per session in `.sessions/`: the session id, worktree, state, source, time and parent
processes, which match the session to its broker and add **Needs you** / **Your turn**. A hook session with no broker is
listed with "not connected to the broker". The lease holder's chat says "holds the editor lease", and the lease holder
name is the client ("Codex in 857f (broker 1234)"). No prompts or transcripts are ever recorded.
- **Claude Code:** user-level hooks in `~/.claude/settings.json` (SessionStart, UserPromptSubmit, Notification, Stop,
  SessionEnd) run `session_hook.py --source claude` **asynchronously**, so they never slow a session. Sessions outside
  one of the project's worktrees are ignored: the hook checks that the folder's git common directory is the main checkout's.
- **Codex:** each worktree's `.codex/hooks.json` runs `session_hook.py --worktree <path>` (timeout 10 s). Codex asks you to review changed hooks once with `/hooks`.
- The hook never fails or blocks its session: every error is swallowed and it always exits 0.

**Start player** launches the bot route's match, with the same checks as `bot_start`.

**Performance tab.** CPU, physical RAM, Windows commit, and dedicated and shared memory per GPU, then a line answering
"can I start something?": how many Unity editors are running, and whether there is room for another player (about
1 GiB) or editor (about 5 GiB) while keeping 2 GiB free. The table lists **every** Unity editor and project player on
the machine, not only the worktrees' own: one row per process, with its helper processes (import workers, shader
compilers and similar) folded in as a count and total. An editor whose project isn't a worktree is marked **not a worktree** in amber. Every header sorts.

**CPU** is a share of the whole machine (all logical processors), like Task Manager, averaged over the time since the
previous sample (5 s while the Overview or Machine page is open), so the first sample after opening shows "measuring".
It appears as a header chip, a meter on the Overview, a card on the Machine page (all processes, and the Unity editors
and players together), a **CPU with helpers** column, and the busiest process groups under the table. Per-process CPU
time comes from the same process-table call (user plus kernel time), keyed by pid and start time so a reused pid never
borrows another process's time; the machine total is Windows' own (psutil). The per-process sum runs below the total:
interrupt time and processes that exited during the interval belong to no running process. Samples take about 0.05 s, because the
whole process table comes from one Windows call (`procs.py`, `NtQuerySystemInformation`).

**AI session controls** (Codex only; needs `BROKER_CODEX_URL` pointing at a loopback Codex App Server, optional
`BROKER_CODEX_TOKEN`). **Pause AI** interrupts a running turn. **Resume AI** continues an interrupted chat in its own
worktree, telling it to check existing results first. **Recover all** resumes unfinished or failed chats one at a time.
**Refresh** reads chat state without resuming anything.

### The Usage page (`usage.py`)

Token use per worktree and per chat. No hooks are needed: Claude Code and Codex already log exact usage for every model
call in their transcripts, and `usage.py` reads them.

- **Sources.** Claude Code: `~/.claude/projects/<folder>/<session>.jsonl`, plus `<session>/subagents/*.jsonl` counted
  toward their chat. Each assistant entry carries cwd, session, model and usage; a streamed reply is logged several times
  under one message id, so each call counts once. Codex: `~/.codex/sessions/YYYY/MM/DD/*.jsonl`, using the session's
  cwd and the change in its running `total_token_usage`. Only the project's folders count.
- **Cards.**
  - Tokens in the period, with the part that isn't cache reads (new input, cache writes and output are what use a plan
    up; cache reads are cheap).
  - Claude's plan limit: Claude logs a `quotaLimits` entry only when a call hits a limit, so the card shows "limit
    reached, resets at" or when it was last hit. It isn't a share of the allowance.
  - Codex's plan limit: Codex logs `used_percent` of its window and the reset time with every token count.
- **Tables.** One row per worktree (Codex worktrees are named by their folder, e.g. "codex 857f") and one per chat:
  title, worktree, total, cache writes, output, images received, calls, last call. The period is Today, Last 2 days or
  Last 7 days.
- **Cost of reading.** Incremental: `.runs/usage-cache.json` keeps each file's byte offset and per-day, per-model sums.
  The first read of a week of logs (often several GB) takes a few minutes in the background; a refresh, every minute,
  takes under a second. Lines are filtered by substring before JSON
  parsing, and files untouched for 7 days are skipped.
- **Command line:** `python usage.py [days]` prints the same per-worktree and per-chat totals and the latest limits.

The numbers come from the clients' own logs and aren't checked against a billing page.

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

## Token cost

Every chat pays for the broker in tokens: the tool definitions sit in its context, and every result it reads is input.
The broker keeps both small:

| What | Cost |
| --- | --- |
| Tool definitions per chat | about 5,900 tokens (29 tools) with the default `unity_mcp.tools: "lean"`; about 28,300 (66 tools) with `"all"` |
| `broker_status` | about 320 tokens: active chats of the last 6 hours, the lease and the build |
| `sync_list` | id, label, worktree, age, first paths and file count per snapshot; `sync_preview` reads the full manifest |
| Results | one compact JSON text, not pretty-printed and not sent twice |
| Captures | see "Cheaper captures" (`view: fit` by default, about 440 tokens) |

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
- `verify` runs checks you configure. The broker ships no compile check of its own (see "Bring your own compile check").
- The test suite is small (`python -m pytest`): `integrate`, `install.py`, `commit.py --untrack` and the default routes.
  The other tools have no automated tests yet.

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
| `project.json`, `project.example.json` | The project's description (template: `project.example.json`; see "Project configuration"). |
| `routes.json`, `routes.example.json` | The worktrees served and their routes (template: `routes.example.json`). |
| `unity-package/` | The Unity package (`com.worktreebroker.unity`): CompileReport, LiveStyles, LiveReload, BrokerCapture, EditorIdleGuard, BrokerFiles. |
| `install.py` | Setup from this machine's paths: `config`, `claude`, `claude-hooks`, `codex`, `all`. Records the Python it ran with in `.python-path` (gitignored) for `Open Dashboard.cmd`. |
| `install_worktree_defaults.py`, `install_session_hooks.py`, `register_player.py` | Setup helpers (`install_worktree_defaults.py` is `install.py codex`). |
| `LICENSE` | MIT. The Unity package carries a copy (`unity-package/LICENSE.md`). |
| `.github/workflows/tests.yml` | Windows CI: compiles every module and runs the pytest suite. In the broker's own repository it runs as is; inside another repository, copy it to that repository's `.github/workflows/`. |
| `live-reload/` | Prototype standalone-player patcher (Cecil bridge); your project supplies the bridge's C#. |
| `CHANGELOG.md` | Release notes. |
| `.runs/`, `.sync/`, `.sessions/` | Local state: runs, captures, snapshots, chat state (gitignored). |
