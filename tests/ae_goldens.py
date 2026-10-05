"""The two golden fixtures of the After Effects export (tests/fixtures/after_effects/).

``basic`` is the manual test plan's project: four tracks, a trimmed clip, a
fade and a wipe between clips, a 2x clip, a keyframed move with a fade-in,
an editable title, music with a fade-out and markers. ``effects`` covers
effects, a reversed clip, non-centre gravity, a chroma-keyed clip with a
blend mode, an image title, a linked clip, missing media and a locked
track at 25 fps.

``python tests/ae_goldens.py`` (or ``ZENVI_UPDATE_GOLDENS=1`` with the
tests) rewrites the project JSON and the golden scripts.
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(_HERE, "fixtures", "after_effects")
TITLES = os.path.join(_HERE, "..", "src", "titles")
GENERATOR = "Zenvi golden"

FADE = "/zenvi/transitions/common/fade.svg"
WIPE = "/zenvi/transitions/common/wipe_left_to_right.svg"


def _builders():
    from ae_project import LINEAR, ProjectBuilder, const, kf, point

    def basic():
        b = ProjectBuilder(tracks=4)
        b.name_track(1, "Video")
        b.name_track(4, "Music")
        beach = b.add_file("video", path="/fixtures/media/beach.mp4", has_audio=True, duration=12.0)
        city = b.add_file("video", path="/fixtures/media/city.mp4", has_audio=True, duration=10.0)
        drone = b.add_file("video", path="/fixtures/media/drone.mp4", duration=20.0)
        photo = b.add_file("image", path="/fixtures/media/photo.jpg")
        title = b.add_title("/fixtures/project/Trip_assets/titles/Standard_3.svg")
        music = b.add_file("audio", path="/fixtures/media/music.mp3")
        b.add_clip(beach, track=1, position=0.0, start=1.0, end=6.0)
        b.add_clip(city, track=1, position=4.0, start=0.0, end=4.0)
        b.add_transition(track=1, position=4.0, duration=1.0, mask=FADE, fade_audio_hint=True)
        drone_clip = b.add_clip(drone, track=1, position=7.5, start=0.0, end=10.0)
        b.clip(drone_clip)["time"] = kf((1, 1, LINEAR), (151, 301, LINEAR))
        b.clip(drone_clip)["end"] = 151 / 30.0
        b.add_transition(track=1, position=7.5, duration=0.5, mask=WIPE)
        b.add_clip(photo, track=2, position=1.0, start=0.0, end=4.0,
                   location_x=kf((1, -0.5), (31, 0.0)), alpha=kf((1, 0.0), (16, 1.0)),
                   scale_x={"Points": [point(1, 0.9), point(121, 1.05, 0, (0.5, 0.0), (0.35, 1.0))]},
                   scale_y={"Points": [point(1, 0.9), point(121, 1.05, 0, (0.5, 0.0), (0.35, 1.0))]})
        b.add_clip(title, track=3, position=2.0, start=0.0, end=4.0)
        b.add_clip(music, track=4, position=0.0, start=0.0, end=12.5, volume=kf((1, 1.0), (331, 1.0), (376, 0.0)))
        b.add_marker(2.0, "Title in", "red")
        b.add_marker(7.5, "Wipe", "blue")
        return b

    def effects():
        b = ProjectBuilder(fps=(25, 1), tracks=3)
        b.name_track(3, "Graphics", lock=True)
        a = b.add_file("video", path="/fixtures/media/interview.mov", has_audio=True, duration=30.0,
                       fps={"num": 25, "den": 1})
        gone = b.add_file("video", path="/fixtures/media/missing.mp4", duration=8.0)
        green = b.add_file("video", path="/fixtures/media/greenscreen.mp4", duration=8.0)
        still = b.add_file("image", path="/fixtures/media/poster.png")
        title = b.add_title("/fixtures/project/titles/Gold_1.svg")
        link = {"version": 1, "kind": "remotion", "source": {"project_dir": "/fixtures/remotion/promo",
                "entry": "src/index.ts", "composition": "Intro", "composition_key": None, "file": "src/Intro.tsx",
                "line": 12, "aep": None}, "props": {"title": "Launch"},
                "render": {"codec": "prores4444", "width": 1920, "height": 1080, "fps": {"num": 25, "den": 1},
                           "duration_frames": 100, "output": "@assets/links/remotion/Intro-1a2b3c4d.mov",
                           "rendered_at": "2026-10-04T21:00:00Z", "fingerprint": "sha256:00"},
                "state": "fresh", "error": None}
        linked = b.add_file("video", path="/fixtures/project/links/Intro-1a2b3c4d.mov", duration=4.0,
                            zenvi_link=link)
        c1 = b.add_clip(a, track=1, position=0.0, start=2.0, end=8.0)
        b.add_effect(c1, "Blur", horizontal_radius=const(4.0), vertical_radius=const(4.0), iterations=const(2.0))
        b.add_effect(c1, "Brightness", brightness=kf((1, 0.0), (51, 0.15)), contrast=const(20.0))
        b.add_effect(c1, "Saturation", saturation=const(1.3))
        b.add_effect(c1, "Crop", left=const(0.05), right=const(0.05), top=const(0.0), bottom=const(0.1))
        c2 = b.add_clip(gone, track=1, position=6.0, start=0.0, end=4.0)
        b.clip(c2)["time"] = kf((1, 101, LINEAR), (101, 1, LINEAR))
        c3 = b.add_clip(green, track=2, position=1.0, start=0.0, end=5.0, gravity=0, composite=13,
                        scale_x=kf((1, 0.4), (51, 0.6)), scale_y=kf((1, 0.4), (51, 0.6)),
                        location_x=kf((1, 0.02), (51, 0.1)), location_y=const(0.05))
        b.add_effect(c3, "ChromaKey", color={"red": const(0), "green": const(200), "blue": const(40),
                                             "alpha": const(255)}, fuzz=const(35.0))
        c4 = b.add_clip(still, track=2, position=6.5, start=0.0, end=3.0)
        b.add_effect(c4, "ColorGrade", exposure=const(0.3), saturation=const(0.8), temperature=const(0.2),
                     lut_path="/fixtures/luts/film.cube")
        b.add_effect(c4, "Pixelate", pixelization=const(0.3))
        b.add_effect(c4, "Wave")
        b.add_clip(title, track=3, position=0.5, start=0.0, end=3.0, alpha=kf((1, 0.0), (13, 1.0), (63, 1.0),
                                                                             (75, 0.0)))
        b.add_clip(linked, track=3, position=5.0, start=0.0, end=4.0)
        b.add_marker(3.0, "Chapter 2", "green")
        return b

    return {"basic": basic, "effects": effects}


def _assets(name, files):
    """media_map, title_assets and mask_assets for a fixture (paths are synthetic; nothing touches the disk)."""
    from classes.exporters import after_effects as AE
    from classes.exporters.after_effects_titles import parse_title_svg
    media, titles = {}, {}
    for f in files:
        path = f["path"]
        if path.endswith(".svg"):
            continue
        base = os.path.basename(path)
        if name == "basic":  # collected next to the script
            media[f["id"]] = AE.MediaRef(abs="/fixtures/export/Trip_AfterEffects/media/" + base, rel="media/" + base)
        else:
            media[f["id"]] = AE.MediaRef(abs=path, missing="missing" in base)
    for f in files:
        if f["path"].endswith("Standard_3.svg"):
            with open(os.path.join(TITLES, "Standard_3.svg"), encoding="utf-8") as fh:
                titles[f["id"]] = AE.TitleAsset("native", layout=parse_title_svg(fh.read()))
        elif f["path"].endswith("Gold_1.svg"):
            titles[f["id"]] = AE.TitleAsset("png", reason="an SVG filter (glow, shadow or blur)", width=1920,
                                            height=1080, image=AE.MediaRef(abs="/fixtures/export/titles/Gold_1.png",
                                                                           rel="titles/Gold_1.png"))
    masks = {FADE: AE.MaskAsset("uniform", gray=0),
             WIPE: AE.MaskAsset("image", width=1920, height=1080,
                                image=AE.MediaRef(abs="/fixtures/export/masks/wipe_left_to_right.png",
                                                  rel="masks/wipe_left_to_right.png"))}
    return media, titles, masks


def project_path(name: str) -> str:
    return os.path.join(FIXTURES, name + ".project.json")


def golden_path(name: str) -> str:
    return os.path.join(FIXTURES, name + ".jsx")


def build(name: str):
    """The AeExport of fixture *name* from its committed project JSON."""
    from classes.exporters import after_effects as AE
    from classes.handoff.timeline_view import TimelineSnapshot
    with open(project_path(name), encoding="utf-8") as fh:
        data = json.load(fh)
    snapshot = TimelineSnapshot.from_project(data, "/fixtures/project/" + ("Trip" if name == "basic" else "Promo")
                                             + ".zvn")
    media, titles, masks = _assets(name, data["files"])
    return AE.build_ae_script(snapshot, media_map=media, title_assets=titles, mask_assets=masks,
                              options=AE.AeExportOptions(generator=GENERATOR))


def regenerate() -> None:
    os.makedirs(FIXTURES, exist_ok=True)
    for name, make in _builders().items():
        with open(project_path(name), "w", encoding="utf-8") as fh:
            json.dump(make().data, fh, indent=1, sort_keys=True)
            fh.write("\n")
        with open(golden_path(name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(build(name).jsx)


if __name__ == "__main__":
    sys.path.insert(0, _HERE)
    import conftest  # noqa: F401  (the headless Qt / openshot stubs)
    regenerate()
    print("regenerated", ", ".join(_builders()))
