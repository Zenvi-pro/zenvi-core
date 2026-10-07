"""Hard Gemini spend cap for every live script of this round.

Counts Gemini's own usage numbers (tokens) against a ceiling price per million tokens that is deliberately above any real
flash-lite price, so the running total over-states what was spent. The total persists in a ledger file shared by all
scripts; any call that would take it past the cap raises SpendCapExceeded BEFORE the request is sent.
Cap: GEMINI_SPEND_CAP_USD (default 5.00 for the whole round).
"""
import json, os, threading, time

import tempfile
LEDGER = os.environ.get("GEMINI_SPEND_LEDGER") or os.path.join(tempfile.gettempdir(), "zenvi_gemini_spend_ledger.json")
CAP = float(os.environ.get("GEMINI_SPEND_CAP_USD", "5.00"))
USD_PER_M_IN = 1.00      # ceiling, not the real price
USD_PER_M_OUT = 5.00
_lock = threading.Lock()


class SpendCapExceeded(RuntimeError):
    pass


def _load():
    try:
        return json.load(open(LEDGER))
    except Exception:
        return {"total_usd": 0.0, "calls": 0, "log": []}


def total() -> float:
    return _load()["total_usd"]


def cost(in_tokens: int, out_tokens: int) -> float:
    return in_tokens / 1e6 * USD_PER_M_IN + out_tokens / 1e6 * USD_PER_M_OUT


def check(estimated_in=0, estimated_out=0):
    with _lock:
        led = _load()
        if led["total_usd"] + cost(estimated_in, estimated_out) > CAP:
            raise SpendCapExceeded(f"Gemini spend cap ${CAP:.2f} reached (ledger ${led['total_usd']:.4f}); refusing to call")


def record(label, in_tokens, out_tokens):
    with _lock:
        led = _load()
        led["total_usd"] = round(led["total_usd"] + cost(in_tokens, out_tokens), 6)
        led["calls"] += 1
        led["log"].append({"t": round(time.time()), "label": label, "in": in_tokens, "out": out_tokens})
        json.dump(led, open(LEDGER, "w"))
        if led["total_usd"] > CAP:
            raise SpendCapExceeded(f"Gemini spend cap ${CAP:.2f} exceeded (ledger ${led['total_usd']:.4f})")


class _Models:
    def __init__(self, inner, label):
        self._m, self._label = inner, label

    def generate_content(self, *a, **k):
        check(estimated_in=20000, estimated_out=8000)            # worst case for one call
        resp = self._m.generate_content(*a, **k)
        u = getattr(resp, "usage_metadata", None)
        record(self._label, int(getattr(u, "prompt_token_count", 0) or 0), int(getattr(u, "candidates_token_count", 0) or 0))
        return resp

    def embed_content(self, *a, **k):
        check(estimated_in=2000)
        resp = self._m.embed_content(*a, **k)
        record(self._label + ":embed", 600, 0)                   # no usage reported: a fixed over-estimate
        return resp

    def __getattr__(self, name):
        return getattr(self._m, name)


class GuardedClient:
    """A google-genai client whose model calls are counted against the cap; files and everything else pass through."""
    def __init__(self, client, label="live"):
        self._c = client
        self.models = _Models(client.models, label)

    def __getattr__(self, name):
        return getattr(self._c, name)


def status() -> str:
    led = _load()
    return f"Gemini spend (ceiling estimate) ${led['total_usd']:.4f} of ${CAP:.2f} cap over {led['calls']} calls"
