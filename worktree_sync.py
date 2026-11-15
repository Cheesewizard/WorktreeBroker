"""Publish saved Unity edits and preview a three-way combination without checkout writes."""

import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import time
from uuid import uuid4

from broker_state import HERE, read_json, write_json


SYNC = HERE / ".sync"
ROOTS = {"Assets", "Packages", "ProjectSettings"}
ID = re.compile(r"[0-9a-f]{32}\Z")
OBJECT_ID = re.compile(r"[0-9a-f]{40,64}\Z")


def invoke(root, *args, env=None, accepted=(0,), data=None):
    result = subprocess.run(
        ["git", "-C", str(root), *args], env=env, input=data,
        stdin=subprocess.DEVNULL if data is None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode not in accepted:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"Git {args[0]} failed ({result.returncode})")
    return result


def output(root, *args, env=None):
    return invoke(root, *args, env=env).stdout.decode("utf-8").strip()


def repository(root):
    return str(Path(output(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve())


def unity_path(value):
    if not isinstance(value, str) or "\\" in value or ":" in value or "\0" in value:
        raise ValueError("Use project-relative paths with forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or path.parts[0] not in ROOTS or (
        ".." in path.parts or path.as_posix() != value
    ):
        raise ValueError(f"Path is outside the allowed Unity project folders: {value}")
    return path


def record_path(folder, identity):
    if not isinstance(identity, str) or not ID.fullmatch(identity):
        raise ValueError("Invalid sync ID")
    return SYNC / folder / identity / "manifest.json"


def capture(root, paths):
    paths = sorted(set(unity_path(value).as_posix() for value in paths))
    if not paths:
        raise ValueError("Choose at least one Unity path to capture")
    for item in paths:
        if not (root / item).resolve().is_relative_to(root):
            raise ValueError(f"Path points outside the worktree: {item}")
    head = output(root, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(prefix="worktree-broker-sync-") as folder:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(folder) / "index"),
                   GIT_LITERAL_PATHSPECS="1")
        invoke(root, "read-tree", head, env=env)
        invoke(root, "add", "-A", "--", *paths, env=env)
        tree = output(root, "write-tree", env=env)
    if output(root, "rev-parse", "HEAD") != head:
        raise RuntimeError("The worktree changed branches while it was being captured")
    return head, tree, paths


def changed_paths(root, before, after):
    raw = invoke(root, "diff", "--no-renames", "--name-only", "-z", before, after).stdout
    return [item.decode("utf-8") for item in raw.split(b"\0") if item]


def publish(root, paths, label=""):
    head, tree, paths = capture(root, paths)
    files = changed_paths(root, head, tree)
    if not files:
        raise RuntimeError("No saved edits found in the selected paths")
    identity = uuid4().hex
    pin = f"refs/worktree-broker/snapshots/{identity}"
    invoke(root, "update-ref", pin, tree)
    manifest = {
        "id": identity, "label": label, "worktree": str(root), "repository": repository(root),
        "base": head, "tree": tree, "paths": paths, "files": files, "created_at": time.time(),
    }
    write_json(record_path("published", identity), manifest)
    return manifest


def list_published(root):
    """Snapshots for an AI chat to pick from: id, label, worktree, age and what they hold. The full manifests (every
    file, git trees) stay on disk for sync_preview; listing them all would cost ~10,000 tokens a call."""
    repo = repository(root)
    values = [read_json(item) for item in (SYNC / "published").glob("*/manifest.json")]
    now = time.time()
    items = sorted((item for item in values if item["repository"] == repo), key=lambda item: item["created_at"], reverse=True)
    return {"snapshots": [{
        "id": item["id"], "label": item.get("label") or "", "worktree": Path(item["worktree"]).name,
        "age_minutes": int((now - item["created_at"]) // 60), "paths": item.get("paths", [])[:5],
        "files": len(item.get("files", [])),
    } for item in items[:20]], **({"older_not_shown": len(items) - 20} if len(items) > 20 else {})}


def export_file(root, tree, name, folder):
    path = unity_path(name)
    listing = invoke(root, "--literal-pathspecs", "ls-tree", "-z", tree, "--", name).stdout
    if not listing:
        return {"path": name, "action": "delete"}
    metadata, _ = listing.rstrip(b"\0").split(b"\t", 1)
    mode, kind, identity = metadata.decode("ascii").split()
    if kind != "blob" or mode not in {"100644", "100755"}:
        raise RuntimeError(f"Preview cannot export this Git file type: {name}")
    blob = invoke(root, "cat-file", "blob", identity).stdout
    if blob.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        object_id = re.search(rb"(?m)^oid sha256:([0-9a-f]{64})$", blob)
        if not object_id:
            raise RuntimeError(f"Invalid Git LFS pointer: {name}")
        key = object_id[1].decode()
        cached = Path(repository(root)) / "lfs" / "objects" / key[:2] / key[2:4] / key
        if not cached.is_file():
            raise RuntimeError(f"Git LFS object is missing locally: {name}")
        blob = cached.read_bytes()
    destination = folder.joinpath(*path.parts)
    if not destination.resolve().is_relative_to(folder.resolve()):
        raise RuntimeError(f"Preview destination is outside its folder: {name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(blob)
    return {"path": name, "action": "write"}


def preview(root, snapshot_id, paths):
    source = read_json(record_path("published", snapshot_id))
    if source["repository"] != repository(root):
        raise RuntimeError("The snapshot belongs to a different Git repository")
    if Path(source["worktree"]).resolve() == root:
        raise ValueError("Choose a snapshot published by another worktree")
    head, local_tree, paths = capture(root, paths)
    result = invoke(
        root, "merge-tree", "--write-tree", "--name-only", "--no-messages", "-z",
        f"--merge-base={source['base']}", local_tree, source["tree"], accepted=(0, 1),
    )
    entries = [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
    if not entries or not OBJECT_ID.fullmatch(entries[0]):
        raise RuntimeError("Git returned an invalid preview tree")
    identity = uuid4().hex
    record = record_path("previews", identity)
    manifest = {
        "id": identity, "snapshot_id": snapshot_id, "worktree": str(root),
        "source_worktree": source["worktree"], "base": head, "local_paths": paths,
        "created_at": time.time(), "status": "conflict" if result.returncode else "ready",
        "conflicts": entries[1:] if result.returncode else [],
    }
    if not result.returncode:
        merged_tree = entries[0]
        patch = invoke(root, "diff", "--binary", local_tree, merged_tree).stdout
        record.parent.mkdir(parents=True, exist_ok=True)
        (record.parent / "incoming.diff").write_bytes(patch)
        combined = record.parent / "files"
        files = sorted(set(changed_paths(root, head, local_tree) + changed_paths(root, local_tree, merged_tree)))
        manifest["files"] = [export_file(root, merged_tree, name, combined) for name in files]
        manifest["directory"] = str(record.parent)
        manifest["incoming_diff"] = str(record.parent / "incoming.diff")
        manifest["preview_kind"] = "Changed-file overlay; no Unity checkout was launched."
    write_json(record, manifest)
    return manifest


def runtime_route(path):
    for item in (SYNC / "runtimes").glob("*/manifest.json"):
        value = read_json(item)
        if Path(value["worktree"]).resolve() == path and (path / "Assets").is_dir():
            return value
    return None
