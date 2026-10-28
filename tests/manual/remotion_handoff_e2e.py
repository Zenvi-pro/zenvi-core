#!/usr/bin/env python3
"""Real end-to-end check of the Remotion <-> Zenvi handoff (not part of the pytest suite).

Runs the project's own Remotion (Node) and libopenshot 1.0 in the headless
editor harness (real ProjectDataStore, UpdateManager and undo; the timeline
widget is the tests' FakeTimeline), driving the real editor tools:

  linked    list + import TitleCard (transparent) and Scene (opaque) with
            import_remotion_project_tool; decode the renders with libopenshot
            (the title keeps alpha); change a prop with update_linked_clip_tool
            (re-render, one undo step); cancel a render and check that no
            headless Chrome is left behind.
  export    a Zenvi project (video + image + title + keyframed move + fade
            transition) -> export_to_remotion_tool -> npm install ->
            npx remotion render (PNG sequence) -> compare with libopenshot's
            frames of the same project (PSNR, mean absolute difference);
            side-by-side images are written for a human to look at.
  reimport  import the export back (restore_native) -> the native clips,
            files, transitions and markers equal the original.
  edits     edits made to the export's timeline.json (moves, trims, a
            duplicate, a title moved under the video) render the same in
            Remotion and, after the restore, in libopenshot.
  helper    (review round 1) helper.mjs run through a symlinked Zenvi folder;
            a Date default prop survives listing -> stills -> a linked import
            -> a re-render; remotion.config's delayRender timeout reaches the
            page.
  restored  (review round 1) "Open as an editable Zenvi project" with the
            real dialog code (offscreen Qt): unsaved changes settled first,
            a new default name, the open project's file never written; the
            written .zvn renders like the original in libopenshot.
  update    (review round 1) exporting into the earlier export: refused over
            Remotion-side edits (tree unchanged), refused through a
            symlinked media folder, a cancelled update changes nothing, an
            unchanged re-export still renders, replace_edits replaces.
  dynamic   (verification round) a project whose compositions come from
            data (templates.map(t => <Composition id={t.id} .../>)): the
            static scan finds none, the import dialog code asks the trust
            question and lists them with the real Remotion; one imports as
            a linked clip while the project is saved under a new name
            mid-render; a re-render and Open in Studio from the menus ask
            first for a folder nobody trusted this session (a "no" runs no
            Node), the agent tool does not ask.
  (edits now also exports into the edited folder after importing its
  edits: no Replace prompt.)

Usage (heavy: renders and npm install go through the machine-wide lock)::

    QT_QPA_PLATFORM=offscreen PYTHONPATH=$HOME/zenvi-deps-1.0/python ZENVI_REMOTION_CONCURRENCY=2 \\
      ~/Projects/zenvi-worktrees/.handoff/bin/heavy.sh .venv/bin/python tests/manual/remotion_handoff_e2e.py \\
      --work /tmp/remotion-e2e --remotion-app /path/to/installed/remotion-app all

``--remotion-app`` is an installed Remotion project with TitleCard and Scene
compositions (the handoff fixture's sample). Results go to
``<work>/report.json``; images to ``<work>/frames``.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
SRC = os.path.join(os.path.dirname(TESTS), "src")
REPORT: dict = {}
_KEEP: list = []


def log(*parts):
    print("[e2e]", *parts, flush=True)


def boot():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path[:0] = [SRC, TESTS]
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    _KEEP.append(app)  # PyQt deletes the C++ QApplication with its last Python reference
    import openshot  # the real libopenshot (PYTHONPATH)
    from classes import tool_handlers
    tool_handlers.QThread = None  # no event loop here: GUI hops run inline, like the headless suite
    log("libopenshot", openshot.OPENSHOT_VERSION_FULL)
    return app


def make_editor(work):
    from classes import info
    from editor_tools_harness import make_editor as _make
    from titles_text_fakes import FakeFilesModel, FakeTimeline
    info.USER_PATH = os.path.join(work, "user")
    os.makedirs(info.USER_PATH, exist_ok=True)
    editor = _make()
    editor.window.timeline = FakeTimeline(editor)
    editor.window.files_model = FakeFilesModel(editor)
    import classes.handoff.remotion  # noqa: F401  (registers the provider)
    return editor


def ffprobe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=codec_name,pix_fmt,width,height,r_frame_rate,nb_frames", "-of", "json", path],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["streams"][0]


def openshot_frame_png(path, frame, out_png):
    import openshot
    reader = openshot.FFmpegReader(path)
    reader.Open()
    try:
        reader.GetFrame(frame).Save(out_png, 1.0, "PNG", 100)
    finally:
        reader.Close()
    return out_png


def alpha_at(png, points):
    from PIL import Image
    img = Image.open(png).convert("RGBA")
    return [img.getpixel(p)[3] for p in points], img.getchannel("A").getextrema()


def over_black(png, out):
    from PIL import Image
    img = Image.open(png).convert("RGBA")
    bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
    Image.alpha_composite(bg, img).convert("RGB").save(out)
    return out


def chrome_orphans(project_dir):
    from classes.handoff.remotion import helper
    return helper.orphan_pids(project_dir)


def chrome_children(project_dir):
    ps = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    return [line.strip() for line in ps.splitlines()
            if os.path.join(project_dir, "node_modules", ".remotion") in line]


# ---------------------------------------------------------------------------
# (a) Remotion -> Zenvi linked clips
# ---------------------------------------------------------------------------

def part_linked(work, app_dir):
    from classes.handoff import jobs
    from classes.handoff import linked_media as lm
    from classes.handoff.remotion import detect, provider
    editor = make_editor(work)
    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)
    result = {}

    t0 = time.time()
    listing = editor.call_receipt("list_remotion_compositions_tool", project_dir=app_dir)
    result["list_seconds"] = round(time.time() - t0, 1)
    comps = {c["id"]: c for c in listing["data"]["compositions"]}
    log("list:", listing["summary"], "(%.1fs)" % result["list_seconds"])
    result["compositions"] = {k: {x: v[x] for x in ("width", "height", "fps", "duration_frames", "file", "line")}
                              for k, v in comps.items()}

    editor.mark()
    t0 = time.time()
    receipt = editor.call_receipt("import_remotion_project_tool", project_dir=app_dir)
    result["import_seconds"] = round(time.time() - t0, 1)
    log("import:", receipt["status"], receipt["summary"][:300])
    assert receipt["status"] == "applied", receipt
    result["import_undo_steps"] = editor.undo_steps_since_mark()
    clips = {c["composition"]: c for c in receipt["data"]["linked"]}
    result["linked"] = {}
    for comp, c in clips.items():
        f = editor.file(c["file_id"])
        info = ffprobe(f["path"])
        link = lm.read_link(f)
        result["linked"][comp] = {"codec": c["codec"], "ffprobe": info, "layer": c["layer"],
                                  "state": lm.check_link(f).state, "path": f["path"],
                                  "source": {k: link["source"].get(k) for k in ("file", "line", "folder")}}
        log(comp, c["codec"], info, "state", result["linked"][comp]["state"])

    # libopenshot decodes them; the title keeps alpha (frame 25: background clear, text solid)
    title_path = editor.file(clips["TitleCard"]["file_id"])["path"]
    png = openshot_frame_png(title_path, 26, os.path.join(frames, "linked-title-f25.png"))
    corner, extrema = alpha_at(png, [(5, 5), (1900, 20)])
    text_alpha = None
    from PIL import Image
    img = Image.open(png).convert("RGBA")
    for y in range(820, 960, 4):  # the title text row of the TitleCard
        for x in range(110, 600, 4):
            if img.getpixel((x, y))[3] == 255:
                text_alpha = (x, y)
                break
        if text_alpha:
            break
    result["title_alpha"] = {"corners": corner, "extrema": extrema, "solid_text_pixel": text_alpha}
    over_black(png, os.path.join(frames, "linked-title-f25-on-black.png"))
    scene_path = editor.file(clips["Scene"]["file_id"])["path"]
    spng = openshot_frame_png(scene_path, 30, os.path.join(frames, "linked-scene-f29.png"))
    result["scene_alpha_extrema"] = alpha_at(spng, [(0, 0)])[1]
    log("title alpha", result["title_alpha"], "scene alpha", result["scene_alpha_extrema"])

    # change a prop -> re-render -> ONE undo step for the swap
    title_id = clips["TitleCard"]["file_id"]
    before = editor.file(title_id)["path"]
    editor.mark()
    t0 = time.time()
    upd = editor.call_receipt("update_linked_clip_tool", file_id=title_id, props={"title": "Launch day"})
    result["rerender_seconds"] = round(time.time() - t0, 1)
    log("update:", upd["status"], upd["summary"][:200])
    after = editor.file(title_id)["path"]
    result["rerender"] = {"status": upd["status"], "undo_steps": editor.undo_steps_since_mark(),
                          "path_changed": after != before, "props": lm.link_props(lm.read_link(editor.file(title_id)))}
    png2 = openshot_frame_png(after, 46, os.path.join(frames, "rerender-title-f45.png"))
    over_black(png2, os.path.join(frames, "rerender-title-f45-on-black.png"))
    editor.undo()
    result["rerender"]["undo_restores_previous_render"] = editor.file(title_id)["path"] == before
    editor.redo()

    # cancel a render: JobCancelled, no partial file, no headless Chrome left behind
    project = detect.inspect_project(app_dir)
    link = lm.read_link(editor.file(title_id))
    link["props"]["title"] = "Cancel me"
    stop = threading.Event()
    threading.Timer(8.0, stop.set).start()
    staging = os.path.join(work, "cancel-staging")
    os.makedirs(staging, exist_ok=True)
    cancelled = False
    try:
        provider.RemotionProvider().render(link, staging, on_progress=lambda f, m: None, should_cancel=stop.is_set)
    except jobs.JobCancelled:
        cancelled = True
    time.sleep(1.0)
    result["cancel"] = {"raised_job_cancelled": cancelled, "orphans": chrome_orphans(project.root),
                        "chrome_processes": chrome_children(project.root),
                        "staging_files": sorted(os.listdir(staging))}
    log("cancel:", result["cancel"])
    REPORT["linked"] = result
    return result


# ---------------------------------------------------------------------------
# (b) Zenvi -> Remotion export, rendered by both
# ---------------------------------------------------------------------------

TITLE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">
  <rect x="192" y="780" width="12" height="150" fill="#FF5A36"/>
  <text x="230" y="860" font-family="Helvetica, Arial, sans-serif" font-size="96" font-weight="700" fill="#F7F7F5">Zenvi to Remotion</text>
  <text x="232" y="920" font-family="Helvetica, Arial, sans-serif" font-size="44" fill="#F7F7F5">round trip check</text>
</svg>
"""


def _kf(*points):
    out = []
    for p in points:
        interp = p[2] if len(p) > 2 else 0
        hl = p[3] if len(p) > 3 else (0.5, 1.0)
        hr = p[4] if len(p) > 4 else (0.5, 0.0)
        out.append({"co": {"X": float(p[0]), "Y": float(p[1])}, "interpolation": interp, "handle_type": 0,
                    "handle_left": {"X": hl[0], "Y": hl[1]}, "handle_right": {"X": hr[0], "Y": hr[1]}})
    return {"Points": out}


MATRIX_TAGS = {"bt709": ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"],
               "bt601": ["-colorspace", "smpte170m", "-color_primaries", "smpte170m", "-color_trc", "smpte170m"]}
TITLE_STRIP = (150, 740, 1300, 960)   # where the title's text is drawn (Qt vs Chrome glyphs differ)


def build_fixture_project(work, matrix="bt709"):
    """A 6 s 1080p30 project with real media: video (trimmed, fading in), image (keyframed move + rotation),
    an SVG title with a fade transition, a marker."""
    import openshot
    from classes.image_types import get_media_type
    from windows.models.files_model import inspect_media
    media = os.path.join(work, "media")
    os.makedirs(media, exist_ok=True)
    video = os.path.join(media, "testsrc-%s.mp4" % matrix)
    if not os.path.exists(video):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=6",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
                       + MATRIX_TAGS[matrix] + ["-c:a", "aac", "-shortest", video], check=True)
    image = os.path.join(media, "card.png")
    if not os.path.exists(image):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=800x400:rate=1",
                        "-frames:v", "1", image], check=True)
    title = os.path.join(media, "title.svg")
    with open(title, "w") as fh:
        fh.write(TITLE_SVG)
    mask = os.path.join(SRC, "transitions", "common", "fade.svg")

    with open(os.path.join(SRC, "settings", "_default.project")) as fh:
        project = json.load(fh)
    project.update(fps={"num": 30, "den": 1}, width=1920, height=1080, sample_rate=48000, channels=2,
                   channel_layout=3, duration=300.0)
    files, clips = [], []

    def add_file(fid, path, name):
        reader, _dur = inspect_media(path)
        data = dict(reader)
        data.update(id=fid, path=path, name=name, media_type=get_media_type(reader))
        files.append(data)
        return data

    def add_clip(cid, fdata, layer, position, start, end, **props):
        clip = json.loads(openshot.Clip(fdata["path"]).Json())
        clip.update(id=cid, file_id=fdata["id"], title=fdata["name"], reader=copy.deepcopy(fdata), layer=layer,
                    position=position, start=start, end=end, duration=float(fdata.get("duration") or end))
        clip.update(props)
        clips.append(clip)
        return clip

    fv = add_file("FVIDEO0001", video, "testsrc.mp4")
    fi = add_file("FIMAGE0001", image, "card.png")
    ft = add_file("FTITLE0001", title, "Title")
    # video: trimmed by 0.5 s, fades in over 0.5 s (eased), full screen
    add_clip("CVIDEO0001", fv, 1000000, 0.0, 0.5, 6.0, alpha=_kf((16, 0.0), (31, 1.0)))
    # image: 40 %, slides from left to right of centre with an expo-out ease, turns 0 -> 10 degrees
    add_clip("CIMAGE0001", fi, 2000000, 0.5, 0.0, 4.0, scale_x=_kf((1, 0.4)), scale_y=_kf((1, 0.4)),
             location_x=_kf((1, -0.3), (91, 0.2, 0, (0.3, 1.0), (0.16, 1.0))),
             location_y=_kf((1, -0.15)), rotation=_kf((1, 0.0), (91, 10.0)))
    # title: from 1.0 s for 4 s, faded in by a 1 s Fade transition on its track
    add_clip("CTITLE0001", ft, 3000000, 1.0, 0.0, 4.0)
    transition = {
        "id": "TFADE00001", "layer": 3000000, "position": 1.0, "start": 0.0, "end": 1.0, "duration": 1.0,
        "title": "Fade", "type": "Mask", "class_name": "Mask", "name": "Alpha Mask / Wipe Transition",
        "brightness": _kf((1, 1.0, 1), (31, -1.0, 1)), "contrast": _kf((1, 3.0)),
        "reader": json.loads(openshot.QtImageReader(mask).Json()), "replace_image": False,
    }
    transition["reader"]["path"] = mask
    project["files"], project["clips"], project["effects"] = files, clips, [transition]
    project["markers"] = [{"id": "MARK000001", "position": 3.0, "name": "Hold", "vector": "blue"}]
    return project


def libopenshot_frames(project, frames, out_dir):
    import openshot
    os.makedirs(out_dir, exist_ok=True)
    t = openshot.Timeline(project["width"], project["height"], openshot.Fraction(30, 1), 48000, 2,
                          openshot.LAYOUT_STEREO)
    t.SetJson(json.dumps(project))
    t.Open()
    out = {}
    try:
        for n in frames:
            path = os.path.join(out_dir, "zenvi-%03d.png" % n)
            t.GetFrame(n + 1).Save(path, 1.0, "PNG", 100)
            out[n] = path
    finally:
        t.Close()
    return out


def _flat(png, size=None):
    from PIL import Image
    img = Image.open(png).convert("RGBA")
    if size is not None and img.size != size:
        img = img.resize(size)
    return Image.alpha_composite(Image.new("RGBA", img.size, (0, 0, 0, 255)), img).convert("RGB")


def compare(a_png, b_png, exclude=(), box=None):
    """(PSNR dB, mean absolute difference 0-255, diff image) over RGB, both composited over black.

    *exclude* boxes are blanked in both images first; *box* crops both to a region.
    """
    import math
    from PIL import ImageChops, ImageStat
    a = _flat(a_png)
    b = _flat(b_png, a.size)
    for region in exclude:
        a.paste((0, 0, 0), region)
        b.paste((0, 0, 0), region)
    if box is not None:
        a, b = a.crop(box), b.crop(box)
    diff = ImageChops.difference(a, b)
    stat = ImageStat.Stat(diff)
    mad = sum(stat.mean) / 3.0
    mse = sum(s / stat.count[i] for i, s in enumerate(stat.sum2)) / 3.0
    psnr = float("inf") if mse == 0 else 10 * math.log10(255.0 ** 2 / mse)
    return round(psnr, 2), round(mad, 3), diff


def side_by_side(a_png, b_png, diff, out):
    from PIL import Image, ImageOps
    a = Image.open(a_png).convert("RGB")
    b = Image.open(b_png).convert("RGB").resize(a.size)
    d = ImageOps.autocontrast(diff.convert("L")).convert("RGB")
    w, h = a.size
    sheet = Image.new("RGB", (w * 3 // 2, h // 2), (40, 40, 40))
    for i, img in enumerate((a, b, d)):
        sheet.paste(img.resize((w // 2, h // 2)), (i * w // 2, 0))
    sheet.save(out)


def decoder_check(work, video, out_dir):
    """Which YUV->RGB matrix each side used for the video: libopenshot vs Remotion vs ffmpeg bt601 / bt709."""
    refs = {}
    for matrix in ("bt601", "bt709"):
        ref = os.path.join(out_dir, "ref-%s.png" % matrix)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1.0", "-i", video, "-frames:v", "1", "-vf",
                        "scale=1920:1080:in_color_matrix=%s:flags=bicubic,format=rgb24" % matrix, ref], check=True)
        refs[matrix] = ref
    return refs


def part_export(work, app_dir, matrix="bt709"):
    from classes.handoff.remotion import install
    editor = make_editor(work)
    project = build_fixture_project(work, matrix)
    editor.store._data = copy.deepcopy(project)
    editor.mark()
    export_dir = os.path.join(work, "trip-remotion")
    if os.path.isdir(export_dir):
        shutil.rmtree(export_dir)
    receipt = editor.call_receipt("export_to_remotion_tool", output_dir=export_dir)
    log("export:", receipt["status"], receipt["summary"][:300])
    assert receipt["status"] in ("applied", "unchanged", "ok"), receipt
    result = {"export": {k: receipt["data"][k] for k in ("clips", "transitions", "media_files", "notes", "warnings",
                                                         "composition")},
              "export_undo_steps": editor.undo_steps_since_mark()}
    last = int(receipt["data"]["composition"]["durationInFrames"]) - 1
    t0 = time.time()
    inst = install.install_dependencies(export_dir)
    result["npm_install_seconds"] = round(time.time() - t0, 1)
    result["npm_install"] = inst
    log("npm install:", inst, "%.1fs" % result["npm_install_seconds"])
    # reuse the headless Chrome the linked part downloaded (saves another ~90 MB download)
    browser = os.path.join(app_dir, "node_modules", ".remotion")
    if os.path.isdir(browser) and not os.path.isdir(os.path.join(export_dir, "node_modules", ".remotion")):
        subprocess.run(["cp", "-Rc", browser, os.path.join(export_dir, "node_modules", ".remotion")], check=True)
    tsc = subprocess.run(["npx", "tsc", "--noEmit", "-p", "."], cwd=export_dir, capture_output=True, text=True)
    result["tsc"] = {"code": tsc.returncode, "output": (tsc.stdout + tsc.stderr)[-1500:]}
    log("tsc --noEmit:", tsc.returncode, (tsc.stdout + tsc.stderr)[-500:])
    seq = os.path.join(work, "frames", "remotion-seq")
    shutil.rmtree(seq, ignore_errors=True)
    t0 = time.time()
    render = subprocess.run(["npx", "remotion", "render", "src/index.ts", "ZenviTimeline", seq, "--sequence",
                             "--image-format=png", "--frames=0-%d" % last, "--concurrency=%s" % os.environ.get(
                                 "ZENVI_REMOTION_CONCURRENCY", "2"), "--log=warn"],
                            cwd=export_dir, capture_output=True, text=True)
    result["remotion_render"] = {"code": render.returncode, "seconds": round(time.time() - t0, 1),
                                 "tail": (render.stdout + render.stderr)[-1200:]}
    log("npx remotion render:", render.returncode, "%.1fs" % result["remotion_render"]["seconds"])
    assert render.returncode == 0, render.stdout + render.stderr
    import re
    rendered = sorted((f for f in os.listdir(seq) if f.endswith(".png")),
                      key=lambda f: int(re.findall(r"\d+", f)[-1]))
    result["remotion_frames"] = len(rendered)
    picks = [n for n in (0, 10, 15, 22, 30, 37, 45, 60, 75, 90, 105, 120, 135, 150) if n <= last] + [last]
    zenvi = libopenshot_frames(project, picks, os.path.join(work, "frames", "zenvi"))
    rows = []
    for n in picks:
        remotion_png = os.path.join(seq, rendered[n])
        psnr, mad, diff = compare(zenvi[n], remotion_png)
        psnr_nt, mad_nt, _ = compare(zenvi[n], remotion_png, exclude=[TITLE_STRIP])
        side_by_side(zenvi[n], remotion_png, diff, os.path.join(work, "frames", "compare-%03d.png" % n))
        rows.append({"frame": n, "psnr_db": psnr, "mean_abs_diff": mad, "psnr_db_without_title_text": psnr_nt,
                     "mean_abs_diff_without_title_text": mad_nt})
        log("frame %3d  PSNR %6.2f dB  mean |diff| %6.3f   (without the title text: %6.2f dB, %6.3f)"
            % (n, psnr, mad, psnr_nt, mad_nt))
    result["compare"] = rows
    # the video's colour matrix: which decode is each side closest to? (frame 15 = source 1.0 s)
    video = [f["path"] for f in project["files"] if f["id"] == "FVIDEO0001"][0]
    refs = decoder_check(work, video, os.path.join(work, "frames"))
    bars = (1500, 600, 1900, 1050)  # plain video colour bars, nothing drawn over them at frame 15
    result["decoder_matrix"] = {
        side: {m: compare(png, refs[m], box=bars)[:2] for m in refs}
        for side, png in (("libopenshot", zenvi[15]), ("remotion", os.path.join(seq, rendered[15])))}
    log("video colour matrix (PSNR, mean |diff| vs ffmpeg decodes):", result["decoder_matrix"])
    REPORT["export"] = result
    REPORT["export_project"] = project
    return project, export_dir


# ---------------------------------------------------------------------------
# (c) back into Zenvi
# ---------------------------------------------------------------------------

def part_reimport(work, project, export_dir):
    editor = make_editor(work)
    editor.store._data["fps"] = dict(project["fps"])
    editor.mark()
    receipt = editor.call_receipt("import_remotion_project_tool", project_dir=export_dir, position=0.0)
    log("reimport:", receipt["status"], receipt["summary"][:300])
    result = {"status": receipt["status"], "undo_steps": editor.undo_steps_since_mark(), "equal": {}}
    for key in ("files", "clips", "effects", "markers"):
        mine = sorted(editor.get(key) or [], key=lambda d: d["id"])
        theirs = sorted(json.loads(json.dumps(project[key])), key=lambda d: d["id"])
        result["equal"][key] = mine == theirs
        if mine != theirs:
            for a, b in zip(mine, theirs):
                if a != b:
                    keys = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
                    result.setdefault("differences", {})[key] = keys
                    break
    log("reimport equality:", result["equal"], result.get("differences"))
    REPORT["reimport"] = result
    return result


# ---------------------------------------------------------------------------
# (d) edits made in the Remotion project come back
# ---------------------------------------------------------------------------

def edit_timeline(timeline):
    """What a person or agent might do in timeline.json (edits whose meaning is the same in Zenvi and Remotion)."""
    clips = {c["id"]: c for c in timeline["clips"]}
    # the title onto a new track below the video: hidden under it in Zenvi; Remotion must stack by track too,
    # although the title's entry still comes after the video's in the file
    clips["CTITLE0001"]["layer"] = 999999
    card = clips["CIMAGE0001"]
    card["position"] += 0.5                                      # move the card 0.5 s later
    card["keyframes"]["location_x"][-1]["value"] = 0.0           # ...and slide it to the centre instead
    dup = copy.deepcopy(card)
    dup.update(id="CARDCOPY01", position=100 / 30)               # a second card at 3.33 s (runs past the end)
    timeline["clips"].append(dup)
    clips["CVIDEO0001"]["end"] = 5.0                             # cut the video's last second
    timeline["markers"][0].update(time=4.0, name="Late hold")
    return {"moved": "CIMAGE0001", "duplicate": "CARDCOPY01", "trimmed": "CVIDEO0001", "under_the_video": "CTITLE0001"}


def part_edits(work, project, export_dir):
    edited_dir = export_dir + "-edited"
    if os.path.isdir(edited_dir):
        shutil.rmtree(edited_dir)
    subprocess.run(["cp", "-Rc", export_dir, edited_dir], check=True)   # APFS clone, node_modules included
    path = os.path.join(edited_dir, "src", "zenvi", "timeline.json")
    with open(path) as fh:
        timeline = json.load(fh)
    what = edit_timeline(timeline)
    with open(path, "w") as fh:
        json.dump(timeline, fh, indent=1)
    fps = float(timeline["composition"]["fps"])
    ends = [int((c["position"] + c["end"] - c["start"]) * fps + 0.5) for c in timeline["clips"]]
    last = max(ends + [t["from"] + t["durationInFrames"] for t in timeline["transitions"]]) - 1  # what Root.tsx computes
    seq = os.path.join(work, "frames", "remotion-edited-seq")
    shutil.rmtree(seq, ignore_errors=True)
    t0 = time.time()
    render = subprocess.run(["npx", "remotion", "render", "src/index.ts", "ZenviTimeline", seq, "--sequence",
                             "--image-format=png", "--frames=0-%d" % last, "--concurrency=%s" % os.environ.get(
                                 "ZENVI_REMOTION_CONCURRENCY", "2"), "--log=warn"],
                            cwd=edited_dir, capture_output=True, text=True)
    result = {"edits": what, "remotion_render": {"code": render.returncode, "seconds": round(time.time() - t0, 1)}}
    log("npx remotion render (edited):", render.returncode, "%.1fs" % result["remotion_render"]["seconds"])
    assert render.returncode == 0, render.stdout + render.stderr
    editor = make_editor(work)
    editor.store._data["fps"] = dict(project["fps"])
    editor.mark()
    receipt = editor.call_receipt("import_remotion_project_tool", project_dir=edited_dir, position=0.0)
    native = receipt["data"]["native"]
    result["restore"] = {"status": receipt["status"], "undo_steps": editor.undo_steps_since_mark(),
                         "edits": [(e["kind"], e["id"], e["field"]) for e in native["edits"]],
                         "warnings": receipt["data"]["warnings"]}
    log("restore:", receipt["summary"][:240])
    log("edits brought back:", result["restore"]["edits"], "warnings:", result["restore"]["warnings"])
    # the round trip goes on: export into the edited folder again -- its edits are in this project now
    again = editor.call_receipt("export_to_remotion_tool", output_dir=edited_dir)
    result["export_again"] = {"status": again["status"], "mode": again["data"].get("mode"),
                              "warnings": again["data"].get("warnings")}
    log("export into the edited folder after importing its edits:", result["export_again"],
        again["summary"][:200])
    assert again["status"] == "applied" and not any("replaced" in w for w in again["data"].get("warnings") or [])
    restored = copy.deepcopy(project)
    for key in ("files", "clips", "effects", "markers", "layers"):
        restored[key] = copy.deepcopy(editor.get(key))
    import re
    rendered = sorted((f for f in os.listdir(seq) if f.endswith(".png")), key=lambda f: int(re.findall(r"\d+", f)[-1]))
    picks = [n for n in (0, 20, 30, 45, 60, 90, 105, 120, 140, 150, 180, 200) if n <= last] + [last]
    zenvi = libopenshot_frames(restored, picks, os.path.join(work, "frames", "zenvi-edited"))
    rows = []
    for n in picks:
        remotion_png = os.path.join(seq, rendered[n])
        psnr, mad, diff = compare(zenvi[n], remotion_png)
        psnr_nt, mad_nt, _ = compare(zenvi[n], remotion_png, exclude=[TITLE_STRIP])
        side_by_side(zenvi[n], remotion_png, diff, os.path.join(work, "frames", "compare-edited-%03d.png" % n))
        rows.append({"frame": n, "psnr_db": psnr, "mean_abs_diff": mad, "psnr_db_without_title_text": psnr_nt,
                     "mean_abs_diff_without_title_text": mad_nt})
        log("edited frame %3d  PSNR %6.2f dB  mean |diff| %6.3f   (without the title text: %6.2f dB, %6.3f)"
            % (n, psnr, mad, psnr_nt, mad_nt))
    result["compare"] = rows
    REPORT["edits"] = result
    return result


# ---------------------------------------------------------------------------
# (e) review round 1: the helper through symlinks, special prop types, config render settings
# ---------------------------------------------------------------------------

REVIEW_CHECKS_TSX = """import React from 'react';
import {AbsoluteFill} from 'remotion';

// Throws unless the prop arrives as a real Date (a plain ISO string has no toISOString()).
export const DateCard: React.FC<{when: Date}> = ({when}) => (
  <AbsoluteFill style={{backgroundColor: 'white', color: 'black', fontSize: 72, justifyContent: 'center',
    alignItems: 'center'}}>
    {when.toISOString().slice(0, 10)}
  </AbsoluteFill>
);

// Throws unless remotion.config's Config.setDelayRenderTimeoutInMilliseconds(123456) reached the page.
export const SettingsCard: React.FC = () => {
  const timeout = (window as unknown as {remotion_puppeteerTimeout?: number}).remotion_puppeteerTimeout;
  if (timeout !== 123456) {
    throw new Error('remotion.config delayRender timeout did not arrive: ' + String(timeout));
  }
  return <AbsoluteFill style={{backgroundColor: '#123456'}} />;
};
"""


def review_app(work, app_dir):
    """A scratch clone of the sample with a Date-prop composition, a settings check and a config setting."""
    app = os.path.join(work, "app-review")
    if not os.path.isdir(app):
        subprocess.run(["cp", "-Rc", app_dir, app], check=True)  # APFS clone, node_modules included
        with open(os.path.join(app, "src", "ReviewChecks.tsx"), "w") as fh:
            fh.write(REVIEW_CHECKS_TSX)
        root = os.path.join(app, "src", "Root.tsx")
        text = open(root).read()
        text = text.replace("import {Scene} from './Scene';",
                            "import {Scene} from './Scene';\nimport {DateCard, SettingsCard} from './ReviewChecks';")
        text = text.replace(
            '<Composition id="Scene"',
            '<Composition id="DateCard" component={DateCard} durationInFrames={30} fps={30} width={640} '
            "height={360} defaultProps={{when: new Date('2026-05-01T00:00:00.000Z')}} />\n"
            '      <Composition id="SettingsCard" component={SettingsCard} durationInFrames={30} fps={30} '
            'width={640} height={360} />\n      <Composition id="Scene"')
        open(root, "w").write(text)
        with open(os.path.join(app, "remotion.config.ts"), "a") as fh:
            fh.write("Config.setDelayRenderTimeoutInMilliseconds(123456);\n")
    return app


def part_helper(work, app_dir):
    from classes.handoff.remotion import helper, importer
    app = review_app(work, app_dir)
    result = {}
    # (1) helper.mjs reached through a symlinked Zenvi folder (macOS /tmp, /opt/zenvi -> /opt/zenvi-1.2, ...)
    link = os.path.join(work, "zenvi-src-link")
    if not os.path.islink(link):
        os.symlink(SRC, link)
    via_link = os.path.join(link, "classes", "handoff", "remotion", "helper.mjs")
    node = shutil.which("node")
    # cwd = the project, as run_helper does: Remotion keeps its browser under the nearest package.json above the
    # cwd (node_modules/.remotion), so elsewhere it would download another one
    out = subprocess.run([node, via_link, "compositions", "--project", app, "--entry", "src/index.ts", "--bundle-dir",
                          os.path.join(work, "bundle-review")], cwd=app, capture_output=True, text=True, timeout=1200)
    events = [helper.parse_event(line) for line in out.stdout.splitlines()]
    events = [e for e in events if e]
    found = [e for e in events if e["event"] == "result"]
    result["symlinked_helper"] = {"exit": out.returncode, "events": len(events),
                                  "compositions": [c["id"] for c in (found[0]["compositions"] if found else [])]}
    log("helper through a symlinked folder:", result["symlinked_helper"])
    assert out.returncode == 0 and found, out.stdout[-800:] + out.stderr[-800:]
    probe = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); "
                            "from classes.handoff.remotion import helper; print(helper.helper_path())" % link],
                           capture_output=True, text=True, timeout=120)
    result["python_through_link"] = {"helper_path": probe.stdout.strip(), "real": os.path.realpath(via_link)}
    assert probe.stdout.strip() == os.path.realpath(via_link), probe.stdout + probe.stderr
    # (2) a Date default prop: listed as a remotion-date token, rendered as a Date
    listing = importer.list_compositions(app)
    date_card = listing.by_id()["DateCard"]
    result["date_default_prop"] = date_card.default_props.get("when")
    log("DateCard default props:", date_card.default_props)
    assert date_card.default_props.get("when") == "remotion-date:2026-05-01T00:00:00.000Z"
    stills = os.path.join(work, "frames", "review-stills")
    ok = helper.run_helper("still", project_dir=app, entry=listing.project.entry, props=date_card.default_props,
                           options={"composition": "DateCard", "frames": "first", "out_dir": stills + "-date"})
    result["date_still"] = [s["output"] for s in ok.result["stills"]]
    try:  # what the old helper sent back: the ISO string without the token
        helper.run_helper("still", project_dir=app, entry=listing.project.entry,
                          props={"when": "2026-05-01T00:00:00.000Z"},
                          options={"composition": "DateCard", "frames": "first", "out_dir": stills + "-old"})
        result["iso_string_still"] = "rendered (unexpected)"
    except helper.HelperError as exc:
        result["iso_string_still"] = "failed as expected: " + str(exc)[:200]
    log("DateCard with the stored Date:", result["date_still"], "| with a plain ISO string:", result["iso_string_still"])
    assert "failed as expected" in result["iso_string_still"]
    # (3) remotion.config's delayRender timeout reaches the page
    settings = helper.run_helper("still", project_dir=app, entry=listing.project.entry,
                                 options={"composition": "SettingsCard", "frames": "first",
                                          "out_dir": stills + "-settings"})
    result["settings_still"] = [s["output"] for s in settings.result["stills"]]
    log("SettingsCard (throws unless the 123456 ms timeout arrived):", result["settings_still"])
    # (4) the whole linked-clip path: import, then a re-render with the stored props
    editor = make_editor(work)
    receipt = editor.call_receipt("import_remotion_project_tool", project_dir=app, compositions=["DateCard"],
                                  position=0.0)
    assert receipt["status"] == "applied", receipt["summary"]
    clip = receipt["data"]["linked"][0]
    from classes.handoff import linked_media as lm
    link_data = lm.read_link(editor.file(clip["file_id"]))
    result["linked_props"] = lm.link_props(link_data)
    again = editor.call_receipt("rerender_linked_clip_tool", file_id=clip["file_id"])
    result["rerender"] = again["status"]
    log("linked DateCard props:", result["linked_props"], "| re-render:", again["status"], again["summary"][:160])
    assert result["linked_props"]["when"] == "remotion-date:2026-05-01T00:00:00.000Z" and again["status"] == "applied"
    REPORT["helper"] = result
    return result


# ---------------------------------------------------------------------------
# (f) review round 1: "Open as an editable Zenvi project"
# ---------------------------------------------------------------------------

class _InlineJob:
    def __init__(self):
        self.state, self.result, self.error = "done", None, None

    def report(self, *_a):
        pass

    def should_cancel(self):
        return False


def _inline_jobs():
    from classes.handoff import jobs

    def submit(fn, *, label, on_done=None, **_kw):
        job = _InlineJob()
        try:
            job.result = fn(job)
        except Exception as exc:  # noqa: BLE001
            job.state, job.error = jobs.FAILED, exc
        if on_done is not None:
            on_done(job)
        return job

    jobs.submit_job = submit


def part_restored(work, project, export_dir):
    from PyQt5.QtWidgets import QMessageBox
    from classes.handoff.remotion import detect, dialogs
    _inline_jobs()
    remotion_project = detect.inspect_project(export_dir)
    source = detect.zenvi_source_project(export_dir) or remotion_project.name
    open_file = os.path.join(os.path.dirname(export_dir), source + ".zvn")  # what the old default would overwrite
    with open(open_file, "w") as fh:
        json.dump(project, fh)
    before = open(open_file, "rb").read()

    class Proj:
        current_filepath = open_file

        def needs_save(self):
            return True

    class App:
        def __init__(self):
            self.project = Proj()
            self._tr = lambda text: text

    app = App()
    dialogs.get_app = lambda: app
    events, warnings, defaults = [], [], []
    QMessageBox.question = staticmethod(lambda *a, **k: events.append("asked: save first?") or QMessageBox.No)
    QMessageBox.warning = staticmethod(lambda w, title, text: warnings.append(text))
    QMessageBox.information = staticmethod(lambda *a, **k: events.append("info"))
    opened = []

    class Window:
        class OpenProjectSignal:
            @staticmethod
            def emit(path):
                opened.append((path, "signal"))

        @staticmethod
        def open_project(path, interactive=True):
            opened.append((path, interactive))
            return True

        @staticmethod
        def actionSave_trigger():
            events.append("saved")

    # 1) typing the open project's own name: refused, the file untouched
    dialogs.ask_restored_path = lambda w, default: defaults.append(default) or open_file
    dialogs._open_restored(Window, remotion_project)
    result = {"default_name": defaults[0], "open_project": open_file,
              "own_name_refused": warnings[-1] if warnings else None,
              "open_project_untouched": open(open_file, "rb").read() == before, "events_1": list(events)}
    # 2) the proposed name: written after the question, opened once without a second prompt
    events.clear()
    dialogs.ask_restored_path = lambda w, default: default
    dialogs._open_restored(Window, remotion_project)
    written = defaults[0]
    result.update(events_2=list(events), opened=opened[-1] if opened else None, written=os.path.isfile(written))
    log("open as editable:", result)
    assert result["default_name"] != open_file and "is the project open in Zenvi" in (result["own_name_refused"] or "")
    assert result["open_project_untouched"] and opened and opened[-1] == (written, False)
    assert events[0] == "asked: save first?" and events.count("asked: save first?") == 1
    with open(written) as fh:
        restored = json.load(fh)
    same = {k: restored.get(k) == json.loads(json.dumps(project.get(k))) for k in ("files", "clips", "effects", "markers")}
    result["equal_to_original"] = same
    picks = [0, 30, 60, 90, 120]
    a = libopenshot_frames(project, picks, os.path.join(work, "frames", "restored-original"))
    b = libopenshot_frames(restored, picks, os.path.join(work, "frames", "restored-zvn"))
    result["psnr_restored_vs_original"] = [compare(a[n], b[n])[0] for n in picks]
    log("restored .zvn equals the original:", same, "PSNR per frame:", result["psnr_restored_vs_original"])
    assert all(same.values())
    REPORT["restored"] = result
    return result


# ---------------------------------------------------------------------------
# (g) review round 1: exporting into the earlier export
# ---------------------------------------------------------------------------

def _tree(folder):
    import hashlib
    out = {}
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != "node_modules"]
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, folder)
            if os.path.islink(path):
                out[rel] = "link:" + os.readlink(path)
            else:
                with open(path, "rb") as fh:
                    out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def part_update(work, project, export_dir):
    from classes.handoff import jobs
    from classes.handoff.remotion import exporter
    from classes.handoff.timeline_view import TimelineSnapshot
    upd = export_dir + "-update"
    if os.path.isdir(upd):
        shutil.rmtree(upd)
    subprocess.run(["cp", "-Rc", export_dir, upd], check=True)  # APFS clone with node_modules
    editor = make_editor(work)
    editor.store._data = copy.deepcopy(project)
    editor.mark()
    result = {}
    # 1) an unchanged re-export updates in place and the project still works
    receipt = editor.call_receipt("export_to_remotion_tool", output_dir=upd)
    result["unchanged_update"] = {"status": receipt["status"], "mode": receipt["data"].get("mode"),
                                  "node_modules_kept": os.path.isdir(os.path.join(upd, "node_modules", "remotion"))}
    listed = subprocess.run(["npx", "remotion", "compositions", "src/index.ts"], cwd=upd,  # (the list is info level)
                            capture_output=True, text=True)
    result["unchanged_update"]["compositions"] = [ln.split()[0] for ln in listed.stdout.splitlines()
                                                  if ln.strip().startswith("ZenviTimeline")]
    log("unchanged re-export:", result["unchanged_update"])
    assert receipt["status"] == "applied" and result["unchanged_update"]["compositions"] == ["ZenviTimeline"]
    # 2) changes made in the Remotion project: refused, nothing touched
    tl_path = os.path.join(upd, "src", "zenvi", "timeline.json")
    timeline = json.load(open(tl_path))
    next(c for c in timeline["clips"] if c["id"] == "CIMAGE0001")["position"] = 2.0
    json.dump(timeline, open(tl_path, "w"), indent=1)
    with open(os.path.join(upd, "src", "zenvi", "ZenviClip.tsx"), "a") as fh:
        fh.write("// a tweak made in the Remotion project\n")
    card = os.path.join(upd, "public", "zenvi-media", "card.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:size=800x400", "-frames:v", "1",
                    card], check=True)
    before = _tree(upd)
    refused = editor.call("export_to_remotion_tool", output_dir=upd)
    result["refused_over_edits"] = refused[:600]
    result["tree_unchanged_after_refusal"] = _tree(upd) == before
    log("export over Remotion-side edits:", refused[:400])
    assert refused.startswith("Error") and "replace_edits=true" in refused and result["tree_unchanged_after_refusal"]
    # 3) a cancelled update leaves everything as it was (staged, then swapped)
    data = exporter.project_copy(editor.store._data)
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 2

    try:
        exporter.export_project(TimelineSnapshot.from_project(data, None), data, upd, replace_edits=True,
                                should_cancel=cancel)
        result["cancelled_update"] = "finished (unexpected)"
    except jobs.JobCancelled:
        result["cancelled_update"] = "cancelled"
    result["tree_unchanged_after_cancel"] = _tree(upd) == before
    result["no_staging_left"] = not [n for n in os.listdir(upd) if n.startswith(".zenvi-update-")]
    log("cancelled update:", result["cancelled_update"], "unchanged:", result["tree_unchanged_after_cancel"])
    assert result["tree_unchanged_after_cancel"] and result["no_staging_left"]
    # 4) a media folder that is a link (e.g. committed to git pointing at a shared library): never written through
    library = os.path.join(work, "shared-library")
    os.makedirs(library, exist_ok=True)
    with open(os.path.join(library, "keynote.mov"), "wb") as fh:
        fh.write(b"precious")
    media = os.path.join(upd, "public", "zenvi-media")
    aside = media + "-aside"
    os.rename(media, aside)
    os.symlink(library, media)
    linked_out = editor.call("export_to_remotion_tool", output_dir=upd, replace_edits=True)
    result["refused_through_link"] = linked_out[:300]
    result["library_untouched"] = sorted(os.listdir(library)) == ["keynote.mov"]
    os.remove(media)
    os.rename(aside, media)
    log("export through a linked media folder:", linked_out[:300])
    assert linked_out.startswith("Error") and "is a link" in linked_out and result["library_untouched"]
    # 5) replace_edits: the changes are replaced and the project renders
    replaced = editor.call_receipt("export_to_remotion_tool", output_dir=upd, replace_edits=True)
    result["replaced"] = {"status": replaced["status"], "warnings": replaced["data"].get("warnings"),
                          "tweak_gone": "a tweak made" not in open(os.path.join(upd, "src", "zenvi",
                                                                                "ZenviClip.tsx")).read()}
    png = os.path.join(work, "frames", "update-frame-45.png")
    still = subprocess.run(["npx", "remotion", "still", "src/index.ts", "ZenviTimeline", png, "--frame=45",
                            "--log=error"], cwd=upd, capture_output=True, text=True)
    zenvi = libopenshot_frames(project, [45], os.path.join(work, "frames", "update-zenvi"))
    result["replaced"]["still"] = still.returncode
    result["replaced"]["psnr_frame_45_without_title_text"] = compare(zenvi[45], png, exclude=[TITLE_STRIP])[0] \
        if still.returncode == 0 else None
    log("replace_edits:", result["replaced"])
    assert replaced["status"] == "applied" and result["replaced"]["tweak_gone"] and still.returncode == 0
    REPORT["update"] = result
    return result


# ---------------------------------------------------------------------------
# (h) verification round: dynamic compositions, the trust gate on re-render / Studio, saving mid-render
# ---------------------------------------------------------------------------

DYNAMIC_ROOT = """import React from 'react';
import {Composition} from 'remotion';
import {z} from 'zod';
import {zColor} from '@remotion/zod-types';
import {TitleCard} from './TitleCard';

export const titleCardSchema = z.object({title: z.string(), subtitle: z.string(), accent: zColor()});

// Compositions from data: no literal id for a static scan to find.
const templates = [
  {id: 'IntroCard', title: 'Intro from data', accent: '#3FA7FF'},
  {id: 'OutroCard', title: 'Outro from data', accent: '#FFB23F'},
];

export const RemotionRoot: React.FC = () => (
  <>
    {templates.map((t) => (
      <Composition key={t.id} id={t.id} component={TitleCard} durationInFrames={60} fps={30} width={1920}
        height={1080} schema={titleCardSchema}
        defaultProps={{title: t.title, subtitle: 'registered with templates.map', accent: t.accent}} />
    ))}
  </>
);
"""


def part_dynamic(work, app_dir):
    from classes.handoff import jobs
    from classes.handoff import linked_media as lm
    from classes.handoff.remotion import dialogs, provider, sources, studio, trust
    os.makedirs(os.path.join(work, "frames"), exist_ok=True)
    app = os.path.join(work, "app-dynamic")
    if not os.path.isdir(app):
        subprocess.run(["cp", "-Rc", app_dir, app], check=True)  # APFS clone, node_modules included
        with open(os.path.join(app, "src", "Root.tsx"), "w") as fh:
            fh.write(DYNAMIC_ROOT)
    result = {"static_scan": sorted(sources.scan_project(app, "src/index.ts"))}
    assert result["static_scan"] == [], result
    # 1) File > Import Project with the dialog code: the trust question, then Remotion lists them
    _inline_jobs()
    trust.reset()
    asked, shown, infos = [], [], []
    dialogs.ask_trust = lambda window, project, install=False: asked.append(project.name) or "run"
    dialogs._show_import_dialog = lambda window, listing, key: shown.append(listing)
    from PyQt5.QtWidgets import QMessageBox
    QMessageBox.information = staticmethod(lambda *a, **k: infos.append(a[2] if len(a) > 2 else a))
    QMessageBox.warning = staticmethod(lambda *a, **k: infos.append(a[2] if len(a) > 2 else a))
    t0 = time.time()
    dialogs.read_project(None, app)
    result["dialog"] = {"asked": asked, "messages": infos, "seconds": round(time.time() - t0, 1),
                        "compositions": [(c.id, c.width, c.height, c.duration_frames, c.default_props.get("title"))
                                         for c in (shown[0].compositions if shown else [])]}
    log("dynamic compositions through the import dialog:", result["dialog"])
    assert asked and not infos and [c[0] for c in result["dialog"]["compositions"]] == ["IntroCard", "OutroCard"]
    # 2) import IntroCard as a linked clip; the project is saved under a new name while it renders
    editor = make_editor(work)
    editor.mark()

    def save_as_later():
        time.sleep(3.0)
        editor.store.current_filepath = os.path.join(work, "saved-mid-render.zvn")

    threading.Thread(target=save_as_later, daemon=True).start()
    receipt = editor.call_receipt("import_remotion_project_tool", project_dir=app, compositions=["IntroCard"],
                                  position=0.0)
    log("import IntroCard (saved mid-render):", receipt["status"], receipt["summary"][:200])
    assert receipt["status"] == "applied", receipt["summary"]
    clip = receipt["data"]["linked"][0]
    path = editor.file(clip["file_id"])["path"]
    png = openshot_frame_png(path, 30, os.path.join(work, "frames", "dynamic-intro-f29.png"))
    over_black(png, os.path.join(work, "frames", "dynamic-intro-f29-on-black.png"))
    result["import"] = {"codec": clip["codec"], "undo_steps": editor.undo_steps_since_mark(),
                        "saved_to": editor.store.current_filepath, "alpha": alpha_at(png, [(5, 5)])[1]}
    # 3) a received project nobody trusted this session: the menus' re-render / Open in Studio ask first
    trust.reset()
    questions = []
    answers = {"value": False}

    def asker(root, name, action, key):
        questions.append((name, action, threading.current_thread().name))
        return answers["value"]

    trust.set_asker(asker)
    renders_before = sorted(os.listdir(os.path.dirname(path)))

    def on_executor(fn, name):
        box = {}

        def run():
            try:
                box["result"] = fn()
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        th = threading.Thread(target=run, name=name)
        th.start()
        th.join(1800)
        return box

    declined = on_executor(lambda: lm.rerender_many([clip["file_id"]]), "handoff_0")
    studio_declined = on_executor(lambda: provider.RemotionProvider().open_studio(lm.read_link(
        editor.file(clip["file_id"]))), "handoff-ui_0")
    result["declined"] = {"rerender": type(declined.get("error")).__name__,
                          "studio": type(studio_declined.get("error")).__name__,
                          "questions": list(questions), "new_files": sorted(set(os.listdir(os.path.dirname(path)))
                                                                           - set(renders_before)),
                          "studios": studio.running_studios(), "chrome": chrome_children(app)}
    log("declined trust:", result["declined"])
    assert isinstance(declined.get("error"), jobs.JobCancelled) and not result["declined"]["new_files"]
    assert isinstance(studio_declined.get("error"), jobs.JobCancelled) and not result["declined"]["studios"]
    answers["value"] = True
    accepted = on_executor(lambda: lm.rerender_many([clip["file_id"]], props={"title": "Re-rendered after yes"}),
                           "handoff_0")
    result["accepted"] = {"error": repr(accepted.get("error")), "swapped": (accepted.get("result") or {}).get(
        "swapped"), "questions": len(questions)}
    log("accepted trust:", result["accepted"])
    assert accepted.get("error") is None and len(questions) == 3
    # 4) the agent tool keeps its documented behaviour: no question
    tool = editor.call_receipt("rerender_linked_clip_tool", file_id=clip["file_id"])
    result["tool_rerender"] = {"status": tool["status"], "questions": len(questions)}
    log("rerender_linked_clip_tool:", result["tool_rerender"])
    assert tool["status"] == "applied" and len(questions) == 3
    trust.set_asker(None)
    REPORT["dynamic"] = result
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work", required=True)
    parser.add_argument("--remotion-app", required=True)
    parser.add_argument("--video-matrix", choices=["bt709", "bt601"], default="bt709",
                        help="colour tags of the test video (libopenshot 1.0 decodes both as BT.601)")
    parser.add_argument("parts", nargs="+", choices=["linked", "export", "reimport", "edits", "helper", "restored",
                                                      "update", "dynamic", "all"])
    args = parser.parse_args()
    work = os.path.abspath(args.work)
    os.makedirs(work, exist_ok=True)
    boot()
    everything = {"linked", "export", "reimport", "edits", "helper", "restored", "update", "dynamic"}
    parts = everything if "all" in args.parts else set(args.parts)
    try:
        if "linked" in parts:
            part_linked(work, os.path.abspath(args.remotion_app))
        if "helper" in parts:
            part_helper(work, os.path.abspath(args.remotion_app))
        if "dynamic" in parts:
            part_dynamic(work, os.path.abspath(args.remotion_app))
        if parts & {"export", "reimport", "edits", "restored", "update"}:
            project, export_dir = part_export(work, os.path.abspath(args.remotion_app), args.video_matrix)
            if "reimport" in parts:
                part_reimport(work, project, export_dir)
            if "edits" in parts:
                part_edits(work, project, export_dir)
            if "restored" in parts:
                part_restored(work, project, export_dir)
            if "update" in parts:
                part_update(work, project, export_dir)
    finally:
        REPORT.pop("export_project", None)
        with open(os.path.join(work, "report.json"), "w") as fh:
            json.dump(REPORT, fh, indent=1, default=str)
        log("report:", os.path.join(work, "report.json"))
    os._exit(0)  # libopenshot / Qt teardown order is not our concern here


if __name__ == "__main__":
    main()
