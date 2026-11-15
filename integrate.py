"""`integrate`: show the COMBINED result of several in-flight worktrees without touching any of them.

Each worktree's saved state (HEAD plus, optionally, its saved changes) becomes a tree in a temporary index, wrapped in a
commit object. The commits are folded onto a base with `git merge-tree --write-tree`; a worktree that conflicts is
skipped and reported. The final commit is pinned at `refs/worktree-broker/integrations/<id>` and the files compile
and the UI reload need are extracted into `.runs/integrate/<id>/tree/`.

Safety: only object-writing git commands run (temporary index, merge-tree, commit-tree, archive, and update-ref under
the integrations namespace). No worktree, index, branch or checkout is ever written.
"""

import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from uuid import uuid4

from broker_state import HERE, read_json, write_json
import worktree_sync as sync

INTEGRATE_DIR = HERE / ".runs" / "integrate"
REF_PREFIX = "refs/worktree-broker/integrations/"
ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}\Z")
KEEP = 5
# What a worktree contributes: the Unity folders, plus any project folders its verify checks need (project.json
# "integrate": {"extra_paths": [...]}, e.g. a compile-check script and plain C# test projects).
from broker_state import PROJECT  # noqa: E402

CAPTURE_PATHS = ["Assets", "Packages", "ProjectSettings", *PROJECT.get("integrate", {}).get("extra_paths", [])]
EXTRACT_SUFFIXES = {".cs", ".asmdef", ".asmref", ".uss", ".uxml", ".json", ".txt", ".csproj", ".props", ".targets", ".rsp", ".py", ".md"}
IDENTITY = {"GIT_AUTHOR_NAME": "worktree-broker", "GIT_AUTHOR_EMAIL": "broker@localhost",
            "GIT_COMMITTER_NAME": "worktree-broker", "GIT_COMMITTER_EMAIL": "broker@localhost"}


def integration_tree(identity, runs=None):
    """The extracted tree of an integration (for ui_live_reload's `integration` argument)."""
    if not isinstance(identity, str) or not ID.fullmatch(identity):
        raise ValueError("Invalid integration id")
    tree = ((runs or INTEGRATE_DIR) / identity / "tree").resolve()
    if not tree.is_dir():
        raise ValueError(f"No integration {identity} (it may have been cleaned up; run integrate again)")
    return tree


def _git(repo, *args, env=None, accepted=(0,), data=None):
    return sync.invoke(repo, *args, env=env, accepted=accepted, data=data)


def _text(repo, *args, env=None):
    return sync.output(repo, *args, env=env)


def _same(a, b):
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def default_main_and_base():
    import broker_state

    route = broker_state.main_route()
    return Path(route["worktree"]).resolve(), route["development_branch"]


def list_worktrees(repo):
    raw = _git(repo, "worktree", "list", "--porcelain").stdout.decode("utf-8", errors="replace")
    rows, current = [], {}
    for line in raw.splitlines() + [""]:
        if not line.strip():
            if "worktree" in current:
                rows.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return [row for row in rows if "bare" not in row and Path(row["worktree"]).is_dir()]


def _resolve_worktrees(repo, main, requested):
    known = [Path(row["worktree"]).resolve() for row in list_worktrees(repo)]
    if requested == "all":
        return [path for path in known if not _same(path, main)]
    if not isinstance(requested, list) or not requested:
        raise ValueError('worktrees must be a list of paths or "all"')
    result = []
    for item in requested:
        path = Path(item).resolve()
        if not any(_same(path, other) for other in known):
            raise ValueError(f"Not a worktree of this repository: {item}")
        if not any(_same(path, other) for other in result):
            result.append(path)
    return result


def snapshot_commit(repo, worktree, include_uncommitted):
    """A commit whose tree is the worktree's HEAD plus (optionally) its saved changes. Parent: the worktree's HEAD."""
    head = _text(worktree, "rev-parse", "HEAD")
    tree = _text(worktree, "rev-parse", f"{head}^{{tree}}")
    if include_uncommitted:
        paths = [path for path in CAPTURE_PATHS
                 if (worktree / path).exists() or _git(worktree, "ls-tree", "--name-only", head, "--", path).stdout.strip()]
        if paths:
            with tempfile.TemporaryDirectory(prefix="worktree-broker-integrate-") as folder:
                env = dict(os.environ, GIT_INDEX_FILE=str(Path(folder) / "index"), GIT_LITERAL_PATHSPECS="1")
                _git(worktree, "read-tree", head, env=env)
                _git(worktree, "add", "-A", "--", *paths, env=env)
                tree = _text(worktree, "write-tree", env=env)
        if _text(worktree, "rev-parse", "HEAD") != head:
            raise RuntimeError(f"{worktree} changed branches while it was being captured")
    commit = _text(repo, "commit-tree", tree, "-p", head, "-m", f"integrate: {worktree.name} saved state",
                   env=dict(os.environ, **IDENTITY))
    return head, tree, commit


def _names(repo, before, after):
    raw = _git(repo, "diff", "--no-renames", "--name-only", "-z", before, after).stdout
    return [item.decode("utf-8") for item in raw.split(b"\0") if item]


def _merge(repo, current, other):
    """(merged tree, []) or (None, conflicted paths)."""
    result = _git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", current, other, accepted=(0, 1))
    entries = [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
    if not entries or not sync.OBJECT_ID.fullmatch(entries[0]):
        raise RuntimeError("Git returned an invalid merge tree")
    return (None, entries[1:] or ["(unknown)"]) if result.returncode else (entries[0], [])


def _extract(repo, commit, destination, main):
    present = [path for path in CAPTURE_PATHS if _git(repo, "ls-tree", "--name-only", commit, "--", path).stdout.strip()]
    if not present:
        return
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    process = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", commit, "--", *present],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if not member.isfile() or name.is_absolute() or ".." in name.parts:
                continue
            if name.suffix.lower() not in EXTRACT_SUFFIXES:
                continue
            target = (root / Path(*name.parts)).resolve()
            if not target.is_relative_to(root):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = archive.extractfile(member)
            target.write_bytes(handle.read())
    if process.wait() != 0:
        raise RuntimeError("git archive failed: " + process.stderr.read().decode("utf-8", errors="replace").strip())
    # Submodules (Stat Conductor) are not in the archive: borrow the main checkout's files (read only).
    listing = _git(repo, "ls-tree", "-r", "-z", commit, "--", "Packages").stdout.decode("utf-8", errors="replace")
    for row in listing.split("\0"):
        if row.startswith("160000 commit "):
            path = row.split("\t", 1)[1]
            source, link = Path(main) / path, root / path
            if source.is_dir() and not link.exists():
                shutil.copytree(source, link, ignore=shutil.ignore_patterns(".git", "Tests", "Tests~", "Samples~", "Documentation~"))
    # The compile check borrows Library/ScriptAssemblies from the checkout it runs in; the tree has none, so link main's.
    library = Path(main) / "Library" / "ScriptAssemblies"
    link = root / "Library" / "ScriptAssemblies"
    if library.is_dir() and not link.exists():
        link.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(library)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)


def _remove_tree(folder):
    def unlock(function, path, _):
        os.chmod(path, stat.S_IWRITE)
        function(path)

    link = folder / "tree" / "Library" / "ScriptAssemblies"
    if os.path.isjunction(link):
        os.rmdir(link)  # remove the junction itself, never its target
    if os.path.isjunction(link) or os.path.islink(link):
        # Never delete a tree that still links into the main checkout's Library: rmtree must not reach main's files.
        raise OSError(f"{link} still links to the main checkout; not deleting {folder}")
    shutil.rmtree(folder, onexc=unlock)


def _cleanup(repo, runs, newest):
    folders = sorted((item for item in runs.iterdir() if item.is_dir() and ID.fullmatch(item.name) and item.name != newest),
                     key=lambda item: item.stat().st_mtime_ns, reverse=True)
    folders = [runs / newest, *folders]
    for old in folders[KEEP:]:
        if old.resolve().parent != runs.resolve():
            continue
        try:
            _remove_tree(old)
            _git(repo, "update-ref", "-d", REF_PREFIX + old.name)
        except (OSError, RuntimeError):
            pass


def integrate(worktrees="all", base=None, include_uncommitted=True, verify="none", main=None, runs=None):
    if verify not in ("none", "compile"):
        raise ValueError('verify must be "none" or "compile"')
    default_main, default_base = default_main_and_base() if (main is None or base is None) else (None, None)
    main = Path(main or default_main).resolve()
    runs = Path(runs or INTEGRATE_DIR)
    base = base or default_base
    base_commit = _text(main, "rev-parse", "--verify", f"{base}^{{commit}}")
    paths = _resolve_worktrees(main, main, worktrees)

    identity = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:4]}"
    current, included, skipped = base_commit, [], []
    for worktree in paths:
        try:
            head, tree, commit = snapshot_commit(main, worktree, include_uncommitted)
            changed = _names(main, head, tree)
            if not changed and head == base_commit:
                included.append({"worktree": str(worktree), "head": head, "commit": commit, "changed_paths": []})
                continue
            merged, conflicts = _merge(main, current, commit)
        except (RuntimeError, subprocess.SubprocessError) as error:
            skipped.append({"worktree": str(worktree), "conflicts": [], "error": str(error)})
            continue
        if conflicts:
            skipped.append({"worktree": str(worktree), "conflicts": conflicts})
            continue
        current = _text(main, "commit-tree", merged, "-p", current, "-p", commit,
                        "-m", f"integrate: add {worktree.name}", env=dict(os.environ, **IDENTITY))
        included.append({"worktree": str(worktree), "head": head, "commit": commit, "changed_paths": changed})

    _git(main, "update-ref", REF_PREFIX + identity, current)
    folder = runs / identity
    tree_folder = folder / "tree"
    _extract(main, current, tree_folder, main)
    manifest = {
        "id": identity, "base": base, "base_commit": base_commit, "created": time.time(),
        "include_uncommitted": include_uncommitted, "included": included, "skipped": skipped,
        "final_commit": current, "changed_vs_base": _names(main, base_commit, current), "folder": str(tree_folder),
    }
    write_json(folder / "manifest.json", manifest)
    _cleanup(main, runs, identity)
    return manifest


def summarize(manifest):
    summary = {
        "id": manifest["id"], "folder": manifest["folder"], "base": manifest["base"],
        "included": [item["worktree"] for item in manifest["included"]],
        "skipped": [{"worktree": item["worktree"], "conflicts": item["conflicts"][:20],
                     **({"error": item["error"]} if "error" in item else {})} for item in manifest["skipped"]],
        "changed_files": len(manifest["changed_vs_base"]),
        "next": f"ui_live_reload with integration={manifest['id']} (styles/source resolved inside the folder), or verify compile.",
    }
    return summary


def run(worktrees="all", base=None, include_uncommitted=True, verify="none", main=None, runs=None):
    """Blocking: integrate, and optionally compile the result. Returns the short summary."""
    manifest = integrate(worktrees, base, include_uncommitted, verify, main, runs)
    summary = summarize(manifest)
    if verify == "compile":
        import verify as verify_tool

        summary["compile"] = verify_tool.verify(manifest["folder"], ["compile"])
    return summary


def list_integrations(root=None, runs=None, limit=KEEP):
    runs = Path(runs or INTEGRATE_DIR)
    rows = []
    for path in runs.glob("*/manifest.json") if runs.is_dir() else []:
        try:
            manifest = read_json(path)
        except (OSError, ValueError):
            continue
        rows.append({"id": manifest["id"], "created": manifest["created"], "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(manifest["created"])),
                     "base": manifest["base"], "included": len(manifest["included"]), "skipped": len(manifest["skipped"]),
                     "changed_files": len(manifest["changed_vs_base"]), "folder": manifest["folder"]})
    return {"integrations": sorted(rows, key=lambda row: row["created"], reverse=True)[:limit]}


# Background runs, so no client times out (Codex gives an MCP tool 60 s); the same shape as verify's.
_RUNS = {}


def start(**arguments):
    run_id = uuid4().hex[:10]
    entry = {"started": time.time(), "done": threading.Event(), "result": None}

    def work():
        try:
            entry["result"] = run(**arguments)
        except Exception as error:  # noqa: BLE001 - reported to the caller
            entry["result"] = {"ok": False, "error": str(error)}
        finally:
            entry["done"].set()

    # Forget results an hour after they finish, so a broker that runs for days doesn't keep them all.
    for old_id, old in list(_RUNS.items()):
        if old["done"].is_set() and time.time() - old["started"] > 3600:
            _RUNS.pop(old_id, None)
    _RUNS[run_id] = entry
    threading.Thread(target=work, daemon=True).start()
    return run_id


def wait(run_id, seconds=50):
    entry = _RUNS.get(run_id)
    if entry is None:
        raise RuntimeError(f"No integrate run {run_id} in this chat (runs end with the chat's broker).")
    if entry["done"].wait(max(0.0, min(float(seconds), 55.0))):
        return {"run_id": run_id, "done": True, **entry["result"]}
    return {"run_id": run_id, "done": False, "running_seconds": round(time.time() - entry["started"]),
            "next": f"Call integrate_status with run_id {run_id} to keep waiting."}
