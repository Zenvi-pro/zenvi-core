"""ToolOutput / MCP / WS image transport (Phase 4 Step 4.2)."""

from __future__ import annotations

import asyncio
import base64
import sys
import time
import types

import pytest

from classes.agent_tools.output import (
    MAX_IMAGE_BYTES,
    ImageBlock,
    ToolOutput,
    assert_budget,
    mcp_content,
    set_last_output,
    trim_to_budget,
    ws_images,
)
from classes.agent_tools.receipt import ToolReceipt


def test_mcp_content_receipt_first_then_images():
    jpeg = b"\xff\xd8\xff" + b"a" * 40
    receipt = ToolReceipt.applied("inspect_timeline_tool", "Rendered 1 frame")
    out = ToolOutput(receipt=receipt, images=[ImageBlock(data=jpeg)])
    blocks = mcp_content(out)
    assert blocks[0].type == "text"
    assert blocks[0].text == receipt.to_json()
    assert blocks[1].type == "image"
    assert blocks[1].mimeType == "image/jpeg"
    assert base64.b64decode(blocks[1].data) == jpeg


def test_error_and_refused_drop_images():
    jpeg = b"\xff\xd8" + b"x" * 20
    for status_factory in (
        lambda: ToolReceipt.error("t", "Error: boom"),
        lambda: ToolReceipt.refused("t", "Error: no"),
    ):
        out = ToolOutput(receipt=status_factory(), images=[ImageBlock(data=jpeg)])
        assert out.images == []
        assert len(mcp_content(out)) == 1


def test_assert_budget_and_trim():
    big = ImageBlock(data=b"x" * (MAX_IMAGE_BYTES // 2 + 10))
    assert assert_budget([big, big]) is not None
    kept, warnings = trim_to_budget([big, big])
    assert len(kept) == 1
    assert warnings


def test_ws_images_shape():
    jpeg = b"\xff\xd8" + b"z" * 8
    out = ToolOutput(
        receipt=ToolReceipt.applied("t", "ok"),
        images=[ImageBlock(data=jpeg, mime_type="image/jpeg")],
    )
    side = ws_images(out)
    assert side == [{
        "mimeType": "image/jpeg",
        "data": base64.b64encode(jpeg).decode("ascii"),
    }]


def test_execute_tool_string_matches_rich_receipt(monkeypatch):
    from classes.agent_tools import execute as ex
    from classes.agent_tools.output import ToolOutput

    def handler(**_kw):
        return ToolOutput(
            receipt=ToolReceipt.applied("list_files_tool", "ok"),
            images=[ImageBlock(data=b"\xff\xd8xx")],
        )

    ex.bind_runtime(
        handlers={"list_files_tool": handler},
        read_only=frozenset({"list_files_tool"}),
        background_safe=frozenset({"list_files_tool"}),
        ungrouped=frozenset({"list_files_tool"}),
        main_thread_timeouts={},
        get_app=lambda: types.SimpleNamespace(
            updates=types.SimpleNamespace(actionHistory=[]),
            project={},
            thread=lambda: None,
        ),
        run_on_main_thread=lambda fn, timeout=30: fn(),
        atomic=lambda app, fn: fn,
        coerce_steps=lambda x: 1,
        qthread=None,
    )
    rich = ex.execute_tool_rich("list_files_tool", {})
    text = ex.execute_tool("list_files_tool", {})
    assert text == rich.receipt.to_json()
    assert len(rich.images) == 1


def test_mcp_live_call_returns_image_blocks():
    pytest.importorskip("mcp")
    from classes.agent_mcp_server import ZenviMcpServer

    jpeg = b"\xff\xd8\xff" + b"y" * 24

    th = types.ModuleType("classes.tool_handlers")
    th.AGENT_TOOL_HANDLERS = {
        "list_files_tool": lambda **_k: "files",
    }
    th.humanize_tool_name = lambda n: n

    def execute_tool_rich(name, args):
        return ToolOutput(
            receipt=ToolReceipt.applied(name, "ok"),
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

    assert content[0].type == "text"
    assert '"contract": 3' in content[0].text or '"contract":3' in content[0].text.replace(" ", "")
    assert any(getattr(c, "type", None) == "image" for c in content)
    img = next(c for c in content if getattr(c, "type", None) == "image")
    assert base64.b64decode(img.data) == jpeg
