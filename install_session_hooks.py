"""Install reviewable Codex hooks in explicitly selected worktrees."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from broker_state import HERE, PROJECT_NAME, worktrees


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("worktree", nargs="+", type=Path)
    args = parser.parse_args()
    known = {Path(row["path"]) for row in worktrees()}
    for path in args.worktree:
        root = path.resolve()
        if root not in known:
            parser.error(f"{root} is not a worktree of {PROJECT_NAME}")
        command = subprocess.list2cmdline(
            [sys.executable, str(HERE / "session_hook.py"), "--worktree", str(root)]
        )
        hook = {"type": "command", "command": command, "timeout": 10}
        events = ("SessionStart", "UserPromptSubmit", "Stop", "Interrupt", "SessionEnd")
        config = {"hooks": {event: [{"hooks": [hook]}] for event in events}}
        target = root / ".codex" / "hooks.json"
        target.parent.mkdir(exist_ok=True)
        with target.open("x", encoding="utf-8") as file:
            json.dump(config, file, indent=2)
        print(target)
