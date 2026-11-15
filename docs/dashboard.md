# The dashboard

Pages, chat states, the editor line, machine headroom and token usage. Part of [Worktree Broker for Unity](../README.md).

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
