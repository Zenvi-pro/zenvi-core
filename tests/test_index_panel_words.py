"""The Index dock lists transcript words, so it must ask get_transcript for them.

get_transcript's default receipt is compact (data.script only, no words), and
the dock showed "0 words" for a transcribed clip.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from windows import index_panel


def _receipt(words):
    return json.dumps({
        "contract": 3,
        "status": "applied",
        "tool": "get_transcript_tool",
        "summary": "ok",
        "data": {
            "transcriptGeneration": 2,
            "transcriptionSource": "local-apple",
            "clips": [{"clipId": "C1", "words": words}],
        },
    })


def test_refresh_requests_words_and_lists_them():
    words = [
        {"text": "Welcome", "startFrame": 3, "timelineStartSec": 0.1},
        {"text": "basically", "startFrame": 160, "timelineStartSec": 5.33},
    ]
    added = []
    panel = SimpleNamespace(
        _app=MagicMock(),
        _list=SimpleNamespace(clear=lambda: added.clear(), addItem=added.append),
        _status=MagicMock(),
        _generation=None,
    )
    with patch("classes.agent_tools.transcript.get_transcript", return_value=_receipt(words)) as get:
        index_panel.IndexPanel.refresh(panel)

    get.assert_called_once()
    assert get.call_args.kwargs.get("includeWords") is True
    assert len(added) == 2
    panel._status.setText.assert_called_with("2 words · local-apple · gen 2")
