"""Read lightweight process CPU and memory, and Windows GPU memory counters."""

import os
import re
import time
from collections import defaultdict
from functools import lru_cache

import psutil
from broker_state import PROJECT
from gpu_adapters import enumerate_adapters


MIB = 1024 * 1024
MEMORY_COUNTERS = {
    "committed": "Committed Bytes",
    "commit_limit": "Commit Limit",
    "nonpaged": "Pool Nonpaged Bytes",
    "paged_resident": "Pool Paged Resident Bytes",
}


@lru_cache(maxsize=1)
def adapters():
    return enumerate_adapters()


def matched_adapters(usage):
    by_hardware = defaultdict(list)
    for adapter in adapters():
        by_hardware[adapter["hardware"]].append(adapter)
    result = []
    for group in by_hardware.values():
        active = [adapter for adapter in group if adapter["luid"] in usage]
        for adapter in active or group[:1]:
            result.append(dict(adapter, usage=usage.get(adapter["luid"])))
    return result


def system_counters():
    try:
        import win32pdh as pdh
    except ImportError:
        return {}
    query = pdh.OpenQuery()
    try:
        counters = {
            key: pdh.AddCounter(query, f"\\Memory\\{counter}")
            for key, counter in MEMORY_COUNTERS.items()
        }
        pdh.CollectQueryData(query)
        return {
            key: pdh.GetFormattedCounterValue(counter, pdh.PDH_FMT_LARGE)[1]
            for key, counter in counters.items()
        }
    except pdh.error:
        return {}
    finally:
        pdh.CloseQuery(query)


def gpu_memory():
    try:
        import win32pdh as pdh
    except ImportError:
        return {}, {}

    query = pdh.OpenQuery()
    try:
        counters = []
        for category in ("GPU Adapter Memory", "GPU Process Memory"):
            _, instances = pdh.EnumObjectItems(None, None, category, pdh.PERF_DETAIL_WIZARD)
            for instance in set(instances):
                match = re.match(r"pid_(\d+)_", instance)
                luid = re.search(r"luid_0x([0-9a-f]+)_0x([0-9a-f]+)", instance, re.I)
                if category == "GPU Process Memory" and not match:
                    continue
                if category == "GPU Adapter Memory" and not luid:
                    continue
                key = int(match[1]) if match else tuple(int(part, 16) for part in luid.groups())
                for field in ("Dedicated Usage", "Shared Usage"):
                    path = pdh.MakeCounterPath((None, category, instance, None, 0, field))
                    try:
                        counters.append((key, field, pdh.AddCounter(query, path)))
                    except pdh.error:
                        continue
        if not counters:
            return {}, {}
        pdh.CollectQueryData(query)
        totals = {}
        for key, field, counter in counters:
            try:
                _, value = pdh.GetFormattedCounterValue(counter, pdh.PDH_FMT_LARGE)
            except pdh.error:
                continue
            counts = totals.setdefault(key, {"Dedicated Usage": 0, "Shared Usage": 0})
            counts[field] += value
        return ({key: value for key, value in totals.items() if isinstance(key, tuple)},
                {key: value for key, value in totals.items() if isinstance(key, int)})
    except pdh.error:
        return {}, {}
    finally:
        pdh.CloseQuery(query)


# Rough sizes for the "room for" line (a mid-sized Unity 6 project): an editor ~5 GiB with its workers,
# a standalone player ~1 GiB. The broker's own launch check keeps 2 GiB RAM free for a player.
EDITOR_GIB = 5.0
PLAYER_GIB = 1.0
RESERVE_GIB = 2.0
UNITY_NAMES = {"unity.exe"}
PLAYER_NAMES = {name.lower() for name in PROJECT.get("process_names", {}).get("players", [])}


# CPU percentages need two samples: the previous process table's CPU times, keyed by (pid, start time) so a reused pid
# never counts another process's time. Percentages are of the whole machine (all logical processors), like Task
# Manager, and average over the time since the previous sample (5 s while the Overview or Machine page is open).
LOGICAL_CPUS = os.cpu_count() or 1
_previous = {"at": None, "cpu": {}}


def _cpu_percents(table):
    """{pid: percent of the machine since the previous sample}; empty on the first sample."""
    now = time.monotonic()
    current = {pid: (info["created"], info.get("cpu", 0.0)) for pid, info in table.items()}
    before, at = _previous["cpu"], _previous["at"]
    _previous.update(at=now, cpu=current)
    if at is None or now - at < 0.5:
        return {}
    budget = (now - at) * LOGICAL_CPUS
    result = {}
    for pid, (created, cpu) in current.items():
        old = before.get(pid)
        used = cpu - old[1] if old and old[0] == created else cpu if created and created >= time.time() - (now - at) else 0.0
        result[pid] = max(0.0, min(100.0, used / budget * 100))
    return result


def _project(pid):
    """The -projectPath of a Unity process (only Unity processes are opened, so this stays cheap)."""
    try:
        args = psutil.Process(pid).cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    lowered = [item.casefold() for item in args]
    if "-projectpath" in lowered:
        index = lowered.index("-projectpath")
        if index + 1 < len(args):
            return args[index + 1].replace("\\", "/").rstrip("/")
    return None


def sample(rows):
    """Memory snapshot for the Performance tab. One process-table call, not one call per process."""
    import procs

    memory = psutil.virtual_memory()
    gpu, gpu_by_pid = gpu_memory()
    table = procs.processes()
    cpu = _cpu_percents(table)
    # psutil keeps its own previous reading; its first call only primes it, so the first sample shows no number.
    machine_cpu = psutil.cpu_percent(interval=None)
    if not cpu:
        machine_cpu = None

    groups, cpu_groups = defaultdict(int), defaultdict(float)
    for pid, info in table.items():
        if info["name"] and pid:  # pid 0 is the Idle process: its "CPU" is free time
            groups[info["name"]] += info["ws"]
            cpu_groups[info["name"]] += cpu.get(pid, 0.0)

    def dedicated(pids):
        values = [gpu_by_pid[pid]["Dedicated Usage"] for pid in pids if pid in gpu_by_pid]
        return sum(values) if values else None

    targets, claimed = [], set()

    def add(name, kind, pid, note=""):
        helpers = [child for child in procs.descendants(table, pid) if child not in claimed]
        claimed.update([pid, *helpers])
        own = table[pid]["ws"]
        helper_ram = sum(table[child]["ws"] for child in helpers)
        targets.append({
            "name": name, "kind": kind, "pid": pid, "ram": own + helper_ram, "own": own,
            "cpu": sum(cpu.get(item, 0.0) for item in (pid, *helpers)) if cpu else None,
            "helpers": len(helpers), "helper_ram": helper_ram, "gpu": dedicated([pid, *helpers]), "note": note,
        })

    # Windows the dashboard already knows (editor per worktree, registered players).
    for row in rows:
        for window in row["windows"]:
            pid = window["pid"]
            info = table.get(pid)
            if info is None or abs(info["created"] - window["started_at"]) > 1:
                continue
            add(row["name"], window["kind"], pid)

    # Every other Unity editor or project player: extra editors are the RAM problem this tab exists to show.
    known = {str(row["path"]).replace("\\", "/").casefold(): row["name"] for row in rows}
    for pid, info in sorted(table.items(), key=lambda item: -item[1]["ws"]):
        lowered = info["name"].lower()
        if pid in claimed or (lowered not in UNITY_NAMES and lowered not in PLAYER_NAMES):
            continue
        parent = table.get(info["ppid"])
        if parent and parent["name"].lower() in UNITY_NAMES and info["ppid"] in claimed:
            continue
        if lowered in UNITY_NAMES:
            project = _project(pid) or "unknown project"
            name = known.get(project.casefold(), project)
            note = "" if project.casefold() in known else "not a worktree"
            add(name, "Editor" if info["ws"] > 512 * 2 ** 20 else "Unity worker", pid, note)
        else:
            add("Standalone player", info["name"], pid, "not registered")

    available_gib = memory.available / 2 ** 30
    spare = max(0.0, available_gib - RESERVE_GIB)
    room = {
        "players": int(spare // PLAYER_GIB),
        "editor": spare >= EDITOR_GIB,
        "editors_running": sum(1 for item in targets if item["kind"] == "Editor"),
    }
    return {
        "total_ram": memory.total, "available_ram": memory.available,
        "gpus": matched_adapters(gpu),
        "targets": targets, "system": system_counters(), "room": room,
        "top": sorted(groups.items(), key=lambda item: item[1], reverse=True)[:5],
        "cpu": machine_cpu, "logical_cpus": LOGICAL_CPUS,
        "top_cpu": sorted(cpu_groups.items(), key=lambda item: item[1], reverse=True)[:5] if cpu else [],
    }
