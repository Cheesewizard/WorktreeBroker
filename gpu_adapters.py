"""Read physical GPU identities and memory capacities from DXGI."""

import ctypes
from ctypes import wintypes
from uuid import UUID


class AdapterDescription(ctypes.Structure):
    _fields_ = [
        ("name", wintypes.WCHAR * 128),
        ("vendor", wintypes.UINT),
        ("device", wintypes.UINT),
        ("subsystem", wintypes.UINT),
        ("revision", wintypes.UINT),
        ("dedicated_video", ctypes.c_size_t),
        ("dedicated_system", ctypes.c_size_t),
        ("shared_system", ctypes.c_size_t),
        ("luid_low", wintypes.DWORD),
        ("luid_high", wintypes.LONG),
        ("flags", wintypes.UINT),
    ]


def method(instance, slot, result, *arguments):
    vtable = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(vtable[slot])


def enumerate_adapters():
    dxgi = ctypes.WinDLL("dxgi")
    create = dxgi.CreateDXGIFactory1
    create.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    create.restype = wintypes.LONG
    iid = ctypes.create_string_buffer(UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le)
    factory = ctypes.c_void_p()
    if create(iid, ctypes.byref(factory)) < 0:
        raise RuntimeError("DXGI could not open the GPU factory")
    adapters = []
    try:
        get_adapter = method(factory, 12, wintypes.LONG, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            result = get_adapter(factory, index, ctypes.byref(adapter))
            if result & 0xFFFFFFFF == 0x887A0002:
                break
            if result < 0:
                raise RuntimeError(f"DXGI could not enumerate GPU {index}")
            index += 1
            try:
                desc = AdapterDescription()
                get_desc = method(adapter, 10, wintypes.LONG, ctypes.POINTER(AdapterDescription))
                if get_desc(adapter, ctypes.byref(desc)) < 0:
                    raise RuntimeError("DXGI could not read a GPU description")
                if desc.flags & 2 or not desc.vendor or not desc.dedicated_video:
                    continue
                adapters.append({
                    "name": desc.name,
                    "hardware": (desc.vendor, desc.device, desc.subsystem),
                    "luid": (desc.luid_high & 0xFFFFFFFF, desc.luid_low),
                    "dedicated_max": desc.dedicated_video,
                    "shared_max": desc.shared_system,
                })
            finally:
                method(adapter, 2, wintypes.ULONG)(adapter)
    finally:
        method(factory, 2, wintypes.ULONG)(factory)
    return adapters
