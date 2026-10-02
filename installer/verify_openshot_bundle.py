"""Check the libopenshot Python bindings a Windows build is about to use.

Run with PYTHONPATH on the bindings (openshot.py + _openshot.pyd + DLLs). Exits
non-zero when the bindings do not import, or when OpenCV effects were asked for
(ZENVI_OPENCV, default ON) and libopenshot was built without them.
"""
import json
import os
import sys

OPENCV_EFFECTS = ("Stabilizer", "Tracker", "ObjectDetection")


def main() -> int:
    import openshot

    print("libopenshot", openshot.OPENSHOT_VERSION_FULL)
    effects = sorted(e.get("class_name", "") for e in json.loads(openshot.EffectInfo.Json()))
    print("%d effects: %s" % (len(effects), ", ".join(effects)))
    if os.environ.get("ZENVI_OPENCV", "ON").upper() == "OFF":
        return 0
    missing = [name for name in OPENCV_EFFECTS if name not in effects]
    if missing:
        print("::error::libopenshot was built without OpenCV: no %s effect" % ", ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
