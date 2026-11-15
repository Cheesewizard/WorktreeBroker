# Bot matches (player route)

Running a built player from a worktree. Part of [Worktree Broker for Unity](../README.md).

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
