"""Memory budgets follow the machine's RAM, and the undo history stops growing (#404, #338).

A 4K export queued 60 decoded frames (1.85 GiB) whatever the machine had, the
preview cache was a fixed 768 MB, and the in-memory undo history had no limit:
``history-limit`` only bounded what was saved.
"""

import os
import py_compile
import sys

from classes.export_acceleration.export_tuning import export_cache_bytes, get_export_pipeline_profile
from classes.openshot_import_diag import prune_bundled_sources
from classes.system_memory import ram_budget_bytes
from classes.updates import UpdateManager

GIB = 1024 ** 3
MIB = 1024 ** 2
FRAME_4K = 3840 * 2160 * 4


def test_a_budget_is_a_sixteenth_of_ram_and_unchanged_when_ram_is_unknown():
    assert ram_budget_bytes(768 * MIB, ram=8 * GIB) == 512 * MIB
    assert ram_budget_bytes(768 * MIB, ram=16 * GIB) == 768 * MIB
    assert ram_budget_bytes(768 * MIB, ram=0) == 768 * MIB
    assert ram_budget_bytes(768 * MIB, ram=1 * GIB) == 128 * MIB  # floor


def test_a_4k_export_on_8gb_queues_and_caches_half_a_gig_each():
    profile = get_export_pipeline_profile(3840, 2160, 30, enable_parallel_composite=False, ram=8 * GIB)
    assert 8 <= profile.max_pending_frames
    assert profile.max_pending_frames * FRAME_4K <= 512 * MIB
    assert profile.cache_bytes <= 512 * MIB
    assert export_cache_bytes(3840, 2160, ram=8 * GIB) <= 512 * MIB


def test_export_sizing_is_unchanged_with_plenty_of_ram_or_small_frames():
    big = get_export_pipeline_profile(3840, 2160, 30, enable_parallel_composite=False, ram=64 * GIB)
    assert big.max_pending_frames == 60
    assert export_cache_bytes(3840, 2160, ram=64 * GIB) == FRAME_4K * 24
    hd = get_export_pipeline_profile(1280, 720, 30, enable_parallel_composite=False, ram=8 * GIB)
    assert hd.max_pending_frames == 60


def _edit(manager, n, transaction=None):
    manager.transaction_id = transaction
    manager.update(["clips", {"id": "c"}], {"position": n})


def test_undo_history_in_memory_keeps_only_the_last_steps_and_keeps_them_whole():
    manager = UpdateManager()
    manager.MEMORY_STEPS = 5
    for n in range(3):
        _edit(manager, n, transaction="first")  # one step of three actions
    for n in range(4):
        _edit(manager, n)
    assert len(manager.actionHistory) == 7  # 5 steps: "first" (3 actions) + 4
    _edit(manager, 99)
    assert [a.transaction for a in manager.actionHistory].count("first") == 0
    assert len({a.transaction for a in manager.actionHistory}) == 5
    assert manager.actionHistory[-1].values == {"position": 99}


def test_pruning_a_bundle_leaves_bytecode_only_and_drops_the_duplicate_trees(tmp_path):
    lib = tmp_path / "lib"
    for tree in ("classes", "src/classes", "openshot_qt/classes", "blender/scripts"):
        (lib / tree).mkdir(parents=True)
        (lib / tree / "zz_prune_probe.py").write_text("VALUE = 7\n", encoding="utf-8")
    py_compile.compile(str(lib / "classes" / "zz_prune_probe.py"), cfile=str(lib / "classes" / "zz_prune_probe.pyc"))

    prune_bundled_sources(str(tmp_path))

    assert not (lib / "src").exists() and not (lib / "openshot_qt").exists()
    assert os.listdir(lib / "classes") == ["zz_prune_probe.pyc"]
    # Nothing compiled this one, so its source is the only copy.
    assert os.listdir(lib / "blender" / "scripts") == ["zz_prune_probe.py"]
    sys.path.insert(0, str(lib / "classes"))
    try:
        import zz_prune_probe
        assert zz_prune_probe.VALUE == 7
    finally:
        sys.path.remove(str(lib / "classes"))
        sys.modules.pop("zz_prune_probe", None)


def test_freeze_prunes_the_windows_bundle_before_listing_it():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "freeze.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert 0 < src.index("prune_bundled_sources(") < src.index("write_bundle_manifest(os.path.join(build_path")
