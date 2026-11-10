"""Physical RAM, and memory budgets sized from it."""

import ctypes
import os
import platform

_FLOOR = 128 * 1024 * 1024


def physical_ram_bytes():
    """Total physical RAM in bytes, or 0 when it cannot be read."""
    try:
        if platform.system() == "Windows":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
            return 0
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except Exception:
        return 0


def ram_budget_bytes(wanted, ram=None):
    """*wanted* bytes, capped at a sixteenth of physical RAM (never under 128 MiB).

    8 GB allows 512 MiB per cache or queue, 16 GB allows 1 GiB. Unknown RAM
    leaves *wanted* as it is.
    """
    if ram is None:
        ram = physical_ram_bytes()
    if ram <= 0:
        return int(wanted)
    return int(min(wanted, max(_FLOOR, ram // 16)))
