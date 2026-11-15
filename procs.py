"""Every process's name, parent, working set and CPU time from one Windows call.

psutil opens each process separately; with ~1,000 processes that took 6-17 s per refresh and starved the dashboard.
NtQuerySystemInformation(SystemProcessInformation) returns the whole table in one system call (a few milliseconds).
Offsets are the documented x64 SYSTEM_PROCESS_INFORMATION layout. Callers fall back to psutil if this fails.
"""

import ctypes
from ctypes import wintypes

SYSTEM_PROCESS_INFORMATION = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
EPOCH_AS_FILETIME = 116444736000000000  # 1970-01-01 in 100 ns ticks since 1601

_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
_ntdll.NtQuerySystemInformation.restype = ctypes.c_ulong


def _read(buffer, offset, kind):
    return kind.from_buffer(buffer, offset).value


def processes():
    """{pid: {"name", "ppid", "ws" (bytes), "created" (epoch seconds), "cpu" (user + kernel seconds)}} for every process."""
    size = 1 << 20
    for _ in range(8):
        buffer = (ctypes.c_byte * size)()
        needed = ctypes.c_ulong()
        status = _ntdll.NtQuerySystemInformation(SYSTEM_PROCESS_INFORMATION, buffer, size, ctypes.byref(needed))
        if status == STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, needed.value + (1 << 16))
            continue
        if status != 0:
            raise OSError(f"NtQuerySystemInformation failed: 0x{status:08x}")
        break
    else:
        raise OSError("NtQuerySystemInformation: process table kept growing")
    result = {}
    offset = 0
    base = ctypes.addressof(buffer)
    while True:
        next_entry = _read(buffer, offset, ctypes.c_ulong)
        created = _read(buffer, offset + 0x20, ctypes.c_longlong)
        cpu_ticks = _read(buffer, offset + 0x28, ctypes.c_longlong) + _read(buffer, offset + 0x30, ctypes.c_longlong)
        name_length = _read(buffer, offset + 0x38, ctypes.c_ushort)
        name_pointer = _read(buffer, offset + 0x40, ctypes.c_void_p)
        pid = _read(buffer, offset + 0x50, ctypes.c_void_p) or 0
        ppid = _read(buffer, offset + 0x58, ctypes.c_void_p) or 0
        working_set = _read(buffer, offset + 0x90, ctypes.c_size_t)
        name = ctypes.wstring_at(name_pointer, name_length // 2) if name_pointer else ("Idle" if pid == 0 else "")
        result[pid] = {
            "name": name, "ppid": ppid, "ws": working_set, "cpu": cpu_ticks / 1e7,
            "created": (created - EPOCH_AS_FILETIME) / 1e7 if created else 0.0,
        }
        if not next_entry:
            break
        offset += next_entry
    del base
    return result


def descendants(table, pid):
    """All processes started (directly or indirectly) by pid that are still running."""
    children = {}
    for child, info in table.items():
        children.setdefault(info["ppid"], []).append(child)
    found, stack = [], list(children.get(pid, []))
    while stack:
        child = stack.pop()
        if child == pid or child in found:
            continue
        # A reused parent id can make an old process look like a child: a real child starts after its parent.
        if table[child]["created"] and table[pid]["created"] and table[child]["created"] < table[pid]["created"]:
            continue
        found.append(child)
        stack.extend(children.get(child, []))
    return found
