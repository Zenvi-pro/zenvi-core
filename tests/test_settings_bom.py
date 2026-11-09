"""A UTF-8 BOM on settings JSON must not wipe restore_project_path."""

from __future__ import annotations

import json
from pathlib import Path

LAUNCH_PY = Path(__file__).resolve().parents[1] / "src" / "launch.py"
QT_API_PY = Path(__file__).resolve().parents[1] / "src" / "qt_api.py"


def test_settings_readers_use_utf8_sig():
    launch = LAUNCH_PY.read_text(encoding="utf-8")
    qt_api = QT_API_PY.read_text(encoding="utf-8")
    reader = qt_api.split("def read_file_text", 1)[1].split("\ndef ", 1)[0]
    assert 'encoding="utf-8-sig"' in launch
    assert 'encoding="utf-8-sig"' in reader


def test_utf8_sig_parses_settings_with_bom(tmp_path):
    path = tmp_path / "openshot.settings"
    payload = [{"setting": "restore_project_path", "value": "C:/edit.zvn"}]
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    assert data[0]["value"] == "C:/edit.zvn"
