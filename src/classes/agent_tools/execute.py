"""Central tool dispatch: schema validate, then one undo group, then receipt."""

from __future__ import annotations

import logging
import uuid as uuid_module
from typing import Callable, Optional

log = logging.getLogger("agent_tools.execute")

# Late-bound: filled by tool_handlers after handlers are defined, to avoid cycles.
_HANDLERS: dict = {}
_READ_ONLY: frozenset = frozenset()
_BACKGROUND_SAFE: frozenset = frozenset()
_UNGROUPED: frozenset = frozenset()
_MAIN_THREAD_TIMEOUTS: dict = {}
_GET_APP: Optional[Callable] = None
_RUN_ON_MAIN: Optional[Callable] = None
_ATOMIC: Optional[Callable] = None
_COERCE_STEPS: Optional[Callable] = None
_QTHREAD = None

_MAIN_THREAD_TIMEOUT_DEFAULT = 30
_MAIN_THREAD_TIMEOUT_PER_STEP = 8
_EXPORT_MAIN_THREAD_TIMEOUT = 60 * 60 * 6


def bind_runtime(
    *,
    handlers: dict,
    read_only: frozenset,
    background_safe: frozenset,
    ungrouped: frozenset,
    main_thread_timeouts: dict,
    get_app: Callable,
    run_on_main_thread: Callable,
    atomic: Callable,
    coerce_steps: Callable,
    qthread,
) -> None:
    global _HANDLERS, _READ_ONLY, _BACKGROUND_SAFE, _UNGROUPED
    global _MAIN_THREAD_TIMEOUTS, _GET_APP, _RUN_ON_MAIN, _ATOMIC, _COERCE_STEPS, _QTHREAD
    _HANDLERS = handlers
    _READ_ONLY = read_only
    _BACKGROUND_SAFE = background_safe
    _UNGROUPED = ungrouped
    _MAIN_THREAD_TIMEOUTS = main_thread_timeouts
    _GET_APP = get_app
    _RUN_ON_MAIN = run_on_main_thread
    _ATOMIC = atomic
    _COERCE_STEPS = coerce_steps
    _QTHREAD = qthread


def _main_thread_timeout(tool_name: str, tool_args: dict) -> int:
    if tool_name in _MAIN_THREAD_TIMEOUTS:
        return _MAIN_THREAD_TIMEOUTS[tool_name]
    if tool_name not in ("undo_tool", "redo_tool"):
        return _MAIN_THREAD_TIMEOUT_DEFAULT
    n = _COERCE_STEPS((tool_args or {}).get("steps"))
    return max(_MAIN_THREAD_TIMEOUT_DEFAULT, _MAIN_THREAD_TIMEOUT_PER_STEP * n)


def _history_len(app) -> int:
    try:
        return len(app.updates.actionHistory)
    except Exception:
        return -1


def _snapshot_clips(app):
    try:
        clips = app.project.get("clips") or []
        return [dict(c) if isinstance(c, dict) else dict(getattr(c, "data", {}) or {}) for c in clips]
    except Exception:
        return None


def execute_tool(tool_name: str, tool_args: dict) -> str:
    """Execute a tool by name. Always returns a contract-3 JSON receipt string."""
    from fractions import Fraction as Fr

    from classes.agent_tools.receipt import (
        ToolReceipt,
        from_handler_str,
        is_error_result,
    )
    from classes.agent_tools.schema import HIDDEN_PARAMS, validate_args
    from classes.agent_tools.snapshot import mutation_result, timeline_snapshot

    handler = _HANDLERS.get(tool_name)
    if not handler:
        return ToolReceipt.error(tool_name, f"Unknown tool '{tool_name}'.").to_json()

    args = dict(tool_args or {})

    # chat_session_id isolation for split/import → add chains.
    if "chat_session_id" in args:
        if tool_name not in (
            "split_file_add_clip_tool",
            "add_clip_to_timeline_tool",
            "place_motion_graphic_tool",
            "import_stock_media_tool",
            "import_files_tool",
        ):
            args.pop("chat_session_id", None)

    # Schema validation BEFORE opening an undo transaction.
    schema_err = validate_args(tool_name, args)
    if schema_err:
        return ToolReceipt.refused(tool_name, schema_err).to_json()

    # Strip hidden params from the validated payload (handlers may still accept
    # transaction_id via explicit kw from internal callers).
    public_args = {k: v for k, v in args.items() if k not in HIDDEN_PARAMS or k == "chat_session_id"}
    # Re-add chat_session_id when allowed.
    if "chat_session_id" in args and tool_name in (
        "split_file_add_clip_tool",
        "add_clip_to_timeline_tool",
        "place_motion_graphic_tool",
        "import_stock_media_tool",
        "import_files_tool",
    ):
        public_args["chat_session_id"] = args["chat_session_id"]
    # Internal composites may pass transaction_id; keep it off the schema but
    # forward when present for ripple+place.
    if "transaction_id" in (tool_args or {}):
        public_args["transaction_id"] = tool_args["transaction_id"]

    def _invoke():
        app = _GET_APP()
        before_len = _history_len(app)
        before_clips = _snapshot_clips(app)
        fps = Fr(30, 1)
        try:
            from classes.clip_utils import project_fps_fraction
            fps = project_fps_fraction()
        except Exception:
            try:
                profile = app.project.get("profile") or {}
                num = int(profile.get("fps", {}).get("num") or app.project.get("fps", {}).get("num") or 30)
                den = int(profile.get("fps", {}).get("den") or app.project.get("fps", {}).get("den") or 1)
                fps = Fr(num, den)
            except Exception:
                pass

        before_snap = None
        if before_clips is not None:
            try:
                before_snap = timeline_snapshot(before_clips, fps=fps)
            except Exception:
                before_snap = None

        try:
            if tool_name in _UNGROUPED:
                raw = handler(**public_args)
            else:
                # One tool call == one undo step. Nested _transaction joins.
                raw = _ATOMIC(app, handler)(**public_args)
        except Exception as e:
            log.error("Tool %s execution failed: %s", tool_name, e, exc_info=True)
            return ToolReceipt.error(tool_name, str(e)).to_json()

        after_len = _history_len(app)
        mutated = before_len >= 0 and after_len > before_len

        # Already a receipt JSON?
        if isinstance(raw, ToolReceipt):
            receipt = raw
            if not mutated:
                receipt.undoSteps = 0
                if receipt.status == "applied" and not receipt.clips and not receipt.removedClipIds:
                    # Prefer unchanged when nothing hit history.
                    if not str(receipt.summary).startswith("Error"):
                        receipt.status = "unchanged"
            return receipt.to_json()

        text = "" if raw is None else str(raw)
        if text.strip().startswith("{") and '"contract"' in text:
            return text

        if is_error_result(text) or text.startswith("Error"):
            return from_handler_str(tool_name, text, mutated=False).to_json()

        receipt = from_handler_str(tool_name, text, mutated=mutated)
        if mutated and before_snap is not None:
            after_clips = _snapshot_clips(app)
            if after_clips is not None:
                try:
                    after_snap = timeline_snapshot(after_clips, fps=fps)
                    enriched = mutation_result(
                        before_snap,
                        after_snap,
                        tool=tool_name,
                        summary=receipt.summary,
                        status="applied",
                        undo_steps=1,
                    )
                    return enriched.to_json()
                except Exception:
                    pass
        if not mutated:
            receipt.undoSteps = 0
        return receipt.to_json()

    try:
        if _QTHREAD is None:
            return _invoke()
        app = _GET_APP()
        if _QTHREAD.currentThread() is app.thread():
            return _invoke()
        if tool_name in _READ_ONLY or tool_name in _BACKGROUND_SAFE:
            return _invoke()
        return _RUN_ON_MAIN(
            _invoke, timeout=_main_thread_timeout(tool_name, public_args)
        )
    except Exception as e:
        log.error("Tool %s dispatch failed: %s", tool_name, e, exc_info=True)
        return ToolReceipt.error(tool_name, str(e)).to_json()
