"""Plan dock checkbox status mapping (mirrors plan.js)."""

import unittest


def checkbox_for_status(status: str) -> str:
    st = (status or "pending").lower()
    if st == "completed":
        return "[x]"
    if st == "in_progress":
        return "[~]"
    if st in ("failed", "blocked"):
        return "[!]"
    if st == "skipped":
        return "[-]"
    return "[ ]"


class TestPlanDockRender(unittest.TestCase):
    def test_checkbox_pending(self):
        self.assertEqual(checkbox_for_status("pending"), "[ ]")

    def test_checkbox_completed(self):
        self.assertEqual(checkbox_for_status("completed"), "[x]")

    def test_checkbox_in_progress(self):
        self.assertEqual(checkbox_for_status("in_progress"), "[~]")

    def test_checkbox_failed(self):
        self.assertEqual(checkbox_for_status("failed"), "[!]")


if __name__ == "__main__":
    unittest.main()
