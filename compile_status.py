"""Does the main checkout compile in the shared editor, and if not, whose files break it?

The editor package's CompileReport writes `<project>/Library/WorktreeBroker/compile-status.json` after every compile.
For each broken file this adds what is known about it:
- whether it has uncommitted changes in the main checkout (`git status`), and its last change time;
- which AI session last edited it, from `.sessions/edits.jsonl` (Claude Code's PostToolUse hook on Edit/Write,
  `session_hook.py`; Codex edits are not recorded yet, so for them only git and the time are known).

`warning()` is one short paragraph the broker appends to every Main Unity call while the build is broken, so each chat
learns at once that the errors are there and probably someone else's.
"""

import json
import subprocess
import time
from pathlib import Path

from broker_state import EDITOR_FILES, HERE

STATUS = EDITOR_FILES / "compile-status.json"
EDITS = HERE / ".sessions" / "edits.jsonl"
MAX_EDIT_LOG = 1024 * 1024


def read_status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def record_edit(session_id, source, worktree, file_path):
    """Append one edit (hook side). Never raises."""
    try:
        EDITS.parent.mkdir(parents=True, exist_ok=True)
        if EDITS.exists() and EDITS.stat().st_size > MAX_EDIT_LOG:
            lines = EDITS.read_text(encoding="utf-8").splitlines()[-2000:]
            EDITS.write_text("\n".join(lines) + "\n", encoding="utf-8")
        line = json.dumps({"time": round(time.time(), 3), "session": session_id, "source": source,
                           "worktree": str(worktree), "file": str(file_path)})
        with EDITS.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _last_edits(root):
    """{relative posix path: last edit record} for edits inside root."""
    last = {}
    try:
        lines = EDITS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return last
    root = Path(root).resolve()
    for line in lines:
        try:
            item = json.loads(line)
            path = Path(item["file"]).resolve()
            last[path.relative_to(root).as_posix().lower()] = item
        except (ValueError, KeyError, OSError):
            continue
    return last


def _uncommitted(root, files):
    try:
        out = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", *files],
                             capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout  # no console flash
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line[3:].strip().strip('"').replace("\\", "/").lower() for line in out.splitlines() if len(line) > 3}


def _session_label(edit):
    """'Claude session 9eeaa4b6 in MyGame': the hooks see a session id, not the chat's title."""
    source = {"claude": "Claude", "codex": "Codex"}.get(edit.get("source"), "AI")
    return f"{source} session {(edit.get('session') or '?')[:8]} in {Path(edit.get('worktree', '?')).name}"


def broken_files(root, status=None):
    """[{file, errors, first_error, uncommitted, changed, last_edit}] for the last compile, worst first; [] if it compiled."""
    status = read_status() if status is None else status
    if not status or status.get("ok", True):
        return []
    by_file = {}
    for error in status.get("errors", []):
        by_file.setdefault(error.get("file") or "(no file)", []).append(error)
    files = [f for f in by_file if f != "(no file)"]
    dirty = _uncommitted(root, files) if files else set()
    edits = _last_edits(root)
    rows = []
    for file, items in by_file.items():
        key = file.lower()
        path = Path(root) / file
        try:
            changed = path.stat().st_mtime
        except OSError:
            changed = None
        first = items[0]
        rows.append({"file": file, "errors": len(items), "first_error": f"line {first.get('line')}: {first.get('message')}",
                     "assembly": first.get("assembly"), "uncommitted": key in dirty, "changed": changed,
                     "last_edit": edits.get(key)})
    rows.sort(key=lambda row: -(row["changed"] or 0))
    return rows


def _ago(stamp, now):
    seconds = max(0, int(now - stamp))
    return "just now" if seconds < 60 else f"{seconds // 60} min ago" if seconds < 3600 else f"{seconds // 3600} h ago"


def describe_file(row, now=None):
    now = time.time() if now is None else now
    facts = ["uncommitted" if row["uncommitted"] else "committed" if row["changed"] else "file not found"]
    if row["changed"]:
        facts.append(f"changed {_ago(row['changed'], now)}")
    edit = row.get("last_edit")
    if edit:
        facts.append(f"last edited by {_session_label(edit)} {_ago(edit['time'], now)}")
    return f"{row['file']} ({row['errors']} error{'s' if row['errors'] != 1 else ''}; {', '.join(facts)}): {row['first_error']}"


def summary(root):
    """One dashboard line, or None when the last compile succeeded."""
    rows = broken_files(root)
    if not rows:
        return None
    total = sum(row["errors"] for row in rows)
    head = rows[0]
    more = f" and {len(rows) - 1} more file{'s' if len(rows) > 2 else ''}" if len(rows) > 1 else ""
    state = "uncommitted" if head["uncommitted"] else "committed"
    return f"Main doesn't compile: {total} error{'s' if total != 1 else ''} in {head['file']} ({state}){more}"


def warning(root, max_files=4):
    """Text appended to Main Unity tool results while the build is broken, or None."""
    rows = broken_files(root)
    if not rows:
        return None
    total = sum(r["errors"] for r in rows)
    lines = [f"NOTE from the broker: Main Unity's last compile failed ({total} error{'s' if total != 1 else ''}), so it "
             "runs the last good build and new code isn't loaded. If you didn't edit these files, the errors are probably "
             "another chat's work in progress: don't fix or revert them without asking the user."]
    lines += ["- " + describe_file(row) for row in rows[:max_files]]
    if len(rows) > max_files:
        lines.append(f"- ... {len(rows) - max_files} more files (editor_compile_status lists them all)")
    return "\n".join(lines)
