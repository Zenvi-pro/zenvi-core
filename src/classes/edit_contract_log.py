"""A tiny, local-only log of how the edit contract behaves, to decide with real numbers whether to turn enforcement on.

What it keeps: event names and counts only (a placement was refused or warned, a brief was written, an audit finished with
N needs). It never keeps what the user typed, file names, ids or paths, and nothing ever leaves the computer. One file under
the user's data folder, capped at 256 KB (the older half is kept as ``.1``), written by a background thread so no disk
write happens on the GUI thread (AGENTS.md). Switch it off with ``ZENVI_EDIT_CONTRACT_LOG=0``.

Read it: ``python -m classes.edit_contract_log`` prints what happened and, above all, how often an agent recovered after a refusal.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

LOG_NAME = "edit_contract_log.jsonl"
MAX_BYTES = 256 * 1024

# The only fields that are ever written, and only as short plain values: no free text can get in.
_ALLOWED = ("tool", "mode", "phase", "rollout", "action", "status", "tier", "source", "rounds", "needs", "structured", "total", "attempts")

_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1000)
_thread: Optional[threading.Thread] = None
_start_lock = threading.Lock()


def enabled(env: Optional[Dict[str, str]] = None) -> bool:
    value = str((env if env is not None else os.environ).get("ZENVI_EDIT_CONTRACT_LOG", "")).strip().lower()
    return value not in ("0", "off", "false", "no")


def log_path(env: Optional[Dict[str, str]] = None) -> str:
    e = env if env is not None else os.environ
    if e.get("ZENVI_EDIT_CONTRACT_LOG_PATH"):
        return e["ZENVI_EDIT_CONTRACT_LOG_PATH"]
    try:
        from classes import info
        base = info.USER_PATH
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".openshot_qt")
    return os.path.join(base, LOG_NAME)


def _clean(fields: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in _ALLOWED:
        value = fields.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            out[key] = int(value) if float(value).is_integer() else round(float(value), 2)
        elif isinstance(value, str):
            out[key] = value[:40]
    return out


def record(event: str, **fields: Any) -> None:
    """Note one event. Never raises, never blocks, never writes on the calling thread (unless ZENVI_EDIT_CONTRACT_LOG_SYNC=1, for tests)."""
    try:
        if not enabled():
            return
        item = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": str(event)[:40], **_clean(fields)}
        if os.environ.get("ZENVI_EDIT_CONTRACT_LOG_SYNC") == "1":
            _write(item)
            return
        _ensure_writer()
        _queue.put_nowait(item)
    except Exception:
        pass


def _ensure_writer() -> None:
    global _thread
    with _start_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name="edit-contract-log", daemon=True)
            _thread.start()


def _worker() -> None:
    while True:
        item = _queue.get()
        try:
            _write(item)
        except Exception:
            pass
        finally:
            _queue.task_done()


def _write(item: Dict[str, Any]) -> None:
    path = log_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        if os.path.getsize(path) > MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, separators=(",", ":")) + "\n")


def flush(timeout: float = 5.0) -> bool:
    """Wait until everything queued is written (tests and shutdown). True when the queue drained in time."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _queue.unfinished_tasks == 0:
            return True
        time.sleep(0.01)
    return _queue.unfinished_tasks == 0


def read(path: Optional[str] = None) -> List[Dict[str, Any]]:
    p = path or log_path()
    events: List[Dict[str, Any]] = []
    for candidate in (p + ".1", p):
        try:
            with open(candidate, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return events


def summarize(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts that answer "is it safe to enforce?": how often a placement was held back, and whether the agent then recovered."""
    s = {"events": 0, "refused": 0, "warned": 0, "begin_edit_ok": 0, "begin_edit_wait": 0, "placed": 0, "audits": 0,
         "pieces_started": 0, "recovered_after_refusal": 0, "refusals_without_recovery": 0, "exports_warned": 0, "mean_needs_at_audit": None}
    pending = False
    needs: List[int] = []
    for e in events:
        s["events"] += 1
        ev = e.get("event")
        if ev == "gate" and e.get("action") == "refuse":
            if pending:
                s["refusals_without_recovery"] += 1
            s["refused"] += 1
            pending = True
        elif ev == "gate" and e.get("action") == "warn":
            s["warned"] += 1
        elif ev == "begin_edit":
            if e.get("status") == "ok":
                s["begin_edit_ok"] += 1
                if pending:
                    s["recovered_after_refusal"] += 1
                    pending = False
            else:
                s["begin_edit_wait"] += 1
        elif ev == "placed":
            s["placed"] += 1
            pending = False                      # it got through some other way (a brief saved by hand)
        elif ev == "audit":
            s["audits"] += 1
            if isinstance(e.get("needs"), int):
                needs.append(e["needs"])
        elif ev == "piece_started":
            s["pieces_started"] += 1
        elif ev == "export_warned":
            s["exports_warned"] += 1
    if pending:
        s["refusals_without_recovery"] += 1
    if needs:
        s["mean_needs_at_audit"] = round(sum(needs) / len(needs), 2)
    return s


if __name__ == "__main__":
    summary = summarize(read())
    print(json.dumps(summary, indent=2))
    refused = summary["refused"]
    if refused:
        print(f"\nAfter a refusal the agent recovered {summary['recovered_after_refusal']} of {refused} times "
              f"({summary['recovered_after_refusal'] / refused:.0%}). Enforce is safe where this is high.")
    else:
        print("\nNo placement has been refused yet (run with ZENVI_EDIT_CONTRACT=enforce to collect that).")
