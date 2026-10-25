"""Find stock footage from any agent.

The in-app Assistant has a Pexels search of its own (it runs in the backend), but Claude Code and every other MCP client only get the editor's
tools, so "use stock footage for what is missing" had no way to find a clip: `import_stock_media_tool` wants the MP4 link of a result nobody could
search for. This tool searches through the same backend route (the Pexels key stays on the server) and remembers each result's link, so
`import_stock_media_tool(source="pexels", video_id=...)` works with just the id from the search.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from classes.editor_tools._base import ToolError, enum, integer, number, obj, ok, string
from classes.editor_tools._registry import editor_tool
from classes.logger import log

MAX_REMEMBERED = 200
_links: Dict[str, str] = {}


def remember(video_id: Any, link: str) -> None:
    _links[str(video_id)] = link
    while len(_links) > MAX_REMEMBERED:
        _links.pop(next(iter(_links)))


def remembered_link(video_id: Any) -> str:
    """The MP4 link a search returned for this Pexels id ('' when it was not searched in this session)."""
    return _links.get(str(video_id), "")


def shape_of(width: int, height: int) -> str:
    if not width or not height:
        return "unknown"
    ratio = width / height
    return "landscape" if ratio > 1.15 else ("portrait" if ratio < 0.87 else "square")


def best_file(video: Dict[str, Any], prefer_height: int = 1080) -> Dict[str, Any]:
    """The MP4 to download: the smallest file at least *prefer_height* tall (a 4K file is slow to fetch and to edit), else the largest there is."""
    files = [f for f in (video.get("video_files") or []) if f.get("link") and "mp4" in str(f.get("file_type") or "mp4")]
    if not files:
        return {}
    tall_enough = sorted((f for f in files if int(f.get("height") or 0) >= prefer_height), key=lambda f: int(f.get("height") or 0))
    return tall_enough[0] if tall_enough else max(files, key=lambda f: int(f.get("height") or 0))


def rows_from(videos: List[Dict[str, Any]], min_seconds: float, orientation: str, count: int) -> List[Dict[str, Any]]:
    rows = []
    for v in videos:
        duration = float(v.get("duration") or 0)
        shape = shape_of(int(v.get("width") or 0), int(v.get("height") or 0))
        chosen = best_file(v)
        if duration < float(min_seconds) or (orientation != "any" and shape != orientation) or not chosen:
            continue
        rows.append({"video_id": v.get("id"), "seconds": round(duration, 1), "shape": shape, "width": chosen.get("width"), "height": chosen.get("height"),
                     "photographer": v.get("user_name") or "", "link": chosen.get("link"), "preview": v.get("image") or "", "page": v.get("url") or ""})
        if len(rows) >= count:
            break
    return rows


@editor_tool(
    "search_stock_video_tool",
    label="Search stock video",
    covers=("ai.stock_media",),
    schema=obj({
        "query": string("What to look for, in plain words ('red fish swimming on a dark background', 'person walking in a park').", ""),
        "count": integer("How many results to return (1 to 15).", 8, minimum=1, maximum=15),
        "min_seconds": number("Only clips at least this long. Use the length of the slot you are filling.", 0.0, minimum=0),
        "orientation": enum(["any", "landscape", "portrait", "square"], "Only clips of this shape.", "any"),
    }, required=["query"]),
    read_only=True,
)
def search_stock_video(query, count=8, min_seconds=0.0, orientation="any"):
    """Search Pexels stock videos (free to use) and list matches with their length, size, photographer and a preview picture. Then bring one
    into the project with import_stock_media_tool(source="pexels", video_id=<video_id>): the link is remembered from this search. A download
    uses stock credits where the account is metered. Use it for shots your own footage cannot cover.
    """
    text = str(query or "").strip()
    if not text:
        raise ToolError("say what to look for, for example query='red fish swimming on a dark background'")
    from classes.api_client import get_backend_client
    count = int(count)
    data = get_backend_client().pexels_search(text, per_page=min(80, max(count * 3, 15)))
    if data.get("error"):
        raise ToolError(f"stock search is not available right now: {data['error']}")
    rows = rows_from(data.get("videos") or [], float(min_seconds), str(orientation), count)
    for row in rows:
        remember(row["video_id"], row["link"])
    if not rows:
        return ok(f"no stock clips for {text!r}" + (f" at least {float(min_seconds):g} s long" if min_seconds else "") + "; try simpler words or a shorter minimum.",
                  results=[], total=int(data.get("total_results") or 0))
    log.info("stock search %r: %d result(s)", text, len(rows))
    return ok(f"{len(rows)} stock clip(s) for {text!r} (of {int(data.get('total_results') or 0):,}); import one with "
              "run_tool(name='import_stock_media_tool', args={'source': 'pexels', 'video_id': <video_id>}).", results=rows, total=int(data.get("total_results") or 0))


def search_stock_for(query: str, min_seconds: float = 0.0, count: int = 3) -> Optional[List[Dict[str, Any]]]:
    """For other tools (the recreation planner): the best few stock matches for a phrase, or None when search is unavailable."""
    try:
        from classes.api_client import get_backend_client
        data = get_backend_client().pexels_search(str(query), per_page=max(count * 3, 15))
    except Exception:
        log.debug("stock search unavailable", exc_info=True)
        return None
    if data.get("error"):
        return None
    rows = rows_from(data.get("videos") or [], float(min_seconds), "any", count)
    for row in rows:
        remember(row["video_id"], row["link"])
    return rows
