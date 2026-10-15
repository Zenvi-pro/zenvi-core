"""The Captions dock, as real Qt widgets over a fake project (run with ZENVI_REAL_QT=1).

Words load when a clip is selected, "Behind the speaker" is offered only where
someone is on camera, and a running job shows its stage with a clock.
"""

import os
import time
import types

import pytest

from _qt_support import skip_without_pyqt5  # noqa: E402

if os.environ.get("ZENVI_REAL_QT") != "1":
    pytest.skip("needs real Qt widgets (ZENVI_REAL_QT=1)", allow_module_level=True)
skip_without_pyqt5()
from PyQt5.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


class _Window(QObject):
    SelectionChanged = pyqtSignal()
    selected_clips = []


@pytest.fixture
def dock(monkeypatch):
    app = QApplication.instance() or QApplication([])
    from classes import query, tool_handlers
    from classes.editor_tools import titles_text_captions as tool
    from windows.views import captions_dock

    window = _Window()
    clips = {cid: types.SimpleNamespace(id=cid, data={"title": cid.lower() + ".mp4"}) for cid in ("A", "B")}
    state = types.SimpleNamespace(app=app, window=window, loads=[], calls=[], tool=tool)

    def words(clip):
        state.loads.append(clip.id)
        time.sleep(0.2)
        return [{"text": "hello"}, {"text": clip.id}]

    def execute(name, args):
        state.calls.append((name, args))
        tool.on_progress("Cutting out the speaker...")
        time.sleep(1.3)
        return "Done."

    monkeypatch.setattr(captions_dock, "get_app", lambda: types.SimpleNamespace(_tr=lambda s: s, window=window))
    monkeypatch.setattr(query.Clip, "get", staticmethod(lambda id=None, **kw: clips.get(id)))
    monkeypatch.setattr(tool, "clip_caption_words", words)
    monkeypatch.setattr(tool, "has_subject", lambda clip: clip.id == "A")      # nobody on camera in B
    monkeypatch.setattr(tool, "on_progress", None)
    monkeypatch.setattr(tool_handlers, "execute_tool", execute)
    state.dock = captions_dock.CaptionsDock()
    return state


def _pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def _select(state, *ids):
    state.window.selected_clips = list(ids)
    state.window.SelectionChanged.emit()


def test_words_load_on_selection_and_follow_the_selection(dock):
    d = dock.dock
    assert not d.generateButton.isEnabled() and d.projectButton.isEnabled()
    _select(dock, "A")
    assert d._busy and "speech" in d.statusLabel.text()
    _select(dock, "B")                                   # moved on while A was still being read
    _pump(dock.app, 1.0)
    assert dock.loads == ["A", "B"] and d.wordsEdit.toPlainText() == "hello B"
    assert not d._busy and d.generateButton.isEnabled()


def test_behind_the_speaker_is_offered_only_where_someone_is_on_camera(dock):
    d = dock.dock
    d.behindSubject.setChecked(True)
    _select(dock, "B")
    _pump(dock.app, 0.6)
    assert not d.behindSubject.isEnabled()
    d.generateButton.click()
    _pump(dock.app, 1.6)
    assert dock.calls[-1][1]["behind_subject"] is False          # not for this clip
    d.projectButton.click()
    _pump(dock.app, 1.6)
    assert dock.calls[-1][1] == {"whole_project": True, "style": "pill-karaoke", "behind_subject": True}
    _select(dock, "A")
    _pump(dock.app, 0.6)
    assert d.behindSubject.isEnabled()


def test_a_running_job_shows_its_stage_and_a_clock(dock):
    d = dock.dock
    d.projectButton.click()
    _pump(dock.app, 1.15)
    text = d.statusLabel.text()
    assert text.startswith("Cutting out the speaker...") and text.endswith("0:01"), text
    assert not d.projectButton.isEnabled()
    _pump(dock.app, 0.6)
    assert d.statusLabel.text() == "Done." and d.projectButton.isEnabled()


@pytest.fixture
def recognisers(dock, monkeypatch):
    from classes import hyperframes_local as hf
    from windows.views import captions_dock

    state = types.SimpleNamespace(chosen="whistle", installed={"whistle": True, "parakeet": False}, supported=True,
                                  asked=[], answer=False, installs=[], fail=False)

    def install(name):
        def run():
            time.sleep(0.2)
            if state.fail:
                raise hf.HyperframesError("no network")
            state.installs.append(name)
            state.installed[name] = True
        return run

    monkeypatch.setattr(hf, "recogniser", lambda: state.chosen if state.supported else "parakeet")
    monkeypatch.setattr(hf, "choose_recogniser", lambda name: setattr(state, "chosen", name))
    monkeypatch.setattr(hf, "speech_supported", lambda *a: state.supported)
    monkeypatch.setattr(hf, "speech_ready", lambda fresh=False: state.installed["whistle"])
    monkeypatch.setattr(hf, "status", lambda: {"ready": True, "speech": state.installed["whistle"],
                                               "parakeet": state.installed["parakeet"]})
    monkeypatch.setattr(hf, "install_speech", install("whistle"))
    monkeypatch.setattr(hf, "install_parakeet", install("parakeet"))
    monkeypatch.setattr(captions_dock, "_confirm_download",
                        lambda parent, label: state.asked.append(label) or state.answer)
    state.dock = captions_dock.CaptionsDock()
    state.app = dock.app
    return state


def _pick(state, name):
    """What a user's click does: select the entry and report it as activated (even the current one)."""
    combo = state.dock.recogniserCombo
    combo.setCurrentIndex(combo.findData(name))
    combo.activated.emit(combo.currentIndex())


def test_the_recogniser_picker_shows_what_is_installed_and_installs_on_request(recognisers):
    s = recognisers
    combo = s.dock.recogniserCombo
    assert not combo.isHidden() and not s.dock.recogniserLabel.isHidden()
    assert combo.currentData() == "whistle"
    assert "installed" in combo.itemText(combo.findData("whistle")).lower()
    assert "900 mb" in combo.itemText(combo.findData("parakeet")).lower()            # a download, with its size

    _pick(s, "parakeet")                                       # not installed: asked first; declining changes nothing
    _pump(s.app, 0.3)
    assert s.asked and s.installs == [] and combo.currentData() == "whistle" and s.chosen == "whistle"

    s.answer = True
    _pick(s, "parakeet")
    assert s.dock._busy and "parakeet" in s.dock.statusLabel.text().lower()
    _pump(s.app, 0.8)
    assert s.installs == ["parakeet"] and s.chosen == "parakeet" and not s.dock._busy
    assert "installed" in combo.itemText(combo.findData("parakeet")).lower()

    _pick(s, "whistle")                                        # installed: switching is immediate, nothing asked
    _pump(s.app, 0.2)
    assert s.chosen == "whistle" and len(s.asked) == 2 and s.installs == ["parakeet"]

    # The chosen recogniser's files were deleted: it shows as a download, and picking it again fetches it.
    s.installed["whistle"] = False
    s.dock._fill_recognisers()
    assert "18 mb" in combo.itemText(combo.findData("whistle")).lower()
    _pick(s, "whistle")
    _pump(s.app, 0.8)
    assert s.installs == ["parakeet", "whistle"] and s.chosen == "whistle"


def test_the_download_question_already_counts_as_busy(recognisers):
    s = recognisers

    def ask(parent, label):
        s.busy_while_asking = s.dock._busy and not s.dock.generateButton.isEnabled()
        return False

    from windows.views import captions_dock
    captions_dock._confirm_download = ask
    _pick(s, "parakeet")
    assert s.busy_while_asking and not s.dock._busy


def test_a_failed_install_keeps_the_previous_recogniser(recognisers):
    s = recognisers
    s.answer, s.fail = True, True
    _pick(s, "parakeet")
    _pump(s.app, 0.8)
    assert s.chosen == "whistle" and s.dock.recogniserCombo.currentData() == "whistle"
    assert "no network" in s.dock.statusLabel.text()


def test_the_cut_out_picker_shows_only_for_behind_the_speaker(recognisers, monkeypatch):
    from classes import hyperframes_local as hf

    s = recognisers
    matte = {"chosen": "rvm", "installed": {"rvm": False, "u2net": True}}
    monkeypatch.setattr(hf, "matting", lambda: matte["chosen"])
    monkeypatch.setattr(hf, "choose_matting", lambda name: matte.update(chosen=name))
    monkeypatch.setattr(hf, "install_rvm", lambda: time.sleep(0.2) or matte["installed"].update(rvm=True))
    monkeypatch.setattr(hf, "status", lambda: {"ready": True, "speech": True, "parakeet": False,
                                               "recogniser": "whistle", "matting": matte["chosen"],
                                               "rvm": matte["installed"]["rvm"], "u2net": matte["installed"]["u2net"]})
    d = s.dock
    d._fill_picker("matting")
    combo = d.mattingCombo
    d.behindSubject.setChecked(False)
    assert combo.isHidden() and d.mattingLabel.isHidden()
    d.behindSubject.setChecked(True)
    assert not combo.isHidden() and not d.mattingLabel.isHidden()
    assert combo.currentData() == "rvm" and "download" in combo.itemText(combo.findData("rvm")).lower()
    assert "installed" in combo.itemText(combo.findData("u2net")).lower()

    combo.setCurrentIndex(combo.findData("u2net"))              # installed: switches at once
    combo.activated.emit(combo.currentIndex())
    assert matte["chosen"] == "u2net" and s.asked == []

    s.answer = True                                              # RVM: asked, downloaded, then used
    combo.setCurrentIndex(combo.findData("rvm"))
    combo.activated.emit(combo.currentIndex())
    _pump(s.app, 0.8)
    assert s.asked and matte["chosen"] == "rvm" and matte["installed"]["rvm"] is True
    assert "installed" in combo.itemText(combo.findData("rvm")).lower()


def test_an_intel_mac_has_no_choice_to_make(dock, monkeypatch):
    from classes import hyperframes_local as hf
    from windows.views import captions_dock

    monkeypatch.setattr(hf, "speech_supported", lambda *a: False)
    monkeypatch.setattr(hf, "status", lambda: {"ready": True, "speech": False, "parakeet": False})
    d = captions_dock.CaptionsDock()
    assert d.recogniserCombo.isHidden() and d.recogniserLabel.isHidden()
