"""Byte-budgeted LRU cache for timeline pixmap / thumbnail entries.

Plain dicts grow forever when keys include zoom width and drag offset.
This cache evicts least-recently-used entries when either the entry count
or the estimated byte cost exceeds its budgets.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Callable, Optional


# Defaults tuned for timeline filmstrips. Overridable by callers / tests.
DEFAULT_MAX_ENTRIES = 512
DEFAULT_MAX_BYTES = 96 * 1024 * 1024  # 96 MiB


def estimate_pixmap_bytes(value: Any) -> int:
    """Best-effort byte cost for a QPixmap, QImage, or nested cache tuple."""
    if value is None:
        return 0

    # Clip cache stores (pix, blur, icons) or (pix, blur, icons, flag)
    if isinstance(value, tuple):
        return sum(estimate_pixmap_bytes(item) for item in value)

    width = getattr(value, "width", None)
    height = getattr(value, "height", None)
    depth = getattr(value, "depth", None)
    is_null = getattr(value, "isNull", None)
    if callable(is_null):
        try:
            if is_null():
                return 64
        except Exception:
            pass
    if callable(width) and callable(height):
        try:
            w = int(width() or 0)
            h = int(height() or 0)
        except Exception:
            return 64
        if w <= 0 or h <= 0:
            return 64
        bpp = 4
        if callable(depth):
            try:
                d = int(depth() or 0)
                if d > 0:
                    bpp = max(1, (d + 7) // 8)
            except Exception:
                bpp = 4
        return max(64, w * h * bpp)

    # Failed-lookup sentinel (empty pixmap) or non-image value
    return 64


class ByteBudgetLRU:
    """OrderedDict LRU with entry-count and byte-cost budgets."""

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        cost_fn: Optional[Callable[[Any], int]] = None,
    ):
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_entries = int(max_entries)
        self.max_bytes = int(max_bytes)
        self._cost_fn = cost_fn or estimate_pixmap_bytes
        self._data: OrderedDict[Any, Any] = OrderedDict()
        self._costs: dict[Any, int] = {}
        self._total_bytes = 0

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: Any) -> bool:
        return key in self._data

    def keys(self):
        return self._data.keys()

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def get(self, key: Any, default: Any = None) -> Any:
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def __getitem__(self, key: Any) -> Any:
        if key not in self._data:
            raise KeyError(key)
        self._data.move_to_end(key)
        return self._data[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        cost = max(0, int(self._cost_fn(value) or 0))
        if key in self._data:
            self._total_bytes -= self._costs.get(key, 0)
            self._data.move_to_end(key)
            self._data[key] = value
            self._costs[key] = cost
            self._total_bytes += cost
        else:
            self._data[key] = value
            self._costs[key] = cost
            self._total_bytes += cost
        self._evict()

    def pop(self, key: Any, default: Any = None) -> Any:
        if key not in self._data:
            return default
        value = self._data.pop(key)
        self._total_bytes -= self._costs.pop(key, 0)
        if self._total_bytes < 0:
            self._total_bytes = 0
        return value

    def clear(self) -> None:
        self._data.clear()
        self._costs.clear()
        self._total_bytes = 0

    def _evict(self) -> None:
        while self._data and (
            len(self._data) > self.max_entries or self._total_bytes > self.max_bytes
        ):
            key, _value = self._data.popitem(last=False)
            self._total_bytes -= self._costs.pop(key, 0)
            if self._total_bytes < 0:
                self._total_bytes = 0
