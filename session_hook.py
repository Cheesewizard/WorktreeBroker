"""Record AI session lifecycle metadata (Codex and Claude Code) without retaining prompts or transcripts.

Codex worktrees pass --worktree. Claude Code runs this from user-level hooks for every project, so without
--worktree the session's cwd is resolved to a worktree of the project and anything else is ignored. A hook must never
block or fail a session: every error is swallowed and the exit code is always 0.
"""

import argparse
import json
from pathlib import Path
import sys


def main():
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--worktree", type=Path)
        parser.add_argument("--source", default="codex", choices=("codex", "claude"))
        args = parser.parse_args()
        payload = json.load(sys.stdin)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from broker_state import project_root_for, record_session

        root = args.worktree.resolve() if args.worktree else project_root_for(payload.get("cwd"))
        if root is None:
            return
        if payload.get("hook_event_name") == "PostToolUse":
            # Who last edited which file, so a compile error can name the chat that made it (compile_status.py).
            file_path = (payload.get("tool_input") or {}).get("file_path") or (payload.get("tool_input") or {}).get("notebook_path")
            if file_path and Path(file_path).suffix.lower() in {".cs", ".asmdef", ".uss", ".uxml"}:
                from compile_status import record_edit

                record_edit(payload.get("session_id"), args.source, root, Path(file_path).resolve())
            return
        record_session(payload, root, args.source)
    except Exception:  # noqa: BLE001 - never break the session that ran the hook
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
