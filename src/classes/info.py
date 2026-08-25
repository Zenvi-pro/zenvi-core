"""
 @file
 @brief This file contains the current version number of OpenShot, along with other global settings.
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2018 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

 OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.
 """

import os
import sys
from time import strftime

VERSION = "1.1.0"
# 0.3.2 minimum for systems where only stable PPA (or older) is available (e.g. aarch64).
MINIMUM_LIBOPENSHOT_VERSION = "0.3.2"
DATE = "20260813000000"
NAME = "zenvi"
PRODUCT_NAME = "Zenvi"
GPL_VERSION = "3"
DESCRIPTION = "Create and edit stunning videos, films, and animations with an " \
              "easy-to-use interface and rich set of features."
COMPANY_NAME = "Zenvi"
COPYRIGHT = "(c) 2008-{} {}".format(strftime("%Y"), COMPANY_NAME)
CWD = os.getcwd()

# Application paths
PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))  # Primary openshot folder

# In frozen builds (cx_Freeze), __file__ may resolve differently across versions.
# Verify PATH by checking for a known subdirectory; fall back to exe-relative paths.
if getattr(sys, 'frozen', False):
    _settings_check = os.path.join(PATH, 'settings', '_default.settings')
    if not os.path.exists(_settings_check):
        _exe_dir = os.path.dirname(sys.executable)
        if os.path.exists(os.path.join(_exe_dir, 'lib', 'settings', '_default.settings')):
            PATH = os.path.join(_exe_dir, 'lib')
        elif os.path.exists(os.path.join(_exe_dir, 'settings', '_default.settings')):
            PATH = _exe_dir

RESOURCES_PATH = os.path.join(PATH, "resources")
PROFILES_PATH = os.path.join(PATH, "profiles")
IMAGES_PATH = os.path.join(PATH, "images")
EXPORT_PRESETS_PATH = os.path.join(PATH, "presets")
COLORS_PATH = os.path.join(PATH, "colors")


def _repo_root():
    """Project root (parent of src/) in dev; exe directory when frozen."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(PATH)


def application_icon_paths():
    """Candidate icon paths (.ico preferred, then .svg), most likely first."""
    root = _repo_root()
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = root
        candidates.extend([
            os.path.join(exe_dir, "zenvi.ico"),
            os.path.join(exe_dir, "xdg", "zenvi.ico"),
            os.path.join(exe_dir, "lib", "xdg", "zenvi.ico"),
            os.path.join(exe_dir, "zenvi.svg"),
            os.path.join(exe_dir, "lib", "xdg", "zenvi.svg"),
        ])
    prefix = getattr(sys, "prefix", "")
    if prefix:
        candidates.extend([
            os.path.join(prefix, "share", "zenvi", "zenvi.ico"),
            os.path.join(prefix, "share", "pixmaps", "zenvi.svg"),
            os.path.join(prefix, "share", "icons", "hicolor", "scalable", "apps", "zenvi.svg"),
        ])
    candidates.extend([
        os.path.join(root, "xdg", "zenvi.ico"),
        os.path.join(root, "xdg", "zenvi.svg"),
        os.path.join(root, "images", "openshot.svg"),
        os.path.join(PATH, "images", "openshot.svg"),
    ])
    seen = set()
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        if os.path.isfile(p):
            yield p


def application_icon_ico_path():
    """Path to the application window/taskbar .ico, or '' if missing."""
    for p in application_icon_paths():
        if p.lower().endswith(".ico"):
            return p
    return ""


_ZENVI_APP_USER_MODEL_ID = "Zenvi.Zenvi.Editor.1"

def _is_windows():
    return sys.platform == "win32" or os.name == "nt"


def application_qicon():
    """Cached QIcon for windows and QApplication (file-based; not :/openshot.svg)."""
    try:
        from PyQt5.QtCore import QSize
        from PyQt5.QtGui import QIcon
    except ImportError:
        return None
    if getattr(application_qicon, "_cached", None) is not None:
        return application_qicon._cached
    icon = QIcon()
    ico_path = application_icon_ico_path()
    if ico_path and _is_windows():
        # Windows taskbar needs explicit sizes when running under python.exe in dev.
        win_icon = QIcon()
        for size in (16, 24, 32, 48, 64, 128, 256):
            win_icon.addFile(ico_path, QSize(size, size))
        if not win_icon.isNull():
            icon = win_icon
    if icon.isNull():
        for p in application_icon_paths():
            candidate = QIcon(p)
            if not candidate.isNull():
                icon = candidate
                break
    application_qicon._cached = icon
    return icon


def application_logo_pixmap(size=80):
    """Scaled Zenvi logo for login / about UI."""
    try:
        from PyQt5.QtCore import QSize, Qt
        from PyQt5.QtGui import QIcon, QPixmap
    except ImportError:
        return None
    logo_path = os.path.join(PATH, "logo", "logo_dark.png")
    if os.path.isfile(logo_path):
        pixmap = QPixmap(logo_path)
        if not pixmap.isNull():
            return pixmap.scaled(
                QSize(size, size), Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
    icon = application_qicon()
    if icon is None or icon.isNull():
        return None
    return icon.pixmap(QSize(size, size), QIcon.Normal, QIcon.Off)


def ensure_windows_app_user_model_id():
    """Group taskbar entry separately from python.exe (call before/after QApplication)."""
    if not _is_windows():
        return
    try:
        from PyQt5.QtWidgets import QApplication
        if QApplication.instance() is not None:
            from PyQt5.QtWinExtras import QtWin
            QtWin.setCurrentProcessExplicitAppUserModelID(_ZENVI_APP_USER_MODEL_ID)
            return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_ZENVI_APP_USER_MODEL_ID)
    except Exception:
        pass


def _windows_load_image_path(path):
    """Absolute path LoadImageW accepts (MSYS /c/... and /home/... included)."""
    path = os.path.abspath(path)
    if not _is_windows():
        return path
    if len(path) >= 3 and path[0] == "/" and path[1].isalpha() and path[2] == "/":
        return os.path.normpath(f"{path[1].upper()}:\\{path[3:].replace('/', os.sep)}")
    if path.startswith("/"):
        try:
            import subprocess
            win_path = subprocess.check_output(
                ["cygpath", "-w", path],
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).decode().strip()
            if win_path:
                return win_path
        except Exception:
            pass
    return os.path.normpath(path)


def _apply_windows_native_window_icon(widget):
    """Win32 WM_SETICON — required for taskbar when host process is python.exe."""
    if not _is_windows() or widget is None:
        return False
    ico_path = application_icon_ico_path()
    if not ico_path:
        return False
    try:
        hwnd = int(widget.winId())
    except (TypeError, ValueError):
        return False
    if hwnd <= 0:
        return False

    import ctypes
    user32 = ctypes.windll.user32
    path = _windows_load_image_path(ico_path)
    LR_LOADFROMFILE = 0x0010
    IMAGE_ICON = 1
    WM_SETICON = 0x0080
    ICON_SMALL = 0
    ICON_BIG = 1

    handles = []
    for cx, cy in ((0, 0), (16, 16), (32, 32), (48, 48), (256, 256)):
        hicon = user32.LoadImageW(
            None, path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE,
        )
        if hicon:
            handles.append(hicon)

    if not handles:
        return False

    widget._zenvi_win_hicons = handles
    primary = handles[0]
    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, primary)
    user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, handles[-1] if len(handles) > 1 else primary)
    return True


def apply_application_icon(widget=None):
    """Set Zenvi icon on QApplication and optionally a top-level widget."""
    ensure_windows_app_user_model_id()
    icon = application_qicon()
    if icon is None or icon.isNull():
        if widget is not None:
            _apply_windows_native_window_icon(widget)
        return
    try:
        from PyQt5.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is not None:
        app.setWindowIcon(icon)
    if widget is not None and hasattr(widget, "setWindowIcon"):
        widget.setWindowIcon(icon)
        _apply_windows_native_window_icon(widget)


def schedule_application_icon(widget):
    """Apply Qt + Win32 icons after the window HWND exists (post-show)."""
    if widget is None:
        return
    try:
        from PyQt5.QtCore import QTimer
    except ImportError:
        apply_application_icon(widget)
        return

    widget._zenvi_icon_retry = 0

    def _apply():
        apply_application_icon(widget)
        if _is_windows() and not _apply_windows_native_window_icon(widget):
            widget._zenvi_icon_retry += 1
            if widget._zenvi_icon_retry < 20:
                QTimer.singleShot(50, _apply)

    QTimer.singleShot(0, _apply)


def windows_profile_candidates(env=None):
    """Possible Windows home directories when USERPROFILE was stripped.

    MSYS launch scripts use ``env -i`` and historically forwarded HOME
    (``/home/user``) but not USERPROFILE. Windows Python's expanduser("~")
    ignores HOME, so without these candidates it returns the literal ``~``.
    Prefer ``C:\\Users\\<user>`` over the MSYS home — Claude Code and Codex
    store login state there after a cmd.exe install.
    """
    env = env if env is not None else os.environ
    candidates = []
    username = (env.get("USERNAME") or env.get("USER") or "").strip()
    drive = (env.get("SYSTEMDRIVE") or "C:").rstrip("\\/")
    if username:
        candidates.append(os.path.join(drive + os.sep, "Users", username))
    home = (env.get("HOME") or "").strip()
    if home and home not in ("~", "/"):
        if len(home) >= 3 and home[0] == "/" and home[1].isalpha() and home[2] == "/":
            candidates.append(os.path.normpath(
                "%s:\\%s" % (home[1].upper(), home[3:].replace("/", os.sep))))
        elif home.startswith("/") and os.name == "nt":
            try:
                import subprocess
                converted = subprocess.check_output(
                    ["cygpath", "-w", home],
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                ).decode().strip()
                if converted:
                    candidates.append(converted)
            except Exception:
                pass
        else:
            candidates.append(home)
    return candidates


def ensure_windows_profile_env():
    """Restore USERPROFILE so expanduser("~") works under MSYS ``env -i``."""
    if os.name != "nt":
        return
    if not os.environ.get("USERPROFILE"):
        for candidate in windows_profile_candidates(os.environ):
            if candidate and os.path.isdir(candidate):
                os.environ["USERPROFILE"] = candidate
                drive, tail = os.path.splitdrive(os.path.abspath(candidate))
                if drive:
                    os.environ.setdefault("HOMEDRIVE", drive)
                if tail:
                    os.environ.setdefault("HOMEPATH", tail)
                break
    profile = os.environ.get("USERPROFILE") or ""
    if profile:
        os.environ.setdefault("APPDATA", os.path.join(profile, "AppData", "Roaming"))
        os.environ.setdefault("LOCALAPPDATA", os.path.join(profile, "AppData", "Local"))


ensure_windows_profile_env()

# User paths
HOME_PATH = os.path.join(os.path.expanduser("~"))
USER_PATH = os.path.join(HOME_PATH, ".openshot_qt")
BACKUP_PATH = os.path.join(USER_PATH)
RECOVERY_PATH = os.path.join(USER_PATH, "recovery")
THUMBNAIL_PATH = os.path.join(USER_PATH, "thumbnail")
CACHE_PATH = os.path.join(USER_PATH, "cache")
BLENDER_PATH = os.path.join(USER_PATH, "blender")
TITLE_PATH = os.path.join(USER_PATH, "title")
TRANSITIONS_PATH = os.path.join(USER_PATH, "transitions")
EMOJIS_PATH = os.path.join(USER_PATH, "emojis")
PREVIEW_CACHE_PATH = os.path.join(USER_PATH, "preview-cache")
USER_PROFILES_PATH = os.path.join(USER_PATH, "profiles")
USER_PRESETS_PATH = os.path.join(USER_PATH, "presets")
USER_TITLES_PATH = os.path.join(USER_PATH, "title_templates")
USER_COLORS_PATH = os.path.join(USER_PATH, "colors")
PROTOBUF_DATA_PATH = os.path.join(USER_PATH, "protobuf_data")
YOLO_PATH = os.path.join(USER_PATH, "yolo")
CLIPBOARD_PATH = os.path.join(USER_PATH, "clipboard")
# Updates staging directory (required by auto_updater.py)
UPDATE_PATH = os.path.join(USER_PATH, "updates")
# Project file extensions (canonical: .zvn; .osp / .flow still open for legacy projects)
PROJECT_EXT = ".zvn"
LEGACY_PROJECT_EXTS = (".osp", ".flow")
ALL_PROJECT_EXTS = (PROJECT_EXT,) + LEGACY_PROJECT_EXTS
LEGACY_PROJECT_EXT = LEGACY_PROJECT_EXTS[0]  # .osp — backwards-compat for older code paths
# User files
BACKUP_FILE = os.path.join(BACKUP_PATH, "backup.zvn")
USER_DEFAULT_PROJECT = os.path.join(USER_PATH, "default.zvn")
LEGACY_DEFAULT_PROJECT = USER_DEFAULT_PROJECT.replace(PROJECT_EXT, ".project")
LEGACY_BACKUP_FILE = os.path.join(BACKUP_PATH, "backup.osp")
LEGACY_USER_DEFAULT_PROJECT = os.path.join(USER_PATH, "default.osp")
FLOW_DEFAULT_PROJECT = os.path.join(USER_PATH, "default.flow")
FLOW_BACKUP_FILE = os.path.join(BACKUP_PATH, "backup.flow")

# Back up "default" values for user paths
_path_defaults = {
    k: v for k, v in locals().items()
    if k.endswith("_PATH")
    and v.startswith(USER_PATH)
}

try:
    from PyQt5.QtCore import QSize

    # UI Thumbnail settings
    LIST_ICON_SIZE = QSize(100, 65)
    LIST_GRID_SIZE = LIST_ICON_SIZE + QSize(5, 25)
    TREE_ICON_SIZE = QSize(75, 49)
    EMOJI_ICON_SIZE = QSize(75, 75)
    EMOJI_GRID_SIZE = EMOJI_ICON_SIZE + QSize(5, 25)
except ImportError:
    # Fail gracefully if we're running without PyQt5 (e.g. CI tasks)
    print("Failed to import `PyQt5.QtCore.QSize` (ignoring exception)")

# Maintainer details, for packaging
JT = {"name": "Jonathan Thomas",
      "email": "jonathan@openshot.org",
      "website": "http://openshot.org/developers/jonathan"}

# Desktop launcher ID, for Linux
DESKTOP_ID = "org.zenvi.Zenvi.desktop"

# Blender minimum version required (a string value)
BLENDER_MIN_VERSION = "5.0"

# Data-model debugging enabler
MODEL_TEST = False

# Default/initial logging levels
LOG_LEVEL_FILE = 'INFO'
LOG_LEVEL_CONSOLE = 'INFO'

# Web backend selection, overridable at launch
WEB_BACKEND = 'auto'

# GitHub repository used for releases checks (required by version/auto-updater).
# Must be in the form "owner/repo" for the GitHub API URL formatter.
# Read before BACKEND_URL resolution (which loads .env via api_client).
GITHUB_REPO = os.getenv("ZENVI_GITHUB_REPO", "Zenvi-pro/zenvi-core")


def _resolve_backend_url():
    """Same resolver as classes.api_client.ZenviBackendClient."""
    try:
        from classes.api_client import ZenviBackendClient
        return ZenviBackendClient._get_backend_url()
    except Exception:
        return os.getenv("ZENVI_BACKEND_URL", "https://api.zenvi.pro").rstrip("/")


BACKEND_URL = _resolve_backend_url()

# Sentry.io error & transaction reporting rate (0.0 TO 1.0)
# 0.0 = no error reporting to Sentry
# 0.5 = 1/2 of errors reported to Sentry
# 1.0 = all errors reporting to Sentry
#    ERROR: Exceptions sent to Sentry
#    TRANS: Transactions sent to Sentry
#    STABLE: If this version matches the current version (reported on openshot.org)
#    UNSTABLE: If this version does not match the current version (reported on openshot.org)
#    STABLE_VERSION: This is the current stable release reported by openshot.org
ERROR_REPORT_RATE_STABLE = 0.0
ERROR_REPORT_RATE_UNSTABLE = 0.0
TRANS_REPORT_RATE_STABLE = 0.0
TRANS_REPORT_RATE_UNSTABLE = 0.0
ERROR_REPORT_STABLE_VERSION = None

# Languages
CMDLINE_LANGUAGE = None
CURRENT_LANGUAGE = 'en_US'
SUPPORTED_LANGUAGES = ['en_US']

try:
    from language import openshot_lang
    language_path = ":/locale/"
except ImportError:
    language_path = os.path.join(PATH, 'language')
    print("Compiled translation resources missing!")
    print(f"Loading translations from: {language_path}")

# Compile language list from :/locale resource
try:
    from PyQt5.QtCore import QDir
    langdir = QDir(language_path)
    trpaths = langdir.entryList(
        ['OpenShot_*.qm'],
        QDir.NoDotAndDotDot | QDir.Files,
        sort=QDir.Name)
    for trpath in trpaths:
        # Extract everything between "Openshot_" and ".qm"
        lang=trpath[trpath.find('_')+1:-3]
        SUPPORTED_LANGUAGES.append(lang)
except ImportError:
    # Fail gracefully if we're running without PyQt5 (e.g. CI tasks)
    print("Failed to import `PyQt5.QtCore.QDir` (ignoring exception)")

SETUP = {
    "name": NAME,
    "version": VERSION,
    "author": JT["name"] + " and others",
    "author_email": JT["email"],
    "maintainer": JT["name"],
    "maintainer_email": JT["email"],
    "url": "https://zenvi.pro/",
    "license": "GNU GPL v." + GPL_VERSION,
    "description": DESCRIPTION,
    "long_description": "Create and edit videos and movies\n"
                        " Zenvi is a free, open-source, non-linear video editor. It\n"
                        " can create and edit videos and movies using many popular video, audio, \n"
                        " image formats.  Create videos for YouTube, Flickr, Vimeo, Metacafe, iPod,\n"
                        " Xbox, and many more common formats!\n"
                        ".\n"
                        " Features include:\n"
                        "  * Multiple tracks (layers)\n"
                        "  * Compositing, image overlays, and watermarks\n"
                        "  * Support for image sequences (rotoscoping)\n"
                        "  * Key-frame animation\n  * Video and audio effects (chroma-key)\n"
                        "  * Transitions (lumas and masks)\n"
                        "  * 3D animation (titles and simulations)\n"
                        "  * Upload videos (YouTube and Vimeo supported)",

    # see http://pypi.python.org/pypi?%3Aaction=list_classifiers
    "classifiers": [
                       "Development Status :: 5 - Production/Stable",
                       "Environment :: X11 Applications",
                       "Environment :: X11 Applications :: GTK",
                       "Intended Audience :: End Users/Desktop",
                       "License :: OSI Approved :: GNU General Public License (GPL)",
                       "Operating System :: OS Independent",
                       "Operating System :: POSIX :: Linux",
                       "Programming Language :: Python",
                       "Topic :: Artistic Software",
                       "Topic :: Multimedia :: Video :: Non-Linear Editor", ] +
                   ["Natural Language :: " + language for language in SUPPORTED_LANGUAGES],

    # Automatic launch script creation
    "entry_points": {
        "gui_scripts": [
            "zenvi = openshot_qt.launch:main"
        ]
    }
}

def setup_userdirs():
    """Create user paths if they do not exist (this is where
    temp files are stored... such as cached thumbnails)"""
    for folder in _path_defaults.values():
        if not os.path.exists(os.fsencode(folder)):
            os.makedirs(folder, exist_ok=True)

    # Migrate USER_DEFAULT_PROJECT from former name (.project)
    if all([
        os.path.exists(LEGACY_DEFAULT_PROJECT),
        not os.path.exists(USER_DEFAULT_PROJECT),
    ]):
        print("Migrating default project file to new name")
        os.rename(LEGACY_DEFAULT_PROJECT, USER_DEFAULT_PROJECT)
    # Migrate default.osp to default.zvn
    if all([
        os.path.exists(LEGACY_USER_DEFAULT_PROJECT),
        not os.path.exists(USER_DEFAULT_PROJECT),
    ]):
        print("Migrating default project file from .osp to .zvn")
        os.rename(LEGACY_USER_DEFAULT_PROJECT, USER_DEFAULT_PROJECT)
    # Migrate default.flow to default.zvn (short-lived .flow era)
    if all([
        os.path.exists(FLOW_DEFAULT_PROJECT),
        not os.path.exists(USER_DEFAULT_PROJECT),
    ]):
        print("Migrating default project file from .flow to .zvn")
        os.rename(FLOW_DEFAULT_PROJECT, USER_DEFAULT_PROJECT)
    # Migrate backup.osp to backup.zvn
    if all([
        os.path.exists(LEGACY_BACKUP_FILE),
        not os.path.exists(BACKUP_FILE),
    ]):
        try:
            import shutil
            shutil.copy2(LEGACY_BACKUP_FILE, BACKUP_FILE)
            os.unlink(LEGACY_BACKUP_FILE)
            print("Migrated backup.osp to backup.zvn")
        except OSError:
            pass
    # Migrate backup.flow to backup.zvn
    if all([
        os.path.exists(FLOW_BACKUP_FILE),
        not os.path.exists(BACKUP_FILE),
    ]):
        try:
            import shutil
            shutil.copy2(FLOW_BACKUP_FILE, BACKUP_FILE)
            os.unlink(FLOW_BACKUP_FILE)
            print("Migrated backup.flow to backup.zvn")
        except OSError:
            pass


def reset_userdirs():
    """Reset all info.FOO_PATH attributes back to their initial values,
    as they may have been modified by the runtime code (retargeting
    info.THUMBNAIL_PATH to a project assets directory, for example)"""
    for k, v in _path_defaults.items():
        globals()[k] = v


def get_default_path(varname):
    """Return the default value of the named info.FOO_PATH attribute,
    even if it's been modified"""
    return _path_defaults.get(varname, None)


def website_language():
    """Get the current website language code for URLs"""
    return {
        "zh_CN": "zh-hans/",
        "zh_TW": "zh-hant/",
        "en_US": ""}.get(CURRENT_LANGUAGE,
                         "%s/" % CURRENT_LANGUAGE.split("_")[0].lower())
