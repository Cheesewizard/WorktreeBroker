"""Register an existing standalone preview by its real process and log."""

import argparse
from pathlib import Path

import psutil

from broker_state import HERE, PROJECT, RUNS, argument, read_json, write_json


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    parser.add_argument("worktree", type=Path)
    args = parser.parse_args()
    root = args.worktree.resolve()
    import broker_state

    route = next((item for item in broker_state.routes()
                  if Path(item["worktree"]).resolve() == root and item["kind"] == "player"), None)
    if route is None:
        parser.error("The worktree must have a configured player route")
    process = psutil.Process(args.pid)
    executable = Path(process.exe())
    log = argument(process.cmdline(), "-logFile")
    expected = PROJECT.get("player", {}).get("executable", "")
    if executable.name.lower() != expected.lower() or not log:
        parser.error(f"Expected {expected or 'the project.json player'} launched with -logFile")
    log_path = Path(log).resolve(strict=True)
    started = process.create_time()
    run_id = f"preview-{process.pid}-{int(started)}"
    record = RUNS / route["name"] / run_id / "run.json"
    write_json(record, {
        "run_id": run_id, "pid": process.pid, "worktree": str(root),
        "process_started_at": started, "player": str(executable), "log": str(log_path),
        "build_provenance": "Registered running preview",
    })
    print(record)
