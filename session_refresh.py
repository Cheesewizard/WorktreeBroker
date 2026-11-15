"""Refresh registered chat rows from Codex, without starting or resuming turns."""

from contextlib import contextmanager
import json
import os
from queue import Empty, Queue
import shutil
import subprocess
from threading import Thread

from broker_state import SESSIONS, read_json, write_json
from session_control import AppServer, last_turn, session_host


class StdioSocket:
    def __init__(self, process):
        self.process = process
        self.lines = Queue()
        Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, value):
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()

    def recv(self, timeout):
        try:
            line = self.lines.get(timeout=timeout)
        except Empty as error:
            raise TimeoutError("Codex did not return recorded chat state") from error
        if line is None:
            raise RuntimeError("Codex App Server closed before returning chat state")
        return line


@contextmanager
def recorded_host():
    executable = shutil.which("codex.exe")
    if not executable:
        raise RuntimeError("Codex CLI was not found")
    with subprocess.Popen(
        [executable, "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
    ) as process:
        socket = StdioSocket(process)
        server = AppServer(socket)
        try:
            server.request("initialize", clientInfo={"name": "worktree-broker-dashboard-refresh", "version": "0.1.0"})
            socket.send(json.dumps({"method": "initialized"}))
            yield server
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=3)


def refresh_sessions():
    # Only Codex chats can be re-read from Codex; Claude Code chats are kept current by their hooks.
    files = [f for f in SESSIONS.glob("*.json") if read_json(f).get("source", "codex") == "codex"]
    if not files:
        return "Refreshed. (No Codex chats to re-read; Claude Code chats update through their hooks.)"
    connected = bool(os.environ.get("BROKER_CODEX_URL"))
    refreshed = 0
    failed = 0
    with (session_host() if connected else recorded_host()) as client:
        for file in files:
            session = read_json(file)
            try:
                thread = client.request("thread/read", threadId=session["id"])["thread"]
                if thread["id"] != session["id"]:
                    raise RuntimeError("Codex returned a different chat")
                turn = last_turn(client, session["id"])
            except (RuntimeError, KeyError):
                failed += 1
                continue
            time = max(thread.get("updatedAt") or 0, turn.get("startedAt") or 0,
                       turn.get("completedAt") or 0)
            current = read_json(file)
            if time and time >= current.get("updated_at", 0):
                state = {
                    "inProgress": "Turn open in log",
                    "completed": "Finished",
                    "failed": "Failed",
                    "interrupted": "Interrupted",
                }.get(turn.get("status"), "Idle")
                if not connected and turn.get("status") in {"interrupted", "inProgress"} and not turn.get("completedAt"):
                    if current.get("turn_id") == turn.get("id") and current["state"] in {"Working", "Active when checked"}:
                        state = current["state"]
                    else:
                        state = "Unfinished in log"
                if connected and thread["status"]["type"] == "active":
                    state = "Working"
                write_json(file, {
                    **current, "state": state, "updated_at": time, "turn_id": turn.get("id"),
                })
            refreshed += 1
    kind = "live host" if connected else "saved Codex records"
    return f"Refreshed {refreshed} chats from {kind}" + (f" · {failed} unavailable." if failed else ".")
