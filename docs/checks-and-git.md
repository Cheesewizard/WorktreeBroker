# Checks and git tools

`verify`, worktree drift, path-only commits, the integrate preview and sync snapshots. Part of [Worktree Broker for Unity](../README.md).

## Verify a worktree (`verify`, `verify_status`)

A worktree chat can check its own saved files without the shared editor (`verify.py`). The checks are whatever the
project's `project.json` lists under `verify.checks` (see [Project configuration](configuration.md#project-configuration)): each is a named list of command
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

## Worktree drift (`worktree_freshness`, `worktree_update`)

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

## Commits that can't race (`commit_paths`, `commit.py`)

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

## Combined view of several worktrees (`integrate`, `integrate_status`, `integrate_list`)

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

## Sharing saved edits between worktrees (sync)

1. In the publishing worktree, call `sync_publish` with a label and the project-relative paths to share (include `.meta`
   files). The broker snapshots the saved contents, including untracked and deleted files, through a temporary git index
   and pins the tree under `refs/worktree-broker/snapshots/<id>`.
2. In another worktree, `sync_list` shows the snapshots. `sync_preview` with a snapshot id and your own paths makes a
   three-way merge.
3. A ready preview has an `incoming.diff` and a folder of the merged files under `.sync/previews/<id>`. Its `manifest.json`
   lists writes and deletions. A conflict lists the paths and writes nothing.

Neither checkout, its index nor the development branch is touched. Previews are files to read; they don't run in Unity.
