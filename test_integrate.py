"""integrate on a throwaway repo (never the real one). Run: python -m pytest tools/WorktreeBroker/test_integrate.py"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import integrate

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
A = "Assets/Game/A.cs"
NEW = "Assets/Game/Presentation/New.uss"


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True, env=ENV,
                          creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()


def write(root, name, text):
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def state(path):
    return git(path, "rev-parse", "HEAD"), git(path, "status", "--porcelain", "-uall"), git(path, "rev-parse", "--abbrev-ref", "HEAD")


@pytest.fixture
def repo(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-b", "dev")
    git(main, "config", "core.autocrlf", "false")
    write(main, A, "a1\na2\na3\n")
    write(main, "Assets/Game/Presentation/Menu.uss", "menu { }\n")
    write(main, "Assets/Game/Big.png", "not extracted")
    write(main, "ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 1\n")
    git(main, "add", "-A")
    git(main, "commit", "-m", "base")
    trees = {}
    for name in ("one", "two", "three"):
        trees[name] = tmp_path / name
        git(main, "worktree", "add", "-b", name, str(trees[name]))
    # one: saved but uncommitted (a new file and an edit at the top of A.cs)
    write(trees["one"], NEW, "new { }\n")
    write(trees["one"], A, "A1\na2\na3\n")
    # two: a committed change at the bottom of A.cs
    write(trees["two"], A, "a1\na2\nA3\n")
    git(trees["two"], "commit", "-am", "two")
    # three: a committed change to the same line as one
    write(trees["three"], A, "other\na2\na3\n")
    git(trees["three"], "commit", "-am", "three")
    return main, trees, tmp_path / "runs"


def test_integrate_included_skipped_manifest_and_nothing_touched(repo):
    main, trees, runs = repo
    everything = {**trees, "main": main}
    before = {name: state(path) for name, path in everything.items()}
    heads_before = git(main, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")

    manifest = integrate.integrate([str(trees["one"]), str(trees["two"]), str(trees["three"])], base="dev", main=main, runs=runs)

    assert [Path(i["worktree"]).name for i in manifest["included"]] == ["one", "two"]
    assert [Path(i["worktree"]).name for i in manifest["skipped"]] == ["three"]
    assert manifest["skipped"][0]["conflicts"] == [A]
    assert sorted(manifest["changed_vs_base"]) == [A, NEW]
    assert sorted(manifest["included"][0]["changed_paths"]) == [A, NEW]
    folder = Path(manifest["folder"])
    assert (folder / A).read_bytes() == b"A1\na2\nA3\n"
    assert (folder / NEW).is_file()
    assert (folder / "ProjectSettings/ProjectVersion.txt").is_file()
    assert not (folder / "Assets/Game/Big.png").exists()
    assert (runs / manifest["id"] / "manifest.json").is_file()
    assert git(main, "rev-parse", integrate.REF_PREFIX + manifest["id"]) == manifest["final_commit"]
    assert integrate.integration_tree(manifest["id"], runs) == folder.resolve()
    assert integrate.list_integrations(runs=runs)["integrations"][0]["id"] == manifest["id"]

    assert {name: state(path) for name, path in everything.items()} == before
    assert git(main, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads") == heads_before


def test_without_uncommitted_and_all_keyword(repo):
    main, trees, runs = repo
    manifest = integrate.integrate("all", base="dev", include_uncommitted=False, main=main, runs=runs)
    names = {Path(i["worktree"]).name for i in manifest["included"]} | {Path(i["worktree"]).name for i in manifest["skipped"]}
    assert names == {"one", "two", "three"}  # the main checkout is left out
    assert NEW not in manifest["changed_vs_base"]  # one's change was never committed


def test_rejects_foreign_paths_and_bad_ids(repo, tmp_path):
    main, trees, runs = repo
    with pytest.raises(ValueError):
        integrate.integrate([str(tmp_path)], base="dev", main=main, runs=runs)
    with pytest.raises(ValueError):
        integrate.integration_tree("../../x", runs)


def test_keeps_newest_five(repo):
    main, trees, runs = repo
    ids = [integrate.integrate([str(trees["two"])], base="dev", main=main, runs=runs)["id"] for _ in range(7)]
    left = sorted(p.name for p in runs.iterdir())
    assert len(left) == 5 and set(left) <= set(ids)
    assert len(git(main, "for-each-ref", "--format=%(refname)", integrate.REF_PREFIX).splitlines()) == 5
