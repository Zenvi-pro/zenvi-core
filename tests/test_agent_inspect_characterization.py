"""Phase 4 characterization: document today's vision gaps before they flip.

Defects A–C start as strict xfails and flip as Steps 4.2 / 4.6–4.8 land.
Defect D (_content_to_text drops images) must stay green forever.
"""

from __future__ import annotations

import asyncio
import base64
import sys
import time
import types

import pytest


def test_content_to_text_drops_image_blocks():
    from windows.agent_runners import _content_to_text

    mixed = [
        {"type": "text", "text": '{"contract":3,"status":"applied"}'},
        {
            "type": "image",
            "mimeType": "image/jpeg",
            "data": base64.b64encode(b"\xff\xd8fake").decode("ascii"),
        },
        {"type": "text", "text": "note"},
    ]
    text = _content_to_text(mixed)
    assert text == '{"contract":3,"status":"applied"}\nnote'
    assert "image" not in text.lower()
    assert "ffd8" not in text.lower()


def test_inspect_tools_registered():
    from classes.tool_handlers import (
        AGENT_TOOL_HANDLERS,
        BACKGROUND_SAFE_TOOLS,
        READ_ONLY_TOOLS,
    )

    assert "inspect_timeline_tool" in AGENT_TOOL_HANDLERS
    assert "inspect_media_tool" in AGENT_TOOL_HANDLERS
    assert "inspect_timeline_tool" in READ_ONLY_TOOLS
    assert "inspect_media_tool" in READ_ONLY_TOOLS
    assert "inspect_timeline_tool" in BACKGROUND_SAFE_TOOLS
    assert "inspect_media_tool" in BACKGROUND_SAFE_TOOLS


def test_watch_clip_window_has_no_inspect_image_shape():
    from classes.tool_handlers import watch_clip_window

    # Doc / return contract: prose only — no MIME, no data.frames.
    doc = (watch_clip_window.__doc__ or "").lower()
    assert "jpeg" not in doc
    assert "image/jpeg" not in doc
    assert "data.frames" not in doc


def test_server_instructions_still_point_at_watch_for_vision():
    """Flipped in Step 4.8 — kept as inverse assert that inspect is preferred."""
    from classes.agent_mcp_server import SERVER_INSTRUCTIONS

    text = SERVER_INSTRUCTIONS
    assert "inspect_timeline_tool" in text
    assert "Do not use watch_clip_window_tool for verification" in text


def test_mcp_call_tool_emits_image_content_when_rich(monkeypatch):
    pytest.importorskip("mcp")
    import mcp.types as mcp_types
    from classes.agent_mcp_server import ZenviMcpServer
    from classes.agent_tools.output import ImageBlock, ToolOutput
    from classes.agent_tools.receipt import ToolReceipt

    th = types.ModuleType("classes.tool_handlers")
    th.AGENT_TOOL_HANDLERS = {
        "list_files_tool": lambda **_k: "files",
    }
    th.humanize_tool_name = lambda n: n

    jpeg = b"\xff\xd8\xff" + b"x" * 32

    def execute_tool_rich(name, args):
        return ToolOutput(
            receipt=ToolReceipt.applied("list_files_tool", "ok"),
            images=[ImageBlock(data=jpeg)],
        )

    th.execute_tool = lambda name, args: execute_tool_rich(name, args).receipt.to_json()
    th.execute_tool_rich = execute_tool_rich

    saved = sys.modules.get("classes.tool_handlers")
    sys.modules["classes.tool_handlers"] = th
    try:
        srv = ZenviMcpServer().start()
        time.sleep(0.8)
        try:
            async def run():
                import httpx
                from mcp import ClientSession
                from mcp.client.streamable_http import streamable_http_client

                headers = {"Authorization": "Bearer %s" % srv.token}
                async with httpx.AsyncClient(headers=headers) as http_client:
                    async with streamable_http_client(
                        srv.url(), http_client=http_client
                    ) as (r, w, _):
                        async with ClientSession(r, w) as session:
                            await session.initialize()
                            result = await session.call_tool("list_files_tool", {})
                            return result.content

            content = asyncio.run(run())
        finally:
            srv.stop()
    finally:
        if saved is not None:
            sys.modules["classes.tool_handlers"] = saved
        else:
            sys.modules.pop("classes.tool_handlers", None)

    assert len(content) >= 2
    assert isinstance(content[0], mcp_types.TextContent)
    assert any(getattr(c, "type", None) == "image" for c in content)


def test_server_instructions_will_prefer_inspect():
    from classes.agent_mcp_server import SERVER_INSTRUCTIONS

    text = SERVER_INSTRUCTIONS
    assert "inspect_timeline_tool" in text
    assert "watchSuggested" in text
    lowered = text.lower()
    assert "do not use watch_clip_window_tool for verification" in lowered
