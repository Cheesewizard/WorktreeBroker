"""Identify and focus top-level Windows windows by process identity."""

import ctypes
from ctypes import wintypes

import psutil


USER = ctypes.WinDLL("user32", use_last_error=True)
CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
USER.EnumWindows.argtypes = [CALLBACK, wintypes.LPARAM]
USER.IsWindowVisible.argtypes = [wintypes.HWND]
USER.GetWindowTextLengthW.argtypes = [wintypes.HWND]
USER.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
USER.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
USER.GetForegroundWindow.restype = wintypes.HWND
USER.SetForegroundWindow.argtypes = [wintypes.HWND]
USER.IsIconic.argtypes = [wintypes.HWND]
USER.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
USER.FlashWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
USER.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
USER.GetWindow.restype = wintypes.HWND


def owner(hwnd):
    pid = wintypes.DWORD()
    USER.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def visible_windows():
    result = {}

    @CALLBACK
    def visit(hwnd, _):
        length = USER.GetWindowTextLengthW(hwnd)
        if length and USER.IsWindowVisible(hwnd) and not USER.GetWindow(hwnd, 4):
            title = ctypes.create_unicode_buffer(length + 1)
            USER.GetWindowTextW(hwnd, title, len(title))
            pid = owner(hwnd)
            result.setdefault(pid, {"pid": pid, "hwnd": hwnd, "title": title.value})
        return True

    USER.EnumWindows(visit, 0)
    return result


def jump_to_window(window):
    hwnd = window["hwnd"]
    pid = window["pid"]
    if owner(hwnd) != pid or psutil.Process(pid).create_time() != window["started_at"]:
        raise RuntimeError("The window has closed or changed. Refresh and try again.")
    if USER.IsIconic(hwnd):
        USER.ShowWindow(hwnd, 9)
    USER.SetForegroundWindow(hwnd)


def confirm_focus(window):
    hwnd = window["hwnd"]
    if USER.GetForegroundWindow() == hwnd:
        return True
    if owner(hwnd) == window["pid"]:
        USER.FlashWindow(hwnd, True)
    return False
