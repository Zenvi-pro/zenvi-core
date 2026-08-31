"""A single dead Freesound preview URL must not fail the whole stock_music run.

Reported: "Error in stock_music at step 'import': Freesound download error:
404 ... 749923_14640845-hq.mp3", retried, failed the same way.
"""

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_qt = MagicMock()
_qt.QObject = object
_qt.pyqtSignal = lambda *a, **k: MagicMock()
sys.modules.setdefault("PyQt5.QtCore", _qt)
sys.modules.setdefault("PyQt5.QtWidgets", MagicMock(QApplication=MagicMock))

import pytest  # noqa: E402

from classes.api_client import ZenviBackendClient  # noqa: E402

HQ = "https://cdn.freesound.org/previews/749/749923_14640845-hq.mp3"


def _client(ok_urls):
    c = ZenviBackendClient.__new__(ZenviBackendClient)
    tried = []

    def _dl(url, ext, filename_hint="", timeout=0):
        tried.append(url)
        if url in ok_urls:
            return {"success": True, "path": f"/tmp/x{ext}"}
        return {"success": False, "error": "404 Client Error: Not Found"}

    c._download_url_to_temp = _dl
    return c, tried


def test_the_happy_path_uses_the_given_url_only():
    c, tried = _client({HQ})
    out = c.freesound_download(749923, HQ)
    assert out["success"] is True
    assert tried == [HQ]


def test_a_404_on_hq_falls_back_to_lq():
    lq = HQ.replace("-hq.mp3", "-lq.mp3")
    c, tried = _client({lq})
    out = c.freesound_download(749923, HQ)
    assert out["success"] is True
    assert tried[0] == HQ
    assert lq in tried


def test_it_falls_back_to_ogg_when_both_mp3s_are_gone():
    ogg = HQ.replace("-hq.mp3", "-hq.ogg")
    c, tried = _client({ogg})
    assert c.freesound_download(749923, HQ)["success"] is True
    assert ogg in tried


def test_an_lq_url_falls_back_to_hq():
    lq = HQ.replace("-hq.mp3", "-lq.mp3")
    c, tried = _client({HQ})
    assert c.freesound_download(749923, lq)["success"] is True
    assert HQ in tried


def test_every_variant_dead_reports_the_error_not_a_crash():
    c, tried = _client(set())
    out = c.freesound_download(749923, HQ)
    assert out["success"] is False
    assert "404" in out["error"]
    assert len(tried) >= 3, "must actually try the alternatives"


def test_a_blank_url_is_rejected_without_a_request():
    c, tried = _client(set())
    assert c.freesound_download(749923, "")["success"] is False
    assert tried == []


def test_no_duplicate_requests_for_the_same_url():
    c, tried = _client(set())
    c.freesound_download(749923, HQ)
    assert len(tried) == len(set(tried))


def test_the_extension_matches_the_chosen_variant():
    ogg = HQ.replace("-hq.mp3", "-hq.ogg")
    c, _tried = _client({ogg})
    assert c.freesound_download(749923, HQ)["path"].endswith(".ogg")
