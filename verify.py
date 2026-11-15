"""`verify`: let a worktree chat check its own work without touching the shared editor.

The checks are the project's own commands, listed in project.json's "verify" section (see project.example.json). Each
runs inside the worktree, against its saved files; `{main}` in a command is the main checkout, so an older worktree
runs the current copy of a checker script, and `{work}` is `.runs/verify/<worktree>/`, where build output and logs go
(never into the worktree). A check can name checks to run first (lint before compile: reported, never blocking), skip
steps whose scripts are missing, and pick an output parser: `unity-compile-check` (assemblies and csc errors),
`dotnet-test` (totals and failing tests) or `lines` (default: the output's first lines).

At most two slow verifies run at once on the machine (each dotnet build uses 1-2 GB); a third waits for a slot. Checks
marked "slot": false skip that queue. The result is a short pass/fail summary with the first errors or failing tests,
not the full log (that is saved in the work folder and its path returned).
"""

import msvcrt
import os
import re
import subprocess
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from broker_state import HERE, PROJECT, REPOSITORY, fill

SLOTS = 2
SLOT_WAIT_SECONDS = 15 * 60
CHECK_TIMEOUT_SECONDS = 30 * 60
MAX_LINES = 40


@contextmanager
def _slot():
    folder = HERE / ".runs" / "verify"
    folder.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + SLOT_WAIT_SECONDS
    while True:
        for index in range(SLOTS):
            handle = (folder / f"slot-{index}.lock").open("a+b")
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                continue
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                handle.close()
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"Both verify slots stayed busy for {SLOT_WAIT_SECONDS // 60} minutes; try again later.")
        time.sleep(2)


def _work_folder(root):
    key = sha256(str(Path(root).resolve()).casefold().encode()).hexdigest()[:12]
    folder = HERE / ".runs" / "verify" / f"{Path(root).name}-{key}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _kill_tree(pid):
    """End a check and everything it started: subprocess's own kill stops only the top process, and dotnet/MSBuild/csc
    children (1-2 GB each) would keep running and hold the output pipes open."""
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _run(command, cwd, log, env=None):
    started = time.monotonic()
    # No MSBuild node reuse: a reused build node outlives the check and keeps its memory.
    environment = {**os.environ, "MSBUILDDISABLENODEREUSE": "1", **(env or {})}
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=environment,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # no console flash
    try:
        output, _ = process.communicate(timeout=CHECK_TIMEOUT_SECONDS)
        code = process.returncode
    except subprocess.TimeoutExpired:
        _kill_tree(process.pid)
        try:
            output, _ = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            output = ""
        output = (output or "") + f"\n[timed out after {CHECK_TIMEOUT_SECONDS // 60} min]"
        code = -1
    log.write_text(output, encoding="utf-8")
    return code, output, time.monotonic() - started


def _parse_unity_compile_check(output, code):
    """tools/UnityCompileCheck-style output: `ok   <assembly>` / `FAIL <assembly>` lines and csc errors under gen/."""
    failed = [line[5:].strip() for line in output.splitlines() if line.startswith("FAIL ")]
    passed = sum(1 for line in output.splitlines() if line.startswith("ok   "))
    errors = []
    pattern = re.compile(r"[\/]gen[\/]([^\/]+)[\/]src[\/](.+?\(\d+,\d+\)): (error \w+: .+?) \[")
    for line in output.splitlines():
        match = pattern.search(line)
        if match:
            text = f"{match.group(1)}: {match.group(2).replace(chr(92), '/')}: {match.group(3)}"
            if text not in errors:
                errors.append(text)
    if code != 0 and not failed:
        errors = errors or output.strip().splitlines()[-10:]  # the checker stopped before compiling (a setup problem)
    return {"assemblies_ok": passed, "assemblies_failed": failed, "errors": errors[:MAX_LINES],
            "more_errors": max(0, len(errors) - MAX_LINES)}


def _parse_dotnet_test(output, code):
    totals = re.findall(r"(Passed|Failed)!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+)", output)
    failed_tests = sorted({m.strip() for m in re.findall(r"^\s*Failed (\S+) \[", output, re.MULTILINE)})
    errors = [line.strip() for line in output.splitlines() if ": error " in line]
    result = {"failed_tests": failed_tests[:MAX_LINES], "build_errors": sorted(set(errors))[:MAX_LINES]}
    if totals:
        result["passed"], result["failed"] = int(totals[-1][2]), int(totals[-1][1])
    elif code != 0 and not errors:
        result["tail"] = output.strip().splitlines()[-10:]
    return result


def _parse_lines(output, code):
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return {"details": lines[:MAX_LINES], "more": max(0, len(lines) - MAX_LINES)}


PARSERS = {"unity-compile-check": _parse_unity_compile_check, "dotnet-test": _parse_dotnet_test, "lines": _parse_lines}


def checks():
    """project.json's verify checks: {name: spec}. Each spec has steps ({command, env?, requires?, filter_args?}), an
    optional parser (one of PARSERS; default "lines"), runs_first (checks run before it) and slot (false: no
    machine-wide slot, for fast checks)."""
    return PROJECT.get("verify", {}).get("checks", {})


def default_check():
    configured = checks()
    return PROJECT.get("verify", {}).get("default") or next(iter(configured), None)


def _run_check(name, spec, root, folder, test_filter):
    values = {"main": REPOSITORY, "worktree": root, "work": folder, "filter": test_filter or ""}
    log = folder / f"{name}.log"
    outputs, code, seconds, ran = [], 0, 0.0, 0
    for index, step in enumerate(spec.get("steps", [])):
        if any(not Path(fill(path, **values)).exists() for path in step.get("requires", [])):
            continue  # a step whose script isn't in this checkout is skipped, not failed
        command = [fill(part, **values) for part in step["command"]]
        if test_filter and step.get("filter_args"):
            command += [fill(part, **values) for part in step["filter_args"]]
        env = {key: fill(value, **values) for key, value in step.get("env", {}).items()}
        step_code, output, step_seconds = _run(command, root, log if index == 0 else folder / f"{name}-{index}.log", env)
        code, seconds, ran = code or step_code, seconds + step_seconds, ran + 1
        outputs.append(output)
    if not ran:
        return {"check": name, "ok": True, "skipped": "every step's required files are missing in this checkout"}
    parser = PARSERS.get(spec.get("parser", "lines"), _parse_lines)
    result = {"check": name, "ok": code == 0, "seconds": round(seconds), **parser("\n".join(outputs), code),
              "log": str(log)}
    if spec.get("filter_args"):
        result["filter"] = test_filter or None
    return result


def verify(root, requested=None, test_filter=None):
    root = Path(root).resolve()
    configured = checks()
    if not configured:
        raise RuntimeError("project.json has no verify checks configured (see project.example.json).")
    order = list(configured)
    wanted = [c for c in (requested or [default_check()]) if c in configured] or [default_check()]
    for name in list(wanted):
        for first in configured[name].get("runs_first", []):  # e.g. lint before compile: reported, never blocking
            if first in configured and first not in wanted:
                wanted.append(first)
    wanted.sort(key=order.index)
    folder = _work_folder(root)
    with _worktree_lock(folder):
        return _verify_locked(root, configured, wanted, folder, test_filter)


@contextmanager
def _worktree_lock(folder):
    """One verify at a time per worktree: runs share {work} (the build output and logs)."""
    handle = (folder / "run.lock").open("a+b")
    deadline = time.monotonic() + SLOT_WAIT_SECONDS
    while True:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError:
            if time.monotonic() > deadline:
                handle.close()
                raise RuntimeError("Another verify of this worktree kept running; try again when it finishes.")
            time.sleep(2)
    try:
        yield
    finally:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def _verify_locked(root, configured, wanted, folder, test_filter):
    if all(configured[name].get("slot") is False for name in wanted):  # fast: no slot, no waiting
        results = [_run_check(name, configured[name], root, folder, test_filter) for name in wanted]
        return {"worktree": str(root), "ok": all(r["ok"] for r in results), "waited_for_slot_seconds": 0,
                "results": results}
    waited = time.monotonic()
    with _slot():
        waited = round(time.monotonic() - waited)
        results = [_run_check(name, configured[name], root, folder, test_filter) for name in wanted]
    return {"worktree": str(root), "ok": all(r["ok"] for r in results), "waited_for_slot_seconds": waited,
            "results": results}


# Runs in the background so no client times out: Codex gives an MCP tool 60 s by default, a compile takes ~3 minutes.
_RUNS = {}


def start(root, checks=None, test_filter=None):
    import threading
    from uuid import uuid4

    run_id = uuid4().hex[:10]
    entry = {"started": time.time(), "done": threading.Event(), "result": None}

    def work():
        try:
            entry["result"] = verify(root, checks, test_filter)
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
        raise RuntimeError(f"No verify run {run_id} in this chat (runs end with the chat's broker).")
    if entry["done"].wait(max(0.0, min(float(seconds), 55.0))):
        return {"run_id": run_id, "done": True, **entry["result"]}
    return {"run_id": run_id, "done": False, "running_seconds": round(time.time() - entry["started"]),
            "next": f"Call verify_status with run_id {run_id} to keep waiting."}
