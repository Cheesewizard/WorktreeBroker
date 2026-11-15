"""A named, time-limited claim on the main Unity editor for multi-step work.

The per-call lock (`editor.lock`) keeps two calls from running at once, but a chat doing "enter Play mode, step, capture"
makes several calls, and another chat could run between them. A lease reserves the editor across those calls:

- `editor_lease(minutes, reason)` takes or renews it (1-20 minutes a grant, 30 minutes in a row at most; then it
  ends and the same chat waits 60 s before taking it again, so a waiting chat gets its turn). Calls from other chats
  wait for it like they wait for the lock, then fail with who holds it, why and until when.
- It belongs to the broker process that took it, which is one MCP session (one chat). If that process ends, the lease
  is gone too, so a crashed chat never leaves the editor reserved.
- It always ends: the deadline is the point. A chat stuck polling a dead capture loses the editor when its lease runs out.
- It also lapses when the holder stops using the editor: no editor call from it for `lease_idle_minutes` (project.json,
  default 5) and none running. Chats often finish early and never call editor_release, which would keep other chats
  waiting for the full grant.

State: `.runs/main/lease.json` ({holder_pid, holder, worktree, reason, first_granted, granted, expires, last_used}), guarded by a
file lock so two brokers never both take it.
"""

import json
import msvcrt
import os
import time
from contextlib import contextmanager

from broker_state import HERE, PROJECT, write_json

FILE = HERE / ".runs" / "main" / "lease.json"
GUARD = HERE / ".runs" / "main" / "lease.lock"
MAX_GRANT_MINUTES = 20
MAX_CHAIN_MINUTES = 30
COOLDOWN_SECONDS = 60
IDLE_SECONDS = float(PROJECT.get("lease_idle_minutes", 5)) * 60


def _pid_alive(pid):
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001 - assume alive rather than steal a live lease
        return True


@contextmanager
def _guarded():
    GUARD.parent.mkdir(parents=True, exist_ok=True)
    with GUARD.open("a+b") as handle:
        deadline = time.monotonic() + 5
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("The lease file is locked by another broker; try again") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _read():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(lease):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    if lease is None:
        FILE.unlink(missing_ok=True)
    else:
        write_json(FILE, lease)  # atomic: a reader never sees half a lease and takes it for "no lease"


def _holder_busy(pid):
    """True while the holder's own editor call is running (editor_holder.py names the process inside the lock)."""
    try:
        import editor_holder

        running = editor_holder.current("main")
        return bool(running and running.get("pid") == pid)
    except Exception:  # noqa: BLE001 - unknown counts as busy, so a lease is never cut short by a read error
        return True


def idle_seconds(lease, now=None):
    now = time.time() if now is None else now
    return now - lease.get("last_used", lease.get("granted", now))


def seconds_left(lease, now=None):
    """The longest this lease can still block others: until it expires, or lapses as idle, whichever comes first. While
    the holder has a call running it can't lapse, so only the expiry counts."""
    now = time.time() if now is None else now
    until_expiry = lease.get("expires", now) - now
    if _holder_busy(lease.get("holder_pid")):
        return until_expiry
    return min(until_expiry, IDLE_SECONDS - idle_seconds(lease, now))


def current(now=None):
    """The live lease, or None (expired, holder gone, or holder idle for IDLE_SECONDS with no call running)."""
    now = time.time() if now is None else now
    lease = _read()
    if not lease or lease.get("expires", 0) <= now or not _pid_alive(lease.get("holder_pid", -1)):
        return None
    if idle_seconds(lease, now) > IDLE_SECONDS and not _holder_busy(lease["holder_pid"]):
        return None
    return lease


def touch(pid=None, now=None):
    """Note that the holder used the editor (start and end of each of its editor calls). No-op for anyone else."""
    pid = os.getpid() if pid is None else pid
    now = time.time() if now is None else now
    try:
        with _guarded():
            lease = _read()
            if lease and lease.get("holder_pid") == pid and lease.get("expires", 0) > now:
                lease["last_used"] = now
                _write(lease)
    except Exception:  # noqa: BLE001 - bookkeeping must never fail an editor call
        pass


def describe(lease, now=None):
    now = time.time() if now is None else now
    left = max(0, int(lease["expires"] - now))
    until = time.strftime("%H:%M", time.localtime(lease["expires"]))
    idle_left = max(0, int(IDLE_SECONDS - idle_seconds(lease, now)))
    return (f"{lease['holder']} ({lease['reason']}), until {until}, {left // 60} min {left % 60} s left"
            f" (ends sooner if it makes no editor call for {idle_left // 60} min {idle_left % 60} s)")


def acquire(holder, worktree, reason, minutes, pid=None, now=None):
    """Take or renew the lease for this process. Returns the lease; raises with the holder if someone else has it."""
    pid = os.getpid() if pid is None else pid
    now = time.time() if now is None else now
    minutes = max(1.0, min(float(minutes), MAX_GRANT_MINUTES))
    with _guarded():
        lease = current(now)
        if lease and lease["holder_pid"] != pid:
            raise RuntimeError(f"Main editor is leased by {describe(lease, now)}. Wait or ask the user.")
        last = _read()
        capped = last and last.get("holder_pid") == pid and last["expires"] >= last["first_granted"] + MAX_CHAIN_MINUTES * 60 - 1
        if capped and last["expires"] <= now < last["expires"] + COOLDOWN_SECONDS:
            raise RuntimeError(f"You held the editor for {MAX_CHAIN_MINUTES} minutes in a row; other chats get "
                               f"{COOLDOWN_SECONDS} s to take it first. Try again after that.")
        first = lease["first_granted"] if lease else now
        expires = min(now + minutes * 60, first + MAX_CHAIN_MINUTES * 60)
        lease = {"holder_pid": pid, "holder": holder, "worktree": str(worktree), "reason": reason or "no reason given",
                 "first_granted": first, "granted": now, "expires": expires, "last_used": now}
        _write(lease)
        return lease


def release(pid=None, force=False):
    """Drop the lease if this process holds it (or any lease with force, for the dashboard). True if one was dropped."""
    pid = os.getpid() if pid is None else pid
    with _guarded():
        lease = _read()
        if lease and (force or lease.get("holder_pid") == pid):
            _write(None)
            return True
        return False


def blocking_lease(pid=None):
    """The lease another process holds, which this call must wait behind; None if the editor is free for us."""
    pid = os.getpid() if pid is None else pid
    lease = current()
    return lease if lease and lease["holder_pid"] != pid else None
