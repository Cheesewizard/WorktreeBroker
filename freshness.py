"""How far a worktree has drifted from the development line, and how to catch it up.

Why: a worktree starts from the last commit, while main may carry uncommitted work and newer commits, so a worktree can
be testing or reloading a file whose newer version sits in main. Two kinds of drift matter:

- behind: commits on the development branch the worktree doesn't have;
- overlap: files this worktree changes that main has also changed since the worktree's base (committed, or still
  uncommitted in main, with the AI session that last edited them when the hook recorded it). Overlap is the dangerous
  one: reloading or merging this worktree's version undoes or contradicts the other work.

`check()` reports both (cached briefly; broker results carry `note()` when there is drift). `sync()` merges the
development branch into the worktree: a merge, never a rebase or reset, preceded by a conflict check with
`git merge-tree` that touches nothing, so a conflicting sync changes nothing and lists the files instead.
`include_uncommitted` also merges a snapshot of main's uncommitted files (built in a temporary index; main's own index
and files are untouched), recorded as its own commit so it is clear that work isn't this worktree's.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE_SECONDS = 60
# Per-worktree wiring (MCP and agent config), never worth a drift warning.
IGNORED = (".codex/", ".claude/", ".mcp.json")
BEHIND_WARN = 1          # any commit behind is reported; overlap is what makes it a warning
_cache = {}


def _git(path, *args, env=None, check=True, timeout=60):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, env=env,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {result.stderr.strip()[:300]}")
    return result


def _out(path, *args, **kwargs):
    return _git(path, *args, **kwargs).stdout.strip()


def main_route():
    import broker_state

    route = broker_state.main_route()
    return Path(route["worktree"]).resolve(), route["development_branch"]


def _lines(text):
    return [line for line in text.splitlines() if line.strip()]


def _status_paths(root):
    """{posix path lower: original path} for every uncommitted (incl. untracked) file in root."""
    paths = {}
    # Not _out: stripping would eat the first line's leading status column (" M path").
    for line in _lines(_git(root, "status", "--porcelain", "-uall", timeout=60).stdout):
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"').replace("\\", "/")
        paths[path.lower()] = path
    return paths


def _session_edits(main):
    try:
        import compile_status
        return compile_status._last_edits(main), compile_status._session_label
    except Exception:  # noqa: BLE001 - attribution is optional
        return {}, None


def check(root, use_cache=True):
    root = Path(root).resolve()
    main, branch = main_route()
    key = str(root).lower()
    hit = _cache.get(key)
    if use_cache and hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    if root == main:
        report = {"worktree": str(root), "is_main": True, "level": "main",
                  "summary": "This is the main checkout (the development line itself)."}
        _cache[key] = (time.time(), report)
        return report
    # git costs ~0.2 s a start on Windows, so independent calls run side by side (~1.5 s in all).
    with ThreadPoolExecutor(max_workers=6) as pool:
        head = pool.submit(_out, root, "rev-parse", "HEAD")
        base = pool.submit(_out, root, "merge-base", "HEAD", branch)
        counts = pool.submit(_out, root, "rev-list", "--left-right", "--count", f"HEAD...{branch}")
        mine_dirty = pool.submit(_status_paths, root)
        main_dirty = pool.submit(_status_paths, main)
        base = base.result()
        base_time = pool.submit(_out, root, "show", "-s", "--format=%ct", base)
        mine_committed = pool.submit(_out, root, "diff", "--name-only", "--no-renames", base)
        theirs = pool.submit(_out, root, "diff-tree", "-r", "--name-only", "--no-renames", base, branch)
        head = head.result()
        ahead, behind = (int(n) for n in counts.result().split())
        base_time = int(base_time.result() or 0)
        mine = {p.lower(): p for p in _lines(mine_committed.result())}
        mine.update(mine_dirty.result())
        theirs_committed = {p.lower() for p in _lines(theirs.result())}
        main_dirty = main_dirty.result()
    for low in [low for low in mine if low.startswith(IGNORED)]:
        del mine[low]
    edits, label = _session_edits(main)
    overlap = []
    for low, path in sorted(mine.items()):
        committed = low in theirs_committed
        dirty = low in main_dirty
        if not (committed or dirty):
            continue
        row = {"file": path, "main_committed_since_base": committed, "main_uncommitted": dirty}
        edit = edits.get(low)
        if dirty and edit and label:
            row["main_editor"] = label(edit)
        overlap.append(row)
    level = "stale" if overlap else ("behind" if behind >= BEHIND_WARN or main_dirty else "fresh")
    report = {
        "worktree": str(root), "is_main": False, "development_branch": branch, "level": level,
        "head": head[:10], "base": base[:10], "base_age_hours": round((time.time() - base_time) / 3600, 1),
        "behind": behind, "ahead": ahead, "files_changed_here": len(mine),
        "main_uncommitted_files": len(main_dirty), "overlap": overlap,
    }
    report["summary"] = describe(report)
    _cache[key] = (time.time(), report)
    return report


def describe(report, max_files=5):
    if report.get("is_main"):
        return report["summary"]
    parts = []
    if report["behind"]:
        parts.append(f"{report['behind']} commit(s) behind {report['development_branch']} "
                     f"(base {report['base']}, {report['base_age_hours']} h old)")
    if report["main_uncommitted_files"]:
        parts.append(f"main has {report['main_uncommitted_files']} uncommitted file(s) this worktree can't see")
    text = "; ".join(parts) or "Up to date with the development line."
    overlap = report["overlap"]
    if overlap:
        rows = []
        for row in overlap[:max_files]:
            where = "committed in main" if row["main_committed_since_base"] else ""
            if row["main_uncommitted"]:
                where = (where + " + " if where else "") + "uncommitted in main"
                if row.get("main_editor"):
                    where += f" ({row['main_editor']})"
            rows.append(f"{row['file']} [{where}]")
        more = f" and {len(overlap) - max_files} more" if len(overlap) > max_files else ""
        text += (f". DRIFT: {len(overlap)} file(s) you changed were also changed in main since your base: "
                 + "; ".join(rows) + more + ". Your copy may be older than main's or contradict it: call "
                 "worktree_update to merge the committed part, and check the uncommitted part with the user or the chat "
                 "named before building on it.")
    elif report["behind"] or report["main_uncommitted_files"]:
        text += ". No overlap with your files; worktree_update brings the commits in."
    return text


def note(root):
    """A one-line warning for broker results, or None when there's nothing worth saying."""
    try:
        report = check(root)
    except Exception:  # noqa: BLE001 - a note must never break the call it rides on
        return None
    if report.get("is_main"):
        return None
    if report["overlap"]:
        return "Worktree drift: " + report["summary"]
    if report["behind"] >= 10 or report["base_age_hours"] >= 24:
        return "Worktree drift: " + report["summary"]
    return None


def _snapshot_main(main, branch):
    """A commit of main's working tree (tracked changes and untracked, non-ignored files) on top of the branch tip.
    Uses a temporary index so main's own index and files are untouched. Returns the commit id or None if clean."""
    if not _status_paths(main):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        # A copy of main's own index keeps its stat cache, so `add -A` hashes only changed files (an empty index
        # would re-hash, and run the LFS filter on, every file in the repository).
        index = Path(tmp) / "index"
        shutil.copyfile(Path(_out(main, "rev-parse", "--path-format=absolute", "--git-path", "index")), index)
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        _git(main, "add", "-A", env=env, timeout=300)
        tree = _out(main, "write-tree", env=env)
    tip = _out(main, "rev-parse", branch)
    if tree == _out(main, "rev-parse", f"{tip}^{{tree}}"):
        return None
    message = (f"Snapshot of main's uncommitted work ({time.strftime('%Y-%m-%d %H:%M')}), merged by worktree_update.\n\n"
               "Not this worktree's work: other sessions' unfinished edits in the main checkout. Don't hand it back "
               "as part of this branch.")
    return _out(main, "commit-tree", tree, "-p", tip, "-m", message)


def sync(root, include_uncommitted=False, dry_run=False):
    root = Path(root).resolve()
    main, branch = main_route()
    if root == main:
        raise RuntimeError("The main checkout is the development line; there's nothing to sync it from.")
    if _out(root, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD":
        raise RuntimeError("This worktree is on a detached HEAD. Create a branch first (git switch -c <name>) so the "
                           "merge has somewhere to go.")
    if _git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        raise RuntimeError("A merge is already in progress in this worktree. Finish or abort it first.")
    before = check(root, use_cache=False)
    snapshot = _snapshot_main(main, branch) if include_uncommitted else None
    target = snapshot or _out(root, "rev-parse", branch)
    if _git(root, "merge-base", "--is-ancestor", target, "HEAD", check=False).returncode == 0:
        return {"synced": False, "reason": "Already contains the development line.", "before": before["summary"]}
    # Conflict check on trees only: touches no file, index or ref.
    probe = _git(root, "merge-tree", "--write-tree", "--name-only", "--no-messages", "HEAD", target, check=False)
    if probe.returncode == 1:
        files = _lines(probe.stdout)[1:]
        return {"synced": False, "conflicts": files,
                "reason": "Merging would conflict in these files, so nothing was changed. Resolve them deliberately "
                          "(they are where two sessions disagree): ask the user which version wins, or merge by hand."}
    if probe.returncode != 0:
        raise RuntimeError(f"Conflict check failed: {probe.stderr.strip()[:300]}")
    # The worktree's own uncommitted edits stay where they are: refuse if the merge would change one of them
    # (no stash, so nothing ends up hidden), otherwise git merge leaves them alone.
    incoming = {p.lower() for p in _lines(_out(root, "diff-tree", "-r", "--name-only", "--no-renames", "HEAD", target))}
    blocked = sorted(path for low, path in _status_paths(root).items() if low in incoming)
    if blocked:
        return {"synced": False, "uncommitted_here": blocked,
                "reason": "The merge changes files you have uncommitted edits in. Commit them in this worktree first "
                          "(a worktree commit is fine and easy to undo), then sync again. Nothing was changed."}
    if dry_run:
        return {"synced": False, "dry_run": True, "would_merge": target[:10], "before": before["summary"]}
    merge = _git(root, "merge", "--no-edit", target, check=False, timeout=300)
    if merge.returncode != 0:
        _git(root, "merge", "--abort", check=False)
        return {"synced": False, "reason": "git merge refused; nothing changed.",
                "git": (merge.stderr.strip() or merge.stdout.strip())[-600:]}
    after = check(root, use_cache=False)
    return {"synced": True, "merged": target[:10], "included_uncommitted": bool(snapshot),
            "before": before["summary"], "after": after["summary"],
            "note": "Recompile or reload before trusting earlier captures; they were made against the old base."}


if __name__ == "__main__":
    import sys
    print(json.dumps(check(sys.argv[1] if len(sys.argv) > 1 else "."), indent=2))
