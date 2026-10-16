"""Stock footage any agent can find: search through the backend, remember each result's link, import by id.

Found live: Claude Code was told to use stock for gaps, but `import_stock_media_tool` wanted an MP4 link and the only search tool
(`search_pexels_videos_tool`) lives in the backend, not in the editor's tool list, so no stock clip could ever be found.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

import editor_tools_harness  # noqa: E402,F401  (stubs Qt and libopenshot)
from classes.editor_tools import REGISTRY, ai_generation_stock as stock  # noqa: E402


def video(vid, seconds, width, height, files=((1920, 1080), (3840, 2160), (1280, 720))):
    return {"id": vid, "duration": seconds, "width": width, "height": height, "user_name": "Ann", "image": f"https://img/{vid}.jpg", "url": f"https://pexels/{vid}",
            "video_files": [{"link": f"https://cdn/{vid}_{w}.mp4", "width": w, "height": h, "file_type": "video/mp4", "quality": "hd"} for w, h in files]}


class FakeClient:
    def __init__(self, videos=None, error=None):
        self.videos, self.error, self.asked = videos or [], error, []

    def pexels_search(self, query, per_page=15, page=1):
        self.asked.append((query, per_page))
        return {"error": self.error} if self.error else {"videos": self.videos, "total_results": 1234}


@pytest.fixture(autouse=True)
def clean_memory():
    stock._links.clear()
    yield
    stock._links.clear()


def search(monkeypatch, client, **args):
    monkeypatch.setattr("classes.api_client.get_backend_client", lambda: client)
    out = REGISTRY["search_stock_video_tool"].func(**args)
    head, _, rest = out.partition("\n")
    return head, (json.loads(rest) if rest else {})


def test_a_search_lists_clips_with_the_link_to_a_hd_file_not_the_4k_one(monkeypatch):
    head, data = search(monkeypatch, FakeClient([video(1, 8, 1920, 1080), video(2, 3, 1080, 1920)]), query="red fish")
    assert [r["video_id"] for r in data["results"]] == [1, 2]
    assert data["results"][0]["link"] == "https://cdn/1_1920.mp4" and data["results"][0]["shape"] == "landscape"
    assert "import_stock_media_tool" in head and data["results"][0]["preview"] == "https://img/1.jpg"


def test_minimum_length_and_shape_filter_the_results(monkeypatch):
    clips = [video(1, 2, 1920, 1080), video(2, 9, 1920, 1080), video(3, 9, 1080, 1920)]
    _, data = search(monkeypatch, FakeClient(clips), query="x", min_seconds=5, orientation="landscape")
    assert [r["video_id"] for r in data["results"]] == [2]


def test_results_are_remembered_so_the_id_alone_imports_them(monkeypatch):
    search(monkeypatch, FakeClient([video(7, 6, 1920, 1080)]), query="x")
    assert stock.remembered_link(7) == "https://cdn/7_1920.mp4" and stock.remembered_link(99) == ""


def test_the_import_finds_the_link_of_a_clip_found_earlier_and_explains_when_it_cannot(monkeypatch):
    from classes import tool_handlers
    monkeypatch.setattr("classes.credits_client.check_operation", lambda *a, **k: (False, 0, "blocked here"))
    unknown = tool_handlers.download_pexels_video_tool(video_id="55")
    assert "search_stock_video_tool" in unknown and unknown.startswith("Error")
    stock.remember(55, "https://cdn/55.mp4")
    assert "blocked here" in tool_handlers.download_pexels_video_tool(video_id="55")       # got past the link step


def test_a_search_failure_and_an_empty_query_are_errors_that_say_why(monkeypatch):
    monkeypatch.setattr("classes.api_client.get_backend_client", lambda: FakeClient(error="PEXELS_API_KEY is not configured"))
    out = REGISTRY["search_stock_video_tool"].func(query="x")
    assert out.startswith("Error") and "PEXELS_API_KEY" in out
    assert REGISTRY["search_stock_video_tool"].func(query="  ").startswith("Error")


def test_nothing_found_is_said_plainly_and_is_not_an_error(monkeypatch):
    head, data = search(monkeypatch, FakeClient([]), query="zzz")
    assert "no stock clips" in head and data["results"] == []


def test_the_planner_helper_returns_none_when_search_is_unavailable(monkeypatch):
    monkeypatch.setattr("classes.api_client.get_backend_client", lambda: FakeClient(error="offline"))
    assert stock.search_stock_for("fish") is None
    monkeypatch.setattr("classes.api_client.get_backend_client", lambda: FakeClient([video(4, 5, 1920, 1080)]))
    assert [r["video_id"] for r in stock.search_stock_for("fish")] == [4]
