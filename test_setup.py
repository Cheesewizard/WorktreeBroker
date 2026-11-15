"""install.py, the default routes and commit.py --untrack, on throwaway folders and repos (never the real ones).
Run: python -m pytest tools/WorktreeBroker"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import broker_state
import commit
import install

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True,
                          env=dict(os.environ, **IDENTITY), creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)
    main = tmp_path / "Game"
    (main / "Assets").mkdir(parents=True)
    git(main, "init", "-b", "dev")
    git(main, "config", "core.autocrlf", "false")
    (main / "Assets" / "A.cs").write_text("class A {}\n", encoding="utf-8")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "init")
    return main


def test_default_routes_follow_the_checkout(repo, monkeypatch):
    broker = repo / "tools" / "Broker"
    broker.mkdir(parents=True)
    monkeypatch.setattr(broker_state, "HERE", broker)
    monkeypatch.setattr(broker_state, "_DEFAULT_ROUTES", None)
    main = broker_state.main_route()
    assert Path(main["worktree"]) == repo.resolve()
    assert main["development_branch"] == "dev" and main["default"]


def test_default_routes_from_a_submodule_name_the_superproject(repo, tmp_path, monkeypatch):
    tool = tmp_path / "BrokerRepo"
    tool.mkdir()
    git(tool, "init", "-b", "main")
    (tool / "broker.py").write_text("", encoding="utf-8")
    git(tool, "add", "-A")
    git(tool, "commit", "-q", "-m", "broker")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(tool), "tools/Broker")
    monkeypatch.setattr(broker_state, "HERE", repo / "tools" / "Broker")
    monkeypatch.setattr(broker_state, "_DEFAULT_ROUTES", None)
    main = broker_state.main_route()
    assert Path(main["worktree"]) == repo.resolve() and main["development_branch"] == "dev"


def test_default_routes_outside_git_say_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_state, "HERE", tmp_path)
    monkeypatch.setattr(broker_state, "_DEFAULT_ROUTES", None)
    with pytest.raises(RuntimeError, match="install.py config"):
        broker_state.routes()


def test_config_writes_once(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "HERE", tmp_path)
    monkeypatch.setattr(broker_state, "default_routes",
                        lambda: [{"name": "main", "worktree": "C:/x", "development_branch": "dev", "default": True}])
    install.config()
    routes = json.loads((tmp_path / "routes.json").read_text(encoding="utf-8"))
    assert routes == [{"name": "main", "worktree": "C:/x", "development_branch": "dev"}]
    (tmp_path / "project.json").write_text('{"name": "Mine"}', encoding="utf-8")
    install.config()
    assert json.loads((tmp_path / "project.json").read_text(encoding="utf-8")) == {"name": "Mine"}


def test_claude_replaces_only_the_brokers_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "HERE", tmp_path / "broker")
    monkeypatch.setattr(install, "_state", lambda: (tmp_path, "unity_broker", "unityMCP"))
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"},
                                                                    "unity_broker": {"command": "old"}}}), encoding="utf-8")
    other_hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": "lint.py"}]}
    old_block = {"matcher": "mcp__unityMCP__.*", "hooks": [{"type": "command", "command": "python block_direct_unity.py"}]}
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": []}, "hooks": {"PreToolUse": [other_hook, old_block]}}), encoding="utf-8")
    install.claude()
    install.claude()  # running it twice must not duplicate anything
    mcp = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert mcp["other"] == {"command": "x"}
    assert mcp["unity_broker"] == {"command": str(install.PYTHON), "args": [str(tmp_path / "broker" / "broker.py")]}
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    pre = settings["hooks"]["PreToolUse"]
    assert settings["permissions"] == {"allow": []} and pre[0] == other_hook and len(pre) == 2
    assert install.PYTHON.as_posix() in pre[1]["hooks"][0]["command"]


def test_claude_hooks_merge_keeps_other_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    mine = {"hooks": [{"type": "command", "command": "notify.exe"}]}
    old = {"hooks": [{"type": "command", "command": "python old/session_hook.py --source claude"}]}
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [mine, old]}}), encoding="utf-8")
    install.claude_hooks(write=True)
    hooks = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
    assert hooks["Stop"][0] == mine and len(hooks["Stop"]) == 2
    assert set(hooks) == {*install.HOOK_EVENTS, "PostToolUse"}
    assert (tmp_path / ".claude" / "settings.json.bak").is_file()


def test_untrack_keeps_the_file_and_the_shared_index(repo):
    (repo / "routes.json").write_text("[]", encoding="utf-8")
    (repo / "other.txt").write_text("someone else's", encoding="utf-8")
    git(repo, "add", "routes.json", "other.txt")
    git(repo, "commit", "-q", "-m", "track")
    (repo / "other.txt").write_text("staged by another chat", encoding="utf-8")
    git(repo, "add", "other.txt")
    result = commit.commit(repo, [], "untrack routes", untrack=[str(repo / "routes.json")])
    assert result["committed"]
    assert (repo / "routes.json").is_file()
    assert "routes.json" not in git(repo, "ls-tree", "--name-only", "HEAD").split()
    assert git(repo, "diff", "--cached", "--name-only") == "other.txt"  # the other chat's staging is untouched
    assert "routes.json" not in git(repo, "ls-files").split()
