"""Token usage per worktree and per chat, read from the AI tools' own transcripts.

Sources (no hooks needed; both tools already log exact per-call usage):

- Claude Code: `~/.claude/projects/<folder>/<session>.jsonl` (+ `<session>/subagents/agent-*.jsonl`, rolled into the
  parent chat). Each `assistant` entry has `cwd`, `sessionId`, the model and `message.usage` (input, cache writes, cache
  reads, output). A streamed reply is logged several times with the same message id and the same usage, so calls are
  counted once per id. A `quotaLimits` entry appears when a call hits a plan limit (status, type, reset time).
  `custom-title` entries name the chat.
- Codex: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. The first line (`session_meta`) has the session id and cwd;
  `token_count` events carry the running `total_token_usage` (counted as deltas, so repeated events add nothing) and the
  plan's `rate_limits` (used percent of the window, reset time).

Only the project's folders count: Claude project folders whose name contains its folder name, Codex sessions whose cwd is under a
project worktree. Files untouched for WINDOW_DAYS are skipped. Reading is incremental: `.runs/usage-cache.json` keeps each
file's byte offset and per-day, per-model sums, so a refresh reads only new lines (the first scan of a week of logs reads
a few GB and takes a minute or two; lines are prefiltered by substring before JSON parsing).

These are token counts, not a share of a Claude subscription's allowance (the transcripts don't carry that); Codex's
logged `used_percent` is shown as Codex reports it.
"""
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from broker_state import HERE, REPOSITORY

WINDOW_DAYS = 7
CACHE = HERE / ".runs" / "usage-cache.json"
CACHE_VERSION = 1
CLAUDE_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOT = Path.home() / ".codex" / "sessions"
FIELDS = ("input", "cache_write", "cache_read", "output")
IMAGE = re.compile(r'"type":\s*"image"')


def _day(timestamp):
    return (timestamp or "")[:10] or "unknown"


def _new_record(kind, path):
    return {"kind": kind, "path": str(path), "offset": 0, "size": 0, "session": None, "title": None, "cwd": None,
            "model": None, "skip": False, "ids": [], "buckets": {}, "images": 0, "calls": 0, "first": None, "last": None,
            "prev_total": None, "limits": None, "parent": None}


def _add(record, day, model, values):
    key = f"{day}|{model or '?'}"
    bucket = record["buckets"].setdefault(key, [0, 0, 0, 0])
    for index, value in enumerate(values):
        bucket[index] += int(value or 0)


def _complete_lines(path, offset):
    """New complete lines from offset; returns (lines, new offset). A half-written last line waits for next time."""
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    return data[:end + 1].decode("utf-8", errors="replace").splitlines(), offset + end + 1


def _read_claude(record, path):
    lines, record["offset"] = _complete_lines(path, record["offset"])
    seen = set(record["ids"])
    for line in lines:
        if '"custom-title"' in line and '"customTitle"' in line:
            try:
                record["title"] = json.loads(line).get("customTitle") or record["title"]
            except ValueError:
                pass
            continue
        if line.startswith('{"type":"user"') or '"type":"user"' in line[:200]:
            record["images"] += len(IMAGE.findall(line))
            continue
        if '"type":"assistant"' not in line or '"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") or {}
        usage = message.get("usage")
        message_id = message.get("id") or entry.get("requestId")
        if not usage or message_id in seen:
            continue
        seen.add(message_id)
        record["session"] = record["session"] or entry.get("sessionId")
        record["cwd"] = entry.get("cwd") or record["cwd"]
        record["model"] = message.get("model") or record["model"]
        if entry.get("quotaLimits"):
            record["limits"] = {"source": "claude", "at": entry.get("timestamp"), **entry["quotaLimits"]}
        stamp = entry.get("timestamp")
        record["first"] = record["first"] or stamp
        record["last"] = stamp or record["last"]
        record["calls"] += 1
        _add(record, _day(stamp), message.get("model"), (
            usage.get("input_tokens"), usage.get("cache_creation_input_tokens"),
            usage.get("cache_read_input_tokens"), usage.get("output_tokens")))
    record["ids"] = list(seen)[-5000:]


def _read_codex(record, path, is_project):
    lines, record["offset"] = _complete_lines(path, record["offset"])
    for line in lines:
        if record["cwd"] is None and '"session_meta"' in line:
            try:
                payload = json.loads(line).get("payload") or {}
            except ValueError:
                continue
            record["session"] = payload.get("id")
            record["cwd"] = payload.get("cwd")
            record["title"] = payload.get("originator")
            if not is_project(record["cwd"]):
                record["skip"] = True
                record["offset"] = path.stat().st_size
                return
            continue
        if '"turn_context"' in line and record["model"] is None:
            try:
                record["model"] = (json.loads(line).get("payload") or {}).get("model")
            except ValueError:
                pass
            continue
        if '"token_count"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        payload = entry.get("payload") or {}
        info = payload.get("info") or {}
        total = info.get("total_token_usage")
        if payload.get("rate_limits"):
            record["limits"] = {"source": "codex", "at": entry.get("timestamp"), **payload["rate_limits"]}
        if not total:
            continue
        previous = record["prev_total"] or {}
        delta = {key: int(total.get(key) or 0) - int(previous.get(key) or 0) for key in total}
        record["prev_total"] = total
        if delta.get("total_tokens", 0) <= 0:
            continue
        stamp = entry.get("timestamp")
        record["first"] = record["first"] or stamp
        record["last"] = stamp or record["last"]
        record["calls"] += 1
        cached = delta.get("cached_input_tokens", 0)
        _add(record, _day(stamp), record["model"], (
            delta.get("input_tokens", 0) - cached, delta.get("cache_write_input_tokens", 0), cached,
            delta.get("output_tokens", 0)))
    if record["cwd"] is None and record["offset"] > 65536:
        record["skip"] = True  # no session_meta in the first 64 KB: not a session file we understand


def _files(cutoff):
    for folder in CLAUDE_ROOT.glob(f"*{REPOSITORY.name}*"):
        for path in folder.glob("*.jsonl"):
            yield "claude", path, None
        for path in folder.glob("*/subagents/*.jsonl"):
            yield "claude", path, path.parent.parent.name
    for path in CODEX_ROOT.glob("*/*/*/*.jsonl"):
        yield "codex", path, None


def _load():
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if data.get("version") == CACHE_VERSION:
            return data
    except (OSError, ValueError):
        pass
    return {"version": CACHE_VERSION, "files": {}}


def _save(data):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    temp = CACHE.with_suffix(".tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temp, CACHE)


def refresh(worktree_paths, window_days=WINDOW_DAYS, progress=None):
    """Read new transcript lines; returns the cache data. worktree_paths: the project's checkout roots (for Codex filtering)."""
    roots = [str(Path(path)).lower().rstrip("\\/") for path in worktree_paths]
    roots.append(str(Path.home() / ".codex" / "worktrees").lower())  # Codex's own worktrees

    name = REPOSITORY.name.lower()

    def is_project(cwd):
        low = (cwd or "").lower()
        return name in low or any(low.startswith(root) for root in roots)

    data = _load()
    cutoff = time.time() - window_days * 86400
    files = data["files"]
    live = set()
    for count, (kind, path, parent) in enumerate(_files(cutoff)):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            continue
        key = str(path)
        live.add(key)
        record = files.get(key)
        if record is None or stat.st_size < record["offset"]:
            record = files[key] = _new_record(kind, path)
            record["parent"] = parent
        if record["skip"] or stat.st_size == record["offset"]:
            continue
        try:
            if kind == "claude":
                _read_claude(record, path)
            else:
                _read_codex(record, path, is_project)
        except OSError:
            continue
        record["size"] = stat.st_size
        if progress and count % 25 == 0:
            progress(count)
    for key in [key for key in files if key not in live]:
        del files[key]
    data["scanned_at"] = time.time()
    _save(data)
    return data


def _worktree_of(cwd, worktrees):
    low = (cwd or "").lower()
    best = None
    for path in worktrees:
        root = str(path).lower().rstrip("\\/")
        if (low == root or low.startswith(root + "\\") or low.startswith(root + "/")) and (best is None or len(root) > len(str(best))):
            best = path
    return str(best) if best else (cwd or "?")


def summarize(data, worktrees, days=1):
    """Totals for the last `days` calendar days (1 = today): per chat, per worktree, plus latest plan limits."""
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1) * 86400))
    chats = {}
    limits = {}
    for record in data["files"].values():
        if record["skip"] or not record["calls"]:
            continue
        sums = [0, 0, 0, 0]
        models = defaultdict(int)
        for key, values in record["buckets"].items():
            day, model = key.split("|", 1)
            if day < since:
                continue
            for index in range(4):
                sums[index] += values[index]
            models[model] += sum(values)
        if record["limits"]:
            source = record["limits"]["source"]
            if source not in limits or (record["limits"].get("at") or "") > (limits[source].get("at") or ""):
                limits[source] = record["limits"]
        if not any(sums):
            continue
        session = record["parent"] or record["session"] or Path(record["path"]).stem
        chat = chats.setdefault((record["kind"], session), {
            "tool": record["kind"], "session": session, "title": None, "worktree": None, "sums": [0, 0, 0, 0],
            "models": defaultdict(int), "images": 0, "calls": 0, "last": None, "subagent_tokens": 0})
        if record["parent"]:
            chat["subagent_tokens"] += sum(sums)
        else:
            chat["title"] = record["title"] or chat["title"]
            chat["worktree"] = _worktree_of(record["cwd"], worktrees)
        for index in range(4):
            chat["sums"][index] += sums[index]
        for model, value in models.items():
            chat["models"][model] += value
        chat["images"] += record["images"]
        chat["calls"] += record["calls"]
        chat["last"] = max(filter(None, [chat["last"], record["last"]]), default=None)
    rows = sorted(chats.values(), key=lambda row: -sum(row["sums"]))
    by_worktree = defaultdict(lambda: [0, 0, 0, 0])
    for row in rows:
        row["worktree"] = row["worktree"] or "?"
        row["total"] = sum(row["sums"])
        row["models"] = dict(row["models"])
        for index in range(4):
            by_worktree[row["worktree"]][index] += row["sums"][index]
    return {"since": since, "chats": rows, "worktrees": dict(by_worktree), "limits": limits,
            "scanned_at": data.get("scanned_at")}


def short(number):
    number = float(number or 0)
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(number) >= size:
            return f"{number / size:.1f}{unit}"
    return str(int(number))


if __name__ == "__main__":
    import sys
    from broker_state import worktrees as list_worktrees
    paths = [row["path"] for row in list_worktrees()]
    started = time.time()
    data = refresh(paths)
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    report = summarize(data, paths, days)
    print(f"scan {time.time() - started:.1f} s; since {report['since']}")
    for path, sums in sorted(report["worktrees"].items(), key=lambda item: -sum(item[1])):
        print(f"{short(sum(sums)):>8}  {path}")
    for row in report["chats"][:15]:
        print(f"{short(row['total']):>8} {row['tool']:<6} {row['session'][:8]} {str(row['title'])[:40]:<40} "
              f"out {short(row['sums'][3])} cw {short(row['sums'][1])} img {row['images']}")
    print(json.dumps(report["limits"], indent=1)[:800])
