"""Rich tool results: contract-3 receipt plus optional image blocks for MCP/WS."""

from __future__ import annotations

import base64
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from classes.agent_tools.receipt import ToolReceipt

MAX_IMAGE_BYTES = int(2.5 * 1024 * 1024)
_tls = threading.local()


@dataclass
class ImageBlock:
    data: bytes
    mime_type: str = "image/jpeg"

    def to_ws(self) -> dict:
        return {
            "mimeType": self.mime_type,
            "data": base64.b64encode(self.data).decode("ascii"),
        }


@dataclass
class ToolOutput:
    receipt: ToolReceipt
    images: list[ImageBlock] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.receipt.status in ("error", "refused") and self.images:
            self.images = []


def clear_last_output() -> None:
    _tls.last = None


def set_last_output(output: ToolOutput) -> None:
    _tls.last = output


def get_last_output() -> Optional[ToolOutput]:
    return getattr(_tls, "last", None)


def assert_budget(images: list[ImageBlock], *, limit: int = MAX_IMAGE_BYTES) -> Optional[str]:
    total = sum(len(b.data) for b in images)
    if total > limit:
        return f"Error: inspect image budget exceeded ({total} bytes > {limit} bytes)."
    return None


def trim_to_budget(
    images: list[ImageBlock], *, limit: int = MAX_IMAGE_BYTES,
) -> tuple[list[ImageBlock], list[str]]:
    kept: list[ImageBlock] = []
    warnings: list[str] = []
    total = 0
    for i, block in enumerate(images):
        if total + len(block.data) > limit:
            warnings.append(
                f"Stopped early at image {i}/{len(images)} to stay under {limit} byte budget."
            )
            break
        kept.append(block)
        total += len(block.data)
    return kept, warnings


def mcp_content(output: ToolOutput) -> list[Any]:
    import mcp.types as types

    blocks: list[Any] = [
        types.TextContent(type="text", text=output.receipt.to_json()),
    ]
    for img in output.images:
        blocks.append(
            types.ImageContent(
                type="image",
                data=base64.b64encode(img.data).decode("ascii"),
                mimeType=img.mime_type,
            )
        )
    return blocks


def ws_images(output: ToolOutput) -> list[dict]:
    return [b.to_ws() for b in output.images]


def wrap_str_result(tool: str, text: str) -> ToolOutput:
    from classes.agent_tools.receipt import from_handler_str, parse_receipt

    parsed = parse_receipt(text)
    if parsed is None:
        return ToolOutput(receipt=from_handler_str(tool, text or ""))
    receipt = ToolReceipt(
        status=parsed.get("status") or "applied",
        tool=parsed.get("tool") or tool,
        summary=parsed.get("summary") or (text or ""),
        contract=int(parsed.get("contract") or 3),
        clips=list(parsed.get("clips") or []),
        clipsNote=parsed.get("clipsNote"),
        shifted=list(parsed.get("shifted") or []),
        removedClipIds=list(parsed.get("removedClipIds") or []),
        createdTracks=list(parsed.get("createdTracks") or []),
        markers=list(parsed.get("markers") or []),
        removedMarkerIds=list(parsed.get("removedMarkerIds") or []),
        warnings=list(parsed.get("warnings") or []),
        notes=list(parsed.get("notes") or []),
        watchSuggested=parsed.get("watchSuggested"),
        undoSteps=int(parsed.get("undoSteps") or 0),
        data=parsed.get("data"),
    )
    return ToolOutput(receipt=receipt, images=[])
