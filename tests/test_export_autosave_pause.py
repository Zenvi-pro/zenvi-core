"""Export dialog pauses autosave at open and copies a stripped project."""

from __future__ import annotations

from pathlib import Path

from classes.updates import WAVEFORM_CACHED_MARKER, strip_bulk_waveforms


EXPORT_PY = Path(__file__).resolve().parents[1] / "src" / "windows" / "export.py"


def test_export_init_pauses_autosave_and_uses_stripped_copy():
    src = EXPORT_PY.read_text(encoding="utf-8")
    init = src.split("def __init__", 1)[1].split("\n    def ", 1)[0]
    assert "pause_window_auto_save()" in init
    assert "export_project_copy" in init
    assert "copy.deepcopy(get_app().project)" not in init


def test_export_project_copy_helper_strips_waveforms_without_mutating_live():
    class _Project:
        def __init__(self):
            self._data = {
                "clips": [{
                    "id": "C1",
                    "ui": {"audio_data": [0.01] * 40000, "audio_data_rms": [0.02] * 40000},
                }]
            }
            self.current_filepath = "edit.zvn"

    live = _Project()
    cloned = type("Clone", (), {})()
    cloned._data = strip_bulk_waveforms(live._data)
    assert cloned._data["clips"][0]["ui"]["audio_data"] == WAVEFORM_CACHED_MARKER
    assert len(live._data["clips"][0]["ui"]["audio_data"]) == 40000
