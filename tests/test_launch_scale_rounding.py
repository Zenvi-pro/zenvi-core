"""Fractional display scales (Windows 125%) must round, not pass through.

PassThrough renders the whole editor at 1.25x with soft, pixelated icons.
"""
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_launch_rounds_fractional_display_scale():
    with open(os.path.join(REPO_ROOT, "src", "launch.py"), encoding="utf-8") as fh:
        policies = re.findall(r"QT_SCALE_FACTOR_ROUNDING_POLICY'\]\s*=\s*\"(\w+)\"", fh.read())
    assert policies == ["Round"]
