"""MemoryError on save must stop autosave and must not open a modal."""

from pathlib import Path

MAIN_WINDOW = Path(__file__).resolve().parents[1] / "src" / "windows" / "main_window.py"


def test_save_project_memoryerror_branch_has_no_dialog():
    src = MAIN_WINDOW.read_text(encoding="utf-8")
    fn = src.split("def save_project", 1)[1].split("\n    def ", 1)[0]
    oom, rest = fn.split("except MemoryError", 1)
    generic = rest.split("except Exception", 1)[0]
    assert "except MemoryError" in fn
    assert "QMessageBox" not in generic
    assert "_autosave_oom" in generic
    assert "auto_save_timer" in generic
