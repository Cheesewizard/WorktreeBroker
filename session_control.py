"""Codex commands scoped to a registered chat and its original worktree."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse

from websockets.sync.client import connect

from broker_state import control_path, read_json, write_json


RESUME_MESSAGE = (
    "Resume the unfinished work in this same worktree. Check the saved edits and "
    "existing build and test results before repeating any operation. Keep any "
    "running Unity editor or standalone bot match running. Continue the prior task."
)


def pause_marker(session_id):
    path = control_path(session_id)
    return read_json(path) if path.exists() else None


class AppServer:
    def __init__(self, socket):
        self.socket = socket
        self.sequence = 0

    def request(self, method, **params):
        self.sequence += 1
        request_id = self.sequence
        self.socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + 12
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Codex did not acknowledge {method}")
            reply = json.loads(self.socket.recv(timeout=remaining))
            if reply.get("id") != request_id or "method" in reply:
                continue
            if "error" in reply:
                raise RuntimeError(reply["error"]["message"])
            return reply["result"]


@contextmanager
def session_host():
    url = os.environ.get("BROKER_CODEX_URL")
    if not url:
        raise RuntimeError("No Codex control host connected. Set BROKER_CODEX_URL and reopen the dashboard.")
    address = urlparse(url)
    if address.scheme != "ws" or address.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("The Codex control host must be a loopback WebSocket")
    token = os.environ.get("BROKER_CODEX_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else None
    with connect(url, additional_headers=headers, open_timeout=3, close_timeout=1,
                 proxy=None, max_size=8 * 1024 * 1024) as socket:
        client = AppServer(socket)
        client.request("initialize", clientInfo={"name": "worktree-broker-dashboard", "version": "0.1.0"})
        socket.send(json.dumps({"method": "initialized"}))
        yield client


def checked_thread(client, session):
    thread = client.request("thread/read", threadId=session["id"])["thread"]
    if thread["id"] != session["id"] or not thread.get("cwd") or (
        Path(thread["cwd"]).resolve() != Path(session["worktree"]).resolve()
    ):
        raise RuntimeError("Codex chat no longer matches the registered worktree")
    return thread


def last_turn(client, session_id):
    result = client.request("thread/turns/list", threadId=session_id, limit=1,
                            sortDirection="desc", itemsView="notLoaded")
    return next(iter(result["data"]), {})


def pause(client, session):
    thread = checked_thread(client, session)
    if thread["status"]["type"] != "active":
        raise RuntimeError("The selected Codex chat has no active turn")
    turn = last_turn(client, session["id"])
    if turn.get("status") != "inProgress":
        raise RuntimeError("Codex did not report an interruptible turn")
    client.request("turn/interrupt", threadId=session["id"], turnId=turn["id"])
    write_json(control_path(session["id"]), {
        "id": session["id"], "worktree": session["worktree"],
        "turn_id": turn["id"], "requested_at": time.time(),
    })
    return "Pause requested. Unity and player windows continue independently."


def resume(client, session, recovery=False):
    thread = checked_thread(client, session)
    if thread["status"]["type"] == "active":
        raise RuntimeError("The selected chat is already active")
    previous = last_turn(client, session["id"])
    marker = pause_marker(session["id"])
    marked = marker and marker["turn_id"] == previous.get("id")
    if recovery and marked:
        return None
    if previous.get("status") not in {"interrupted", "failed", "inProgress"}:
        return None
    client.request("thread/resume", threadId=session["id"], excludeTurns=True)
    result = client.request(
        "turn/start", threadId=session["id"], cwd=session["worktree"],
        input=[{"type": "text", "text": RESUME_MESSAGE}],
    )
    return result["turn"]["id"]


def inspect(client, session):
    thread = checked_thread(client, session)
    previous = last_turn(client, session["id"])
    marker = pause_marker(session["id"])
    if thread["status"]["type"] == "active":
        return "Pausing" if marker and marker["turn_id"] == previous.get("id") else "Working"
    if marker and marker["turn_id"] == previous.get("id") and previous.get("status") == "interrupted":
        return "Paused"
    return {"interrupted": "Interrupted", "failed": "Failed", "inProgress": "Interrupted",
            "completed": "Finished"}.get(previous.get("status"), "Idle")


def recover_next(client, sessions):
    """Recover one eligible chat. The dashboard waits before taking another."""
    errors = []
    for session in sessions:
        try:
            state = inspect(client, session)
            if state not in {"Interrupted", "Failed"}:
                continue
            turn_id = resume(client, session, recovery=True)
            if turn_id:
                return session, errors
        except RuntimeError as error:
            errors.append(f"{Path(session['worktree']).name}: {error}")
    return None, errors
