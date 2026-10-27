"""Keep the windowed Windows build from flashing a console per child process."""

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000


def hide_child_consoles():
    """Give every child process a hidden console when this process has none.

    Zenvi.exe is a GUI-subsystem program, so Windows opens a new console window
    for each console child it starts (hermes, claude, cmd shims, taskkill...).
    Zenvi-cli.exe owns a console that children inherit, so it is left alone.
    """
    if sys.platform != "win32":
        return
    import ctypes

    if ctypes.windll.kernel32.GetConsoleWindow():
        return
    popen_init = subprocess.Popen.__init__

    def init(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
        popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = init
