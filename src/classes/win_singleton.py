"""
 @file
 @brief Windows-only named mutex so Inno Setup's AppMutex-driven
        /CLOSEAPPLICATIONS can detect and close running Zenvi processes
        during a silent auto-update install.
 @author Zenvi Team

 @section LICENSE

 Copyright (c) 2008-2026 Zenvi.
 This file is part of Zenvi Video Editor (https://zenvi.pro).

 Zenvi is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
"""

import sys

# Must match the AppMutex directive in installer/windows-installer.iss exactly.
MUTEX_NAME = r"ZenviAppMutex"
GLOBAL_MUTEX_NAME = r"Global\ZenviAppMutex"

_handles = []


def acquire():
    """Create (not merely open) the named mutex(es) that identify a running
    Zenvi process, so Inno Setup's AppMutex-driven /CLOSEAPPLICATIONS can find
    and close us during a silent update install.

    Idempotent, never raises, no-op on non-Windows. Does not function as a
    lock (bInitialOwner is always False) — only the existence of a handle to
    the named object matters; many processes may hold one simultaneously.
    """
    if sys.platform != "win32":
        return False
    if _handles:
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE

    ok = False
    for name in (MUTEX_NAME, GLOBAL_MUTEX_NAME):
        try:
            handle = kernel32.CreateMutexW(None, False, name)
            if handle:
                _handles.append(handle)
                ok = True
            # ERROR_ALREADY_EXISTS (183) is expected/fine here — we don't care
            # who "owns" it, only that this process holds a handle to it.
        except Exception:
            # e.g. Global\ creation denied without SeCreateGlobalPrivilege.
            pass
    return ok


def release():
    """Explicitly close held mutex handles. Only needed by tests — normal
    process exit (clean or crash) releases these for free."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    for handle in _handles:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass
    _handles.clear()
