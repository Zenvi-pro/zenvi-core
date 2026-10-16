#!/usr/bin/env python3
"""Totals for one Claude Code session log: Zenvi tool calls, images it was shown, and model tokens.

    python scripts/claude_session_stats.py ~/.claude/projects/<project>/<session>.jsonl

Used to compare how an agent reaches an answer (frames only, vs the media index) with the same numbers each time.
"""

import json
import sys
from collections import Counter


def stats(path):
    calls, images, results = Counter(), 0, 0
    tokens = Counter()
    seen = set()
    for line in open(path, encoding="utf-8"):
        row = json.loads(line)
        message = row.get("message") or {}
        content = message.get("content")
        if row.get("type") == "assistant":
            usage, mid = message.get("usage"), message.get("id")
            if usage and mid not in seen:
                seen.add(mid)
                for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                    tokens[key] += usage.get(key, 0)
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_use" and "zenvi" in block["name"]:
                    calls[block["name"].split("__")[-1]] += 1
        elif row.get("type") == "user" and isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                    results += 1
                    images += sum(1 for x in block["content"] if x.get("type") == "image")
    return {"zenvi_calls": sum(calls.values()), "by_tool": dict(calls), "images_in_tool_results": images, "tokens": dict(tokens)}


if __name__ == "__main__":
    print(json.dumps(stats(sys.argv[1]), indent=2, ensure_ascii=False))
