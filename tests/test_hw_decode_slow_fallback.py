"""Hardware decode that is slower than software on real media is switched off.

Measured on Windows: with the auto-detected D3D11 decoder, an H.264 phone clip
took 6.2 s to open and deliver its first frame, against 0.06 s in software, and
every reader the editor opened (clip boundaries, thumbnails, Preview File) paid it.
The one-time probe cannot see this: the bundled example clip decodes fine.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.export_acceleration import hw_decode  # noqa: E402


class _Store(dict):
    def set(self, key, value):
        self[key] = value


@pytest.fixture
def lib(monkeypatch):
    """A fake libopenshot whose global decoder setting is D3D11."""
    settings = SimpleNamespace(HARDWARE_DECODER=hw_decode.HW_D3D11)
    module = types.ModuleType("openshot")
    module.Settings = SimpleNamespace(Instance=lambda: settings)
    monkeypatch.setitem(sys.modules, "openshot", module)
    return settings


def _never(_path):
    raise AssertionError("software decode must not be timed")


def test_much_slower_hardware_decode_switches_to_software(lib):
    store = _Store({"hw-decoder": "4"})
    switched = hw_decode.disable_hardware_decode_if_slower(
        "phone.mp4", 6.2, store, time_software=lambda path: 0.06)
    assert switched is True
    assert lib.HARDWARE_DECODER == hw_decode.HW_NONE
    assert store["hw-decoder"] == "0"


def test_a_quick_hardware_decode_is_left_alone(lib):
    store = _Store({"hw-decoder": "4"})
    assert hw_decode.disable_hardware_decode_if_slower("clip.mp4", 0.4, store, time_software=_never) is False
    assert lib.HARDWARE_DECODER == hw_decode.HW_D3D11 and store["hw-decoder"] == "4"


def test_media_that_is_slow_in_software_too_keeps_hardware_decode(lib):
    store = _Store({"hw-decoder": "4"})
    assert hw_decode.disable_hardware_decode_if_slower(
        "huge-8k.mov", 3.0, store, time_software=lambda path: 2.0) is False
    assert lib.HARDWARE_DECODER == hw_decode.HW_D3D11 and store["hw-decoder"] == "4"


def test_nothing_is_timed_when_software_decode_is_already_on(lib):
    lib.HARDWARE_DECODER = hw_decode.HW_NONE
    assert hw_decode.disable_hardware_decode_if_slower("phone.mp4", 6.2, _Store(), time_software=_never) is False


def test_a_failed_software_timing_changes_nothing(lib):
    def broken(_path):
        raise RuntimeError("cannot open")

    store = _Store({"hw-decoder": "4"})
    assert hw_decode.disable_hardware_decode_if_slower("phone.mp4", 6.2, store, time_software=broken) is False
    assert lib.HARDWARE_DECODER == hw_decode.HW_D3D11 and store["hw-decoder"] == "4"
