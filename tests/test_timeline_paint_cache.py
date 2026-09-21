"""Byte-budgeted LRU used by the native timeline paint caches."""

from __future__ import annotations

from windows.views.timeline_backend.paint.byte_lru import (
    ByteBudgetLRU,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ENTRIES,
    estimate_pixmap_bytes,
)


class _FakePix:
    def __init__(self, w, h, null=False):
        self._w = w
        self._h = h
        self._null = null

    def width(self):
        return self._w

    def height(self):
        return self._h

    def depth(self):
        return 32

    def isNull(self):
        return self._null


def test_estimate_pixmap_bytes_uses_wh_depth():
    assert estimate_pixmap_bytes(_FakePix(10, 20)) == 10 * 20 * 4


def test_estimate_null_pixmap_is_small():
    assert estimate_pixmap_bytes(_FakePix(0, 0, null=True)) == 64


def test_lru_evicts_oldest_when_entry_limit_hit():
    cache = ByteBudgetLRU(max_entries=3, max_bytes=10_000_000)
    cache["a"] = _FakePix(10, 10)
    cache["b"] = _FakePix(10, 10)
    cache["c"] = _FakePix(10, 10)
    cache["d"] = _FakePix(10, 10)
    assert len(cache) == 3
    assert "a" not in cache
    assert "d" in cache


def test_lru_evicts_oldest_when_byte_budget_hit():
    # Each 100x100x4 pixmap is 40_000 bytes
    cache = ByteBudgetLRU(max_entries=100, max_bytes=100_000)
    for i in range(5):
        cache[i] = _FakePix(100, 100)
    assert cache.total_bytes <= 100_000
    assert len(cache) <= 2
    assert 0 not in cache
    assert 4 in cache


def test_lru_get_marks_recently_used():
    cache = ByteBudgetLRU(max_entries=2, max_bytes=10_000_000)
    cache["a"] = _FakePix(10, 10)
    cache["b"] = _FakePix(10, 10)
    assert cache.get("a") is not None  # touch a so b is oldest
    cache["c"] = _FakePix(10, 10)
    assert "b" not in cache
    assert "a" in cache
    assert "c" in cache


def test_lru_pop_and_clear():
    cache = ByteBudgetLRU(max_entries=10, max_bytes=10_000_000)
    cache["a"] = _FakePix(10, 10)
    assert cache.pop("a") is not None
    assert len(cache) == 0
    cache["b"] = _FakePix(10, 10)
    cache.clear()
    assert len(cache) == 0
    assert cache.total_bytes == 0


def test_defaults_are_positive():
    assert DEFAULT_MAX_ENTRIES > 0
    assert DEFAULT_MAX_BYTES > 0


def test_clip_cache_key_shape_documented():
    """Paint keys include width/offset so zoom/drag mint new entries — LRU must bound them."""
    key_a = ("clip1", 100, 40, "wave", 1.0, 0.0, 5.0, True, True)
    key_b = ("clip1", 200, 40, "wave", 1.0, 0.0, 5.0, True, True)  # zoomed
    assert key_a != key_b
    cache = ByteBudgetLRU(max_entries=1, max_bytes=10_000_000)
    cache[key_a] = _FakePix(100, 40)
    cache[key_b] = _FakePix(200, 40)
    assert len(cache) == 1
    assert key_a not in cache
