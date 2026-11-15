"""Commit only the given paths, safely, while other chats share the same checkout.

Why: several chats commit in the main checkout. Two `git commit` runs at once race on the shared index: one fails with
"cannot lock ref 'HEAD'", or the index is left built from the old commit, showing the other chat's new files as deleted.
Committing all staged files also sweeps in whatever another chat staged.

How: one commit at a time per checkout (a lock file), and each commit is built in a private index from HEAD plus only
the given paths (`read-tree HEAD`, `add -A -- <paths>`, `write-tree`, `commit-tree`), so the shared index and anything
another chat staged are never read or changed for the commit itself. HEAD moves by compare-and-swap (`update-ref HEAD
<new> <old>`): if another commit lands in between, the build starts again on the new HEAD (up to 3 times). Afterwards
only the committed paths are refreshed in the shared index (`reset -q -- <paths>`), and the post-commit hook runs (Git
LFS's). Repositories with pre-commit, prepare-commit-msg or commit-msg hooks are refused instead of skipping them.

CLI: python tools/WorktreeBroker/commit.py -m "message" [-C <checkout>] [--dry-run] [--untrack <path>] -- <path> ...
     (-F <file> reads the message from a file; "-" reads it from stdin). --untrack (repeatable) stops tracking a path
     but keeps it on disk, like `git rm --cached`, in the same private-index commit, so nothing is staged in the shared
     index first.
"""
import argparse
import msvcrt
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LOCK_WAIT_SECONDS = 60
RETRIES = 3
BLOCKING_HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg")


def _git(root, *args, env=None, check=True, input=None, timeout=300):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout, env=env, input=input,
                            stdin=None if input is not None else subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {(result.stderr or result.stdout).strip()[:500]}")
    return result


def _out(root, *args, **kwargs):
    return _git(root, *args, **kwargs).stdout.strip()


class CheckoutLock:
    """One committer per checkout (worktrees have their own HEAD and index, so their own lock)."""

    def __init__(self, root):
        self.path = Path(_out(root, "rev-parse", "--path-format=absolute", "--git-path", "worktree-broker-commit.lock"))
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("a+b")
        deadline = time.time() + LOCK_WAIT_SECONDS
        while True:
            self.handle.seek(0)
            try:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.time() > deadline:
                    self.handle.close()
                    raise RuntimeError(f"Another commit in this checkout held the lock for {LOCK_WAIT_SECONDS} s; retry.")
                time.sleep(0.2)

    def __exit__(self, *_):
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.handle.close()


def _hooks_dir(root):
    return Path(_out(root, "rev-parse", "--path-format=absolute", "--git-path", "hooks"))


def _check_hooks(root):
    found = [name for name in BLOCKING_HOOKS if (_hooks_dir(root) / name).is_file()]
    if found:
        raise RuntimeError(f"This repository has {', '.join(found)} hook(s), which a private-index commit would skip. "
                           "Use git commit -- <paths> instead.")


def _changed(root, paths):
    """Paths among `paths` that differ from HEAD in the working tree (incl. new and deleted files)."""
    out = _git(root, "status", "--porcelain", "-uall", "--", *paths).stdout
    return [line[3:].strip().strip('"') for line in out.splitlines() if len(line) > 3]


def _build(root, head, paths, message, untrack=()):
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        _git(root, "read-tree", head, env=env)
        # A path gone from both the disk and HEAD has nothing to commit (git add would fail on the pathspec).
        paths = [p for p in paths if (Path(root) / p).exists() or _out(root, "ls-files", "--", p, env=env)]
        if not paths and not untrack:
            return None, ""
        if paths:
            _git(root, "add", "-A", "--", *paths, env=env)
        if untrack:
            _git(root, "rm", "-q", "--cached", "--ignore-unmatch", "--", *untrack, env=env)
        tree = _out(root, "write-tree", env=env)
        files = _out(root, "diff-index", "--cached", "--name-status", "--no-renames", head, env=env)
    if tree == _out(root, "rev-parse", f"{head}^{{tree}}"):
        return None, ""
    commit = _out(root, "commit-tree", tree, "-p", head, "-F", "-", input=message)
    return commit, files


def commit(root, paths, message, dry_run=False, untrack=()):
    root = Path(_out(root, "rev-parse", "--show-toplevel"))
    untrack = [str(p) for p in untrack]
    paths = [str(p) for p in paths if str(p) not in untrack]
    if not paths and not untrack:
        raise ValueError("Name the paths to commit; this tool never commits everything.")
    for path in [*paths, *untrack]:
        # "." or the repository root would sweep in every other chat's unfinished work; ":(...)" is git pathspec magic.
        full = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if not path.strip() or path.lstrip().startswith(":") or full == root.resolve() or not full.is_relative_to(root.resolve()):
            raise ValueError(f"Refusing to commit {path!r}: name files or folders inside the repository, not the "
                             "repository itself, a path outside it, or a git pathspec.")
    if not message.strip():
        raise ValueError("A commit message is required.")
    branch = _out(root, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    if not branch:
        raise RuntimeError("HEAD is detached; switch to a branch first.")
    if _git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        raise RuntimeError("A merge is in progress in this checkout; finish it with git first.")
    _check_hooks(root)
    with CheckoutLock(root):
        for attempt in range(RETRIES):
            head = _out(root, "rev-parse", "HEAD")
            new, files = _build(root, head, paths, message, untrack)
            if new is None:
                return {"committed": False, "reason": "None of these paths differ from HEAD.", "paths": paths}
            if dry_run:
                return {"committed": False, "dry_run": True, "branch": branch, "files": files.splitlines()}
            swap = _git(root, "update-ref", "-m", f"commit (broker): {message.splitlines()[0][:80]}", "HEAD", new, head,
                        check=False)
            if swap.returncode == 0:
                break
            time.sleep(0.3)  # another commit landed in between: rebuild on the new HEAD
        else:
            raise RuntimeError("HEAD kept moving while committing (another chat committing); nothing was committed. Retry.")
        # Refresh only the committed paths in the shared index; another chat's staged files stay staged.
        refreshed = True
        for _ in range(20):
            if _git(root, "reset", "-q", "HEAD", "--", *paths, *untrack, check=False).returncode == 0:
                break
            time.sleep(0.25)  # index.lock held by another git command
        else:
            refreshed = False
        if (_hooks_dir(root) / "post-commit").is_file():
            _git(root, "hook", "run", "post-commit", check=False)
    result = {"committed": True, "commit": new[:10], "branch": branch, "files": files.splitlines(),
              "subject": message.splitlines()[0]}
    if not refreshed:
        result["warning"] = ("The shared index was busy, so it may still show these files as changed: run "
                             f"git reset -q HEAD -- <paths> once it is free.")
    left = _changed(root, paths) if paths else []
    if left:
        result["still_changed"] = left  # edited again while committing
    return result


def main():
    parser = argparse.ArgumentParser(description="Commit only the given paths without touching anyone else's staging.")
    parser.add_argument("-C", dest="root", default=".")
    parser.add_argument("-m", dest="message")
    parser.add_argument("-F", dest="message_file")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--untrack", action="append", default=[], help="stop tracking this path, keep the file")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    if args.message_file:
        message = sys.stdin.read() if args.message_file == "-" else Path(args.message_file).read_text(encoding="utf-8")
    else:
        message = args.message or ""
    import json
    # Paths on the command line are relative to where it was run, like git's own.
    paths = [os.path.abspath(p) for p in args.paths]
    untrack = [os.path.abspath(p) for p in args.untrack]
    print(json.dumps(commit(args.root, paths, message, args.dry_run, untrack), indent=2))


if __name__ == "__main__":
    main()
