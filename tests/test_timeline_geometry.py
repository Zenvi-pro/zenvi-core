"""Native timeline geometry helpers — dirty flag, sort keys, scrollbar clamp."""

from __future__ import annotations

from types import SimpleNamespace

from windows.views.timeline_backend.geometry.base import GeometryBase, _GeometryEntry


class _FakeRect:
    def __init__(self, left, top=0.0, width=10.0, height=10.0):
        self._left = float(left)
        self._top = float(top)
        self._width = float(width)
        self._height = float(height)

    def left(self):
        return self._left

    def right(self):
        return self._left + self._width

    def top(self):
        return self._top

    def bottom(self):
        return self._top + self._height


class _FakeGeometry(GeometryBase):
    """GeometryBase with rebuild stubbed so ensure() is unit-testable."""

    def __init__(self):
        widget = SimpleNamespace(
            track_height=40.0,
            vertical_factor=40.0,
            scrollbar_position=[0.0, 1.0, 1000.0, 400.0],
            v_scrollbar_position=[0.0, 1.0, 800.0, 400.0],
            track_name_width=100.0,
            scroll_bar_thickness=12.0,
            ruler_height=30.0,
            height=lambda: 400,
            width=lambda: 800,
            scroll_bar_rect=None,
            v_scroll_bar_rect=None,
            _keyframes_dirty=False,
        )
        super().__init__(widget)
        self.rebuild_count = 0

    def _rebuild(self):
        self.rebuild_count += 1
        self.dirty = False


def test_mark_dirty_sets_flag_and_keyframes():
    geo = _FakeGeometry()
    geo.dirty = False
    geo.widget._keyframes_dirty = False
    geo.mark_dirty()
    assert geo.dirty is True
    assert geo.widget._keyframes_dirty is True


def test_ensure_rebuilds_only_when_dirty():
    geo = _FakeGeometry()
    geo.dirty = True
    geo.ensure()
    assert geo.rebuild_count == 1
    assert geo.dirty is False
    geo.ensure()
    assert geo.rebuild_count == 1


def test_entry_sort_key_orders_by_left_then_top_then_id():
    a = _GeometryEntry(_FakeRect(10.0, 0.0), SimpleNamespace(id="b"), False)
    b = _GeometryEntry(_FakeRect(10.0, 0.0), SimpleNamespace(id="a"), False)
    c = _GeometryEntry(_FakeRect(5.0, 0.0), SimpleNamespace(id="z"), False)
    keys = [GeometryBase._entry_sort_key(e) for e in (a, b, c)]
    assert sorted(keys) == [keys[2], keys[1], keys[0]]


def test_horizontal_scrollbar_clamps_left_to_visible_range():
    geo = _FakeGeometry()
    w = geo.widget
    w.scrollbar_position = [0.9, 1.0, 1000.0, 400.0]  # left too far right
    scroll_px = geo._update_horizontal_scrollbar(timeline_w=1000.0, view_w=400.0)
    assert w.scrollbar_position[0] <= 0.6 + 1e-9
    assert w.scrollbar_position[1] <= 1.0 + 1e-9
    assert scroll_px <= 600.0 + 1e-6


def test_horizontal_scrollbar_full_view_clears_handle():
    geo = _FakeGeometry()
    scroll_px = geo._update_horizontal_scrollbar(timeline_w=400.0, view_w=400.0)
    assert scroll_px == 0.0
    assert geo.widget.scroll_bar_rect is not None
