"""A force-quit untitled session gets its own draft chat back.

Testing RC v1.2.0 (#216): after a force-quit, relaunching restored the untitled
backup.zvn but showed an empty chat. The chat dock is built while MainWindow is
constructed, before recover_backup runs; building it minted a fresh draft
bucket and saved that key as ``restore_draft_history_key``, overwriting the
crashed session's key. recover_backup then handed the new, empty bucket to
restore_draft. The old conversation was still in chat_history.db.

The key is now recorded only when the untitled backup it belongs to is written.
MainWindow can't be imported headlessly, so its two backup writers are compiled
from main_window.py's source.
"""

import ast
import os
from types import SimpleNamespace

import pytest

from classes import info, session_restore
from windows.ai_chat_ui import AIChatWindow

_MAIN_WINDOW = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "windows", "main_window.py",
)


class _Settings:
    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        self._values[key] = value

    def save(self):
        pass


@pytest.fixture
def store(monkeypatch, tmp_path):
    from classes import chat_history
    chat_history.close()
    monkeypatch.setattr(chat_history, "CHAT_DB_PATH", str(tmp_path / "chat_history.db"))
    yield chat_history
    chat_history.close()


def _remembers_draft_key_in(settings):
    """MainWindow._set_restore_draft_history_key, minus the window."""
    return lambda key: session_restore.set_restore_draft_history_key(settings, key)


def _main_window_method(name, **module_globals):
    with open(_MAIN_WINDOW, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), _MAIN_WINDOW)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
    func = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name]
    ns = dict(module_globals)
    exec(compile(ast.Module(body=func, type_ignores=[]), _MAIN_WINDOW, "exec"), ns)
    return ns[name]


def test_building_the_chat_dock_at_launch_keeps_the_crashed_draft_key(store, monkeypatch):
    # The session that was force-quit: untitled, one conversation, its key saved.
    store.upsert_session("s-crashed", "draft:crashed", title="Trim the intro")
    store.record_message("s-crashed", "user", "trim the intro")
    settings = _Settings({"restore_draft_history_key": "draft:crashed"})

    from classes import app as app_module
    window = SimpleNamespace(_set_restore_draft_history_key=_remembers_draft_key_in(settings))
    monkeypatch.setattr(app_module, "get_app", lambda: SimpleNamespace(window=window))

    # Relaunch: MainWindow builds the dock, which picks this window's bucket...
    dock = SimpleNamespace(_draft_history_key="")
    key = AIChatWindow._resolve_history_key(dock, "")
    assert key.startswith("draft:") and key != "draft:crashed"

    # ...and recover_backup, which runs after, must still find the crashed
    # session's key: it is what restore_draft adopts.
    assert settings.get("restore_draft_history_key") == "draft:crashed"
    [session] = store.load_sessions("draft:crashed")
    messages = store.load_messages(session["session_id"])
    assert [m["content"] for m in messages] == ["trim the intro"]


def _untitled_app(saved):
    project = SimpleNamespace(
        current_filepath=None,
        needs_save=lambda: True,
        save=lambda path, backup_only=False: saved.append((path, backup_only)),
    )
    return SimpleNamespace(project=project, updates=SimpleNamespace(data_version=7))


def _window(settings, draft_key):
    return SimpleNamespace(
        dockAIChat=SimpleNamespace(_draft_history_key=draft_key),
        _set_restore_draft_history_key=_remembers_draft_key_in(settings),
        last_auto_save_data_version=-1,
    )


def test_the_untitled_autosave_records_the_draft_key_with_the_backup(tmp_path):
    saved = []
    settings = _Settings()
    backup = str(tmp_path / "backup.zvn")
    auto_save_project = _main_window_method(
        "auto_save_project",
        get_app=lambda: _untitled_app(saved),
        info=SimpleNamespace(BACKUP_FILE=backup),
        log=SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None),
        os=os,
    )

    auto_save_project(_window(settings, "draft:this-window"))

    assert saved == [(backup, True)]
    assert settings.get("restore_draft_history_key") == "draft:this-window"


def test_the_indexing_flush_records_the_draft_key_with_the_backup(monkeypatch, tmp_path):
    saved = []
    settings = _Settings()
    backup = str(tmp_path / "backup.zvn")
    monkeypatch.setattr(info, "BACKUP_FILE", backup)
    flush_project_to_disk = _main_window_method(
        "flush_project_to_disk",
        get_app=lambda: _untitled_app(saved),
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )

    flush_project_to_disk(_window(settings, "draft:this-window"))

    assert saved == [(backup, True)]
    assert settings.get("restore_draft_history_key") == "draft:this-window"
