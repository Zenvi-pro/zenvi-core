"""Run the eval and print what it measured.

    python tests/eval/run_eval.py                 # the fast, offline subset (about a minute)
    python tests/eval/run_eval.py --out report.json

Exit status 1 when a metric is outside its limit in ``thresholds.json``. Needs ffmpeg (FFMPEG_BIN_DIR) and, for the
libraries Zenvi uses, the same PYTHONPATH as the test suite.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from eval import suite  # noqa: E402


def dig(data: Dict[str, Any], path: str) -> Any:
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def violations(report: Dict[str, Any], limits: Dict[str, Dict[str, float]]) -> List[str]:
    """One line per metric that is missing or outside its limit."""
    bad: List[str] = []
    for path, rule in limits.items():
        value = dig(report, path)
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            bad.append(f"{path}: not measured")
        elif "min" in rule and value < rule["min"]:
            bad.append(f"{path}: {value} is below the floor {rule['min']}")
        elif "max" in rule and value > rule["max"]:
            bad.append(f"{path}: {value} is above the ceiling {rule['max']}")
    return bad


def load_limits() -> Dict[str, Dict[str, float]]:
    return json.loads((HERE / "thresholds.json").read_text())["limits"]


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="write the full report here as JSON")
    args = ap.parse_args(argv)
    started = time.time()
    report = suite.run_fast()
    report["seconds"] = round(time.time() - started, 1)
    bad = violations(report, load_limits())
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1, default=str))
    for path in load_limits():
        print(f"{path:55s} {dig(report, path)}")
    print(f"\n{len(load_limits()) - len(bad)} of {len(load_limits())} within limits in {report['seconds']} s")
    for line in bad:
        print("OUT OF LIMITS:", line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
