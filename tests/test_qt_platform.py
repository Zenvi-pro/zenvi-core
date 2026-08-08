"""Qt platform-plugin selection (Wayland → XWayland).

Qt 5's Wayland plugin can't grab the pointer for ordinary windows, can't
position a top-level window, and reports synthesised global coordinates — so
QDockWidget's drag-to-dock is dead on a Wayland session and a panel dragged out
of the main window can't be dragged back in. We ask for xcb (XWayland) instead,
but only when that's safe: never over an explicit user choice, and never
without an X server to fall back to.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.qt_platform import select_qt_platform, should_force_xcb  # noqa: E402

WAYLAND_ENV = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}


def test_forces_xcb_on_a_wayland_session_with_xwayland():
    assert should_force_xcb(dict(WAYLAND_ENV), "linux") is True


def test_detects_wayland_from_wayland_display_alone():
    # Some sessions (and su/sudo shells) don't export XDG_SESSION_TYPE
    env = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
    assert should_force_xcb(env, "linux") is True


def test_leaves_x11_sessions_alone():
    env = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
    assert should_force_xcb(env, "linux") is False


def test_never_overrides_an_explicit_choice():
    env = dict(WAYLAND_ENV, QT_QPA_PLATFORM="wayland")
    assert should_force_xcb(env, "linux") is False
    # ...including the offscreen platform the test suite itself uses
    env = dict(WAYLAND_ENV, QT_QPA_PLATFORM="offscreen")
    assert should_force_xcb(env, "linux") is False


def test_does_not_force_xcb_without_an_x_server():
    # Wayland with no XWayland: forcing xcb would stop the app starting at all
    env = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}
    assert should_force_xcb(env, "linux") is False


def test_other_platforms_are_untouched():
    for platform in ("darwin", "win32"):
        assert should_force_xcb(dict(WAYLAND_ENV), platform) is False


def test_select_qt_platform_sets_the_env_var():
    env = dict(WAYLAND_ENV)
    assert select_qt_platform(env, "linux") == "xcb"
    assert env["QT_QPA_PLATFORM"] == "xcb"


def test_select_qt_platform_is_a_no_op_when_not_needed():
    env = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
    assert select_qt_platform(env, "linux") is None
    assert "QT_QPA_PLATFORM" not in env
