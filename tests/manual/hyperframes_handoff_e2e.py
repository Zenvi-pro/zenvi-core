#!/usr/bin/env python3
"""Real end-to-end check of the HyperFrames <-> Zenvi handoff (not part of the pytest suite).

Runs the real HyperFrames CLI (Node + headless Chrome + ffmpeg) and libopenshot 1.0 in the headless
editor harness (real ProjectDataStore, UpdateManager and undo; the timeline widget is the tests'
FakeTimeline), driving the real editor tools:

  init      ``npx hyperframes@0.8.126 init`` a project, then give it media primitives (video, alpha
            PNG, audio), a nested composition with variables, a root title and GSAP tweens; lint it.
  import    import_hyperframes_project_tool in native mode (one undo step; renders the composition
            and the title layer as linked ProRes 4444) and in flatten mode (HyperFrames' own render);
            decode the overlays with libopenshot (alpha kept); compare libopenshot's frames of the
            native import with the flattened render (PSNR, mean |diff|); change a variable with
            update_linked_clip_tool (re-render, one undo step); start the Studio; cancel a render.
  export    a Zenvi project (trimmed video fading in, PNG with an eased move + rotation, SVG title,
            audio fade) -> export_to_hyperframes_tool -> hyperframes lint -> hyperframes render
            (PNG sequence) -> compare with libopenshot's frames of the same project.
  reimport  import the export back -> the clips, files and markers equal the original.

Usage (heavy: renders go through the machine-wide lock)::

    QT_QPA_PLATFORM=offscreen PYTHONPATH=$HOME/zenvi-deps-1.0/python ZENVI_HYPERFRAMES_WORKERS=1 \\
      ~/Projects/zenvi-worktrees/.handoff/bin/heavy.sh .venv/bin/python tests/manual/hyperframes_handoff_e2e.py \\
      --work /tmp/hf-e2e all

``ZENVI_HYPERFRAMES_CLI`` may point at an installed CLI (else ``npx hyperframes@0.8.126``). Results go to
``<work>/report.json``; images to ``<work>/frames`` (look at them).
"""

from __future__ import annotations

import argparse
import copy
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
SRC = os.path.join(os.path.dirname(TESTS), "src")
REPORT: dict = {}
_KEEP: list = []
QUIET = {"HYPERFRAMES_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1", "HYPERFRAMES_NO_UPDATE_CHECK": "1",
         "HYPERFRAMES_NO_AUTO_INSTALL": "1", "HYPERFRAMES_SKIP_SKILLS": "1", "NO_COLOR": "1"}
SAMPLE_TIMES = (0.0, 0.5, 1.2, 2.0, 3.0, 3.3, 3.7, 4.5, 5.5)


def log(*parts):
    print("[e2e]", *parts, flush=True)


def boot():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path[:0] = [SRC, TESTS]
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    _KEEP.append(app)
    import openshot  # the real libopenshot (PYTHONPATH)
    from classes import tool_handlers
    tool_handlers.QThread = None  # no event loop here: GUI hops run inline, like the headless suite
    log("libopenshot", openshot.OPENSHOT_VERSION_FULL)
    return app


_EDITORS: list = []


def make_editor(work, name="user"):
    """A fresh harness; the previous one is stopped first (each points get_app() at itself)."""
    from classes import info
    from editor_tools_harness import make_editor as _make
    from titles_text_fakes import FakeFilesModel, FakeTimeline
    while _EDITORS:
        _EDITORS.pop().stop()
    info.USER_PATH = os.path.join(work, name)
    os.makedirs(info.USER_PATH, exist_ok=True)
    editor = _make()
    _EDITORS.append(editor)
    editor.window.timeline = FakeTimeline(editor)
    editor.window.files_model = FakeFilesModel(editor)
    import classes.handoff.hyperframes  # noqa: F401  (registers the provider)
    return editor


def hf_argv(*args):
    from classes.handoff.hyperframes import cli as hf_cli
    return hf_cli.resolve_cli(None).command(*args)


def run_hf(args, cwd, timeout=1800):
    env = dict(os.environ, **QUIET)
    t0 = time.time()
    proc = subprocess.run(hf_argv(*args), cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout + proc.stderr, round(time.time() - t0, 1)


def ffprobe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=codec_name,pix_fmt,width,height,r_frame_rate,nb_frames:format=duration",
                          "-of", "json", path], capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    stream = data["streams"][0]
    stream["duration"] = (data.get("format") or {}).get("duration")
    return stream


def openshot_frame_png(path, frame, out_png):
    import openshot
    reader = openshot.FFmpegReader(path)
    reader.Open()
    try:
        reader.GetFrame(frame).Save(out_png, 1.0, "PNG", 100)
    finally:
        reader.Close()
    return out_png


def libopenshot_frames(project, times, out_dir, prefix):
    import openshot
    os.makedirs(out_dir, exist_ok=True)
    fps = project["fps"]
    t = openshot.Timeline(project["width"], project["height"], openshot.Fraction(fps["num"], fps["den"]), 48000, 2,
                          openshot.LAYOUT_STEREO)
    t.SetJson(json.dumps(project))
    t.Open()
    out = {}
    rate = fps["num"] / fps["den"]
    try:
        for s in times:
            n = int(round(s * rate))
            path = os.path.join(out_dir, "%s-%05.2f.png" % (prefix, s))
            t.GetFrame(n + 1).Save(path, 1.0, "PNG", 100)
            out[s] = path
    finally:
        t.Close()
    return out


def _flat(png, size=None):
    from PIL import Image
    img = Image.open(png).convert("RGBA")
    if size is not None and img.size != size:
        img = img.resize(size)
    return Image.alpha_composite(Image.new("RGBA", img.size, (0, 0, 0, 255)), img).convert("RGB")


def compare(a_png, b_png, exclude=()):
    from PIL import ImageChops, ImageStat
    a = _flat(a_png)
    b = _flat(b_png, a.size)
    for region in exclude:
        a.paste((0, 0, 0), region)
        b.paste((0, 0, 0), region)
    diff = ImageChops.difference(a, b)
    stat = ImageStat.Stat(diff)
    mad = sum(stat.mean) / 3.0
    mse = sum(s / stat.count[i] for i, s in enumerate(stat.sum2)) / 3.0
    psnr = float("inf") if mse == 0 else 10 * math.log10(255.0 ** 2 / mse)
    return round(psnr, 2), round(mad, 3), diff


def side_by_side(a_png, b_png, diff, out):
    from PIL import Image, ImageOps
    a = _flat(a_png)
    b = _flat(b_png, a.size)
    d = ImageOps.autocontrast(diff.convert("L")).convert("RGB")
    w, h = a.size
    sheet = Image.new("RGB", (w * 3 // 2, h // 2), (40, 40, 40))
    for i, img in enumerate((a, b, d)):
        sheet.paste(img.resize((w // 2, h // 2)), (i * w // 2, 0))
    sheet.save(out)


def alpha_stats(png, points):
    from PIL import Image
    img = Image.open(png).convert("RGBA")
    return [img.getpixel(p)[3] for p in points], img.getchannel("A").getextrema()


def chrome_pids():
    """pid -> command of every running chrome-headless-shell (HyperFrames' and anyone else's)."""
    ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    out = {}
    for line in ps.splitlines():
        if "chrome-headless-shell" in line and "grep" not in line:
            pid, _sep, cmd = line.strip().partition(" ")
            out[int(pid)] = cmd[:160]
    return out


# ---------------------------------------------------------------------------
# init: a real HyperFrames project with primitives, a composition and tweens
# ---------------------------------------------------------------------------

LOWER_THIRD = """<html data-composition-variables='[{"id":"headline","type":"string","label":"Headline","default":"Hello"},
  {"id":"accent","type":"color","label":"Accent","default":"#FF5A36"}]'>
<body>
<template id="lower-third-template">
  <div data-composition-id="lower-third" data-width="1920" data-height="1080">
    <div class="card"><span class="headline"></span></div>
    <style>
      [data-composition-id="lower-third"] .card { position: absolute; left: 192px; top: 760px; padding: 24px 40px;
        background: rgba(11, 11, 15, 0.6); border-radius: 12px; }
      [data-composition-id="lower-third"] .headline { font: 700 54px Inter, sans-serif; color: #F7F7F5; }
    </style>
    <script>
      const vars = window.__hyperframes.getVariables();
      document.querySelector('[data-composition-id="lower-third"] .headline').textContent = vars.headline;
      document.querySelector('[data-composition-id="lower-third"] .card').style.borderLeft = "8px solid " + vars.accent;
      const tl = gsap.timeline({ paused: true });
      tl.from('[data-composition-id="lower-third"] .card', { opacity: 0, x: -60, duration: 0.6, ease: "expo.out" }, 0);
      tl.to('[data-composition-id="lower-third"] .card', { opacity: 0, duration: 0.4, ease: "power2.in" }, 2.2);
      window.__timelines["lower-third"] = tl;
    </script>
  </div>
</template>
</body>
</html>
"""

INDEX = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=1920, height=1080" />
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
      * { margin: 0; padding: 0; box-sizing: border-box; }
      html, body { margin: 0; width: 1920px; height: 1080px; overflow: hidden; background: #000000; }
      #root { position: relative; width: 100%; height: 100%; }
      .clip { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
      #title { color: #F7F7F5; font: 700 96px Inter, sans-serif; display: flex; align-items: center;
               justify-content: center; }
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="6" data-width="1920" data-height="1080">
      <video id="bg" class="clip" src="assets/clip.mp4" data-start="0" data-duration="4" data-track-index="0"
             muted playsinline></video>
      <img id="logo" class="clip" src="assets/logo.png" data-start="bg - 1.5" data-duration="2.5" data-track-index="1"
           style="left: 1300px; top: 120px; width: 400px; height: 200px" />
      <audio id="music" src="assets/music.wav" data-start="0" data-duration="6" data-volume="0.5" data-fade-out="1"
             data-track-index="2"></audio>
      <div id="lower" data-composition-id="lower-third" data-composition-src="compositions/lower-third.html"
           data-start="0.5" data-track-index="3" data-variable-values='{"headline":"Launch day"}'></div>
      <h1 id="title" class="clip" data-start="4" data-duration="2" data-track-index="4">Made with Zenvi</h1>
    </div>
    <script>
      const tl = gsap.timeline({ paused: true });
      tl.fromTo("#logo", { opacity: 0, x: -60 }, { opacity: 1, x: 0, duration: 0.6, ease: "power2.out" }, 2.5);
      tl.to("#bg", { opacity: 0, duration: 0.5, ease: "power1.out" }, 3.5);
      tl.from("#title", { opacity: 0, y: 24, duration: 0.6, ease: "expo.out" }, 4);
      window.__timelines["main"] = tl;
    </script>
  </body>
</html>
"""


def make_media(folder):
    os.makedirs(folder, exist_ok=True)
    clip = os.path.join(folder, "clip.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", "-colorspace", "smpte170m",
                    "-color_primaries", "smpte170m", "-color_trc", "smpte170m", clip], check=True)
    logo = os.path.join(folder, "logo.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xFF5A36:s=300x100", "-vf",
                    "format=rgba,pad=400:200:50:50:color=black@0", "-frames:v", "1", logo], check=True)
    music = os.path.join(folder, "music.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=6",
                    music], check=True)
    return clip, logo, music


def part_init(work):
    root = os.path.join(work, "sample")
    shutil.rmtree(root, ignore_errors=True)
    code, out, secs = run_hf(["init", "sample", "--non-interactive"], work)
    log("init:", code, "%.1fs" % secs, out.strip().splitlines()[-3:] if out.strip() else "")
    result = {"init_code": code, "init_seconds": secs, "files": sorted(os.listdir(root)) if os.path.isdir(root) else []}
    make_media(os.path.join(root, "assets"))
    os.makedirs(os.path.join(root, "compositions"), exist_ok=True)
    with open(os.path.join(root, "compositions", "lower-third.html"), "w") as fh:
        fh.write(LOWER_THIRD)
    with open(os.path.join(root, "index.html"), "w") as fh:
        fh.write(INDEX)
    code, out, secs = run_hf(["lint", "--json"], root)
    lint = json.loads(out[out.index("{"):]) if "{" in out else {"raw": out[-400:]}
    result["lint"] = {"code": code, "ok": lint.get("ok"), "errors": lint.get("errorCount"),
                      "warnings": lint.get("warningCount"),
                      "findings": [f.get("code") for f in lint.get("findings") or []]}
    log("lint:", result["lint"])
    REPORT["init"] = result
    return root


# ---------------------------------------------------------------------------
# import: native + flatten, frames compared with libopenshot
# ---------------------------------------------------------------------------

def part_import(work, project_dir):
    from classes.handoff import jobs
    from classes.handoff import linked_media as lm
    from classes.handoff.hyperframes import cli as hf_cli
    from classes.handoff.hyperframes import provider as hfprov
    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)
    result = {}

    editor = make_editor(work, "user-native")
    editor.mark()
    t0 = time.time()
    r = editor.call_receipt("import_hyperframes_project_tool", project_dir=project_dir, mode="native", position=0.0)
    result["native_seconds"] = round(time.time() - t0, 1)
    log("native import:", r["status"], r["summary"][:300], "(%.1fs)" % result["native_seconds"])
    assert r["status"] == "applied", r
    d = r["data"]
    result["native"] = {"mode": d["mode"], "undo_steps": editor.undo_steps_since_mark(), "native": d["native"],
                        "linked": d["linked"], "warnings": d["warnings"], "problems": d["problems"],
                        "timing_from": d["project"]["timing_from"], "cli": d["project"]["hyperframes_cli"],
                        "clips": [{k: c.get(k) for k in ("kind", "role", "element", "name", "layer", "position", "end",
                                                         "codec")} for c in d["clips"]]}
    linked = {}
    for c in d["clips"]:
        if c["kind"] != "linked":
            continue
        f = editor.file(c["file_id"])
        info = ffprobe(f["path"])
        linked[c["role"]] = {"path": f["path"], "ffprobe": info, "state": lm.check_link(f).state,
                             "codec": lm.read_link(f)["render"]["codec"], "file_id": c["file_id"],
                             "source": lm.read_link(f)["source"]}
        log(c["role"], linked[c["role"]]["codec"], info, linked[c["role"]]["state"])
    result["linked"] = linked
    # the overlay keeps its alpha through libopenshot's decoder
    lower = linked["composition"]["path"]
    png = openshot_frame_png(lower, 31, os.path.join(frames, "lower-third-f30.png"))
    corner, extrema = alpha_stats(png, [(5, 5), (1900, 1070)])
    result["lower_third_alpha"] = {"corners": corner, "extrema": extrema}
    log("lower third alpha", result["lower_third_alpha"])
    native_project = copy.deepcopy(editor.store._data)
    native_frames = libopenshot_frames(native_project, SAMPLE_TIMES, frames, "native")

    # change a variable of the composition: re-render, one undo step
    fid = linked["composition"]["file_id"]
    before = editor.file(fid)["path"]
    editor.mark()
    t0 = time.time()
    up = editor.call_receipt("update_linked_clip_tool", file_id=fid, props={"headline": "Ship it"})
    result["rerender"] = {"status": up["status"], "summary": up["summary"][:200], "seconds": round(time.time() - t0, 1),
                          "undo_steps": editor.undo_steps_since_mark(),
                          "path_changed": editor.file(fid)["path"] != before,
                          "props": lm.link_props(lm.read_link(editor.file(fid)))}
    log("update:", result["rerender"])
    if up["status"] == "applied":
        openshot_frame_png(editor.file(fid)["path"], 31, os.path.join(frames, "lower-third-ship-it-f30.png"))
        editor.undo()
        result["rerender"]["undo_restores"] = editor.file(fid)["path"] == before
        editor.redo()

    # Studio: a real preview server, reused, stopped
    t0 = time.time()
    studio = hf_cli.start_studio(project_dir)
    code = urllib.request.urlopen(studio.url.split("#")[0], timeout=30).status
    again = hf_cli.start_studio(project_dir)
    result["studio"] = {"url": studio.url, "http": code, "seconds": round(time.time() - t0, 1),
                        "reused": again is studio, "stopped": hf_cli.stop_studios()}
    studio.proc.wait(timeout=20)
    result["studio"]["exited"] = studio.proc.poll() is not None
    log("studio:", result["studio"])

    # cancel a render: JobCancelled, no wrapper folder, no headless Chrome left behind
    link = lm.read_link(editor.file(fid))
    stop = threading.Event()
    threading.Timer(12.0, stop.set).start()
    staging = os.path.join(work, "cancel-staging")
    os.makedirs(staging, exist_ok=True)
    cancelled = False
    chrome_before = chrome_pids()
    t0 = time.time()
    try:
        hfprov.HyperFramesProvider().render(link, staging, on_progress=lambda f, m: None, should_cancel=stop.is_set)
    except jobs.JobCancelled:
        cancelled = True
    time.sleep(2.0)
    result["cancel"] = {"raised_job_cancelled": cancelled, "seconds": round(time.time() - t0, 1),
                        "wrappers_left": [n for n in os.listdir(project_dir) if n.startswith(".render-")],
                        "chrome_started_and_left": {p: c for p, c in chrome_pids().items() if p not in chrome_before},
                        "staging": sorted(os.listdir(staging))}
    log("cancel:", result["cancel"])

    # flatten: HyperFrames' own render as one linked clip (a fresh project)
    flat = make_editor(work, "user-flat")
    t0 = time.time()
    rf = flat.call_receipt("import_hyperframes_project_tool", project_dir=project_dir, mode="flatten", position=0.0)
    result["flatten_seconds"] = round(time.time() - t0, 1)
    log("flatten import:", rf["status"], rf["summary"][:200], "(%.1fs)" % result["flatten_seconds"])
    (fc,) = rf["data"]["clips"]
    ff = flat.file(fc["file_id"])
    result["flatten"] = {"codec": lm.read_link(ff)["render"]["codec"], "ffprobe": ffprobe(ff["path"]),
                         "state": lm.check_link(ff).state, "undo_steps": flat.undo_steps_since_mark()}
    flat_frames = libopenshot_frames(copy.deepcopy(flat.store._data), SAMPLE_TIMES, frames, "flat")
    cmp = {}
    for s in SAMPLE_TIMES:
        psnr, mad, diff = compare(native_frames[s], flat_frames[s])
        cmp["%.2f" % s] = {"psnr": psnr, "mad": mad}
        side_by_side(native_frames[s], flat_frames[s], diff, os.path.join(frames, "import-%05.2f.png" % s))
    result["native_vs_flatten_in_zenvi"] = cmp
    log("native vs flatten (both as libopenshot shows them):", cmp)
    REPORT["import"] = result


# ---------------------------------------------------------------------------
# export: a Zenvi project rendered by HyperFrames and by libopenshot
# ---------------------------------------------------------------------------

TITLE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">
  <rect x="192" y="780" width="12" height="150" fill="#FF5A36"/>
  <text x="230" y="860" font-family="Helvetica, Arial, sans-serif" font-size="96" font-weight="700" fill="#F7F7F5">Zenvi to HyperFrames</text>
</svg>
"""
TITLE_STRIP = (150, 740, 1500, 960)


def _kf(*points):
    out = []
    for p in points:
        interp = p[2] if len(p) > 2 else 0
        hl = p[3] if len(p) > 3 else (0.5, 1.0)
        hr = p[4] if len(p) > 4 else (0.5, 0.0)
        out.append({"co": {"X": float(p[0]), "Y": float(p[1])}, "interpolation": interp, "handle_type": 0,
                    "handle_left": {"X": hl[0], "Y": hl[1]}, "handle_right": {"X": hr[0], "Y": hr[1]}})
    return {"Points": out}


def build_fixture_project(work):
    import openshot
    from classes.image_types import get_media_type
    from windows.models.files_model import inspect_media
    media = os.path.join(work, "zmedia")
    clip, logo, music = make_media(media)
    card = os.path.join(media, "card.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=800x400:rate=1", "-frames:v",
                    "1", card], check=True)
    title = os.path.join(media, "title.svg")
    with open(title, "w") as fh:
        fh.write(TITLE_SVG)
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
        c = json.loads(openshot.Clip(fdata["path"]).Json())
        c.update(id=cid, file_id=fdata["id"], title=fdata["name"], reader=copy.deepcopy(fdata), layer=layer,
                 position=position, start=start, end=end, duration=float(fdata.get("duration") or end))
        c.update(props)
        clips.append(c)
        return c

    fv = add_file("FVIDEO0001", clip, "clip.mp4")
    fc = add_file("FCARD00001", card, "card.png")
    ft = add_file("FTITLE0001", title, "Title")
    fm = add_file("FMUSIC0001", music, "music.wav")
    add_clip("CVIDEO0001", fv, 1000000, 0.0, 0.5, 4.0, alpha=_kf((16, 0.0), (31, 1.0)))
    add_clip("CCARD00001", fc, 2000000, 0.5, 0.0, 3.0, scale_x=_kf((1, 0.4)), scale_y=_kf((1, 0.4)),
             location_x=_kf((1, -0.3), (61, 0.2, 0, (0.3, 1.0), (0.16, 1.0))), location_y=_kf((1, -0.15)),
             rotation=_kf((1, 0.0), (61, 10.0)))
    add_clip("CTITLE0001", ft, 3000000, 1.0, 0.0, 2.5, alpha=_kf((1, 0.0), (16, 1.0, 1)))
    add_clip("CMUSIC0001", fm, 4000000, 0.0, 0.0, 3.5, volume=_kf((1, 1.0), (61, 1.0, 1), (106, 0.0, 1)))
    project["files"], project["clips"], project["effects"] = files, clips, []
    project["markers"] = [{"id": "MARK000001", "position": 2.0, "name": "Hold", "vector": "blue"}]
    return project


def part_export(work):
    editor = make_editor(work, "user-export")
    project = build_fixture_project(work)
    editor.store._data = copy.deepcopy(project)
    editor.mark()
    out = os.path.join(work, "zenvi-to-hyperframes")
    shutil.rmtree(out, ignore_errors=True)
    t0 = time.time()
    r = editor.call_receipt("export_to_hyperframes_tool", output_dir=out)
    result = {"export_seconds": round(time.time() - t0, 1), "status": r["status"],
              "undo_steps": editor.undo_steps_since_mark()}
    log("export:", r["status"], r["summary"][:300])
    d = r["data"]
    result.update(clips=d["clips"], duration=d["duration"], warnings=d["warnings"], lint=d["lint"],
                  files=d["files"])
    seq = os.path.join(work, "frames", "hf-seq")
    shutil.rmtree(seq, ignore_errors=True)
    args = ["render", out, "--format", "png-sequence", "--output", seq, "--workers",
            os.environ.get("ZENVI_HYPERFRAMES_WORKERS", "1")]
    code, text, secs = run_hf(args, out)
    result["render"] = {"code": code, "seconds": secs, "tail": [x for x in text.splitlines() if x.strip()][-4:]}
    log("render:", result["render"])
    pngs = sorted(glob.glob(os.path.join(seq, "**", "*.png"), recursive=True))
    result["render"]["frames"] = len(pngs)
    times = (0.0, 0.3, 0.5, 0.8, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 3.4)
    ref = libopenshot_frames(project, times, os.path.join(work, "frames"), "zenvi")
    cmp = {}
    for s in times:
        n = int(round(s * 30))
        if n >= len(pngs):
            continue
        whole = compare(ref[s], pngs[n])
        no_text = compare(ref[s], pngs[n], exclude=(TITLE_STRIP,))
        cmp["%.2f" % s] = {"psnr": whole[0], "mad": whole[1], "psnr_without_title": no_text[0],
                           "mad_without_title": no_text[1]}
        side_by_side(ref[s], pngs[n], whole[2], os.path.join(work, "frames", "export-%05.2f.png" % s))
    result["zenvi_vs_hyperframes"] = cmp
    log("zenvi vs hyperframes:", cmp)
    REPORT["export"] = result
    REPORT["_export_project"] = project
    return out, project


def part_reimport(work, out, project):
    editor = make_editor(work, "user-reimport")
    editor.mark()
    r = editor.call_receipt("import_hyperframes_project_tool", project_dir=out, position=0.0)
    log("reimport:", r["status"], r["summary"][:300])
    restored = {c["id"]: c for c in editor.store._data["clips"]}
    original = {c["id"]: c for c in project["clips"]}
    diffs = sorted(k for cid in original for k in set(original[cid]) | set(restored.get(cid, {}))
                   if original[cid].get(k) != restored.get(cid, {}).get(k))
    files_equal = all(any(f.get("path") == g.get("path") for g in editor.store._data["files"]) for f in project["files"])
    REPORT["reimport"] = {"status": r["status"], "mode": r["data"]["mode"], "undo_steps": editor.undo_steps_since_mark(),
                          "clip_ids_equal": set(restored) == set(original), "differing_clip_keys": diffs,
                          "files_equal": files_equal,
                          "markers_equal": editor.store._data.get("markers") == project["markers"]}
    log("reimport:", REPORT["reimport"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("parts", nargs="+", choices=["init", "import", "export", "reimport", "all"])
    args = ap.parse_args()
    work = os.path.abspath(args.work)
    os.makedirs(work, exist_ok=True)
    boot()
    parts = {"init", "import", "export", "reimport"} if "all" in args.parts else set(args.parts)
    try:
        project_dir = os.path.join(work, "sample")
        if "init" in parts:
            project_dir = part_init(work)
        if "import" in parts:
            part_import(work, project_dir)
        if "export" in parts or "reimport" in parts:
            out, project = part_export(work)
            if "reimport" in parts:
                part_reimport(work, out, project)
    finally:
        REPORT.pop("_export_project", None)
        with open(os.path.join(work, "report.json"), "w") as fh:
            json.dump(REPORT, fh, indent=2, default=str)
        log("report:", os.path.join(work, "report.json"))


if __name__ == "__main__":
    main()
