"""Error and refusal text names only tools the model can call by name.

The model is bound to the core tools plus find_tools / run_tool. A refusal that
says "use list_transitions_tool" makes it call an unbound name and fail again.
An unlisted tool is written as run_tool(name="x_tool", args={...}) instead.
"""

import ast
import re
from pathlib import Path

from classes.agent_tools.core_tools import CORE_TOOL_NAMES

SRC = Path(__file__).resolve().parents[1] / "src" / "classes"
LISTED = set(CORE_TOOL_NAMES) | {"find_tools", "run_tool"}
NAME = re.compile(r"\b[a-z][a-z0-9_]*_tool\b")
VIA_RUN_TOOL = re.compile(r"""run_tool\(name=["']([a-z0-9_]+_tool)["']""")
ERROR_CALLS = {"ToolError", "fail"}


def _strings(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub.value


def _error_texts(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in ERROR_CALLS:
                yield node.lineno, " ".join(_strings(node))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("Error:"):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            parts = [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            if parts and parts[0].startswith("Error:"):
                yield node.lineno, " ".join(parts)


def test_refusals_name_only_listed_tools():
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        if "legacy" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, text in _error_texts(tree):
            unlisted = set(NAME.findall(text)) - LISTED - set(VIA_RUN_TOOL.findall(text))
            if unlisted:
                bad.append(f"{path.relative_to(SRC)}:{lineno} {sorted(unlisted)}")
    assert not bad, "\n".join(bad)
