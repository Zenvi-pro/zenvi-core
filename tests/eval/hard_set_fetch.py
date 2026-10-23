"""Fetch candidate clips for the hard test: confusable themes (4 clips each) and continuous long takes.

Writes hard/raw/<theme>_<n>.mp4 (normalised to 720p/30) and hard/candidates.json; then strips/<clip>.jpg, a row of 6
frames per clip, for labelling. usage: python tests/eval/hard_set_fetch.py <backend_dir> <set_dir> (PEXELS_API_KEY in the backend .env)
"""
import json
import os
import shutil
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request

import certifi

CTX = ssl.create_default_context(cafile=certifi.where())
FF = shutil.which("ffmpeg") or "ffmpeg"
FP = shutil.which("ffprobe") or "ffprobe"
backend_dir, QEVAL = sys.argv[1], sys.argv[2]
HARD = os.path.join(QEVAL, "hard")
RAW = os.path.join(HARD, "raw")
DL = os.path.join(QEVAL, "raw")
STRIPS = os.path.join(HARD, "strips")
for d in (RAW, STRIPS):
    os.makedirs(d, exist_ok=True)


def env_key(name):
    for line in open(os.path.join(backend_dir, ".env"), encoding="utf-8"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")


PEXELS = env_key("PEXELS_API_KEY")


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "zenvi-eval/1.0", "Authorization": PEXELS})
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        return json.load(r)


def download(url, path):
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        req = urllib.request.Request(url, headers={"User-Agent": "zenvi-eval/1.0"})
        with urllib.request.urlopen(req, timeout=300, context=CTX) as r, open(path + ".part", "wb") as f:
            while chunk := r.read(1 << 16):
                f.write(chunk)
        os.replace(path + ".part", path)
    return path


def ff(*args):
    subprocess.run([FF, "-nostdin", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def duration(path):
    out = subprocess.run([FP, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(out.stdout.strip() or 0)


# confusable themes: several clips of the same kind of scene, so queries must use details
THEMES = {"dog": "dog playing outdoors", "walk": "person walking city street", "bike": "person riding bicycle",
          "cook": "cooking in kitchen", "beach": "people on beach", "cafe": "people in cafe", "kid": "child playing",
          "car": "car driving road"}
# continuous takes where something happens partway through
LONG = {"long_fetch": "dog fetching ball", "long_skate": "skateboarding", "long_street": "street performer",
        "long_cook": "cooking recipe step", "long_hike": "hiking trail", "long_kids": "kids playing soccer"}

seen = set()
cands = {}


def pick(query, n, min_dur, max_dur):
    data = get_json("https://api.pexels.com/videos/search?" + urllib.parse.urlencode(
        {"query": query, "per_page": 30, "orientation": "landscape", "size": "medium"}))
    out = []
    for v in data.get("videos", []):
        if v["id"] in seen or not (min_dur <= v.get("duration", 0) <= max_dur):
            continue
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and 540 <= (f.get("height") or 0) <= 1080]
        if not files:
            continue
        seen.add(v["id"])
        out.append((v, min(files, key=lambda x: abs((x.get("height") or 0) - 720))))
        if len(out) == n:
            break
    return out


for key, query in THEMES.items():
    for i, (v, f) in enumerate(pick(query, 4, 8, 40)):
        name = f"{key}_{i + 1}"
        dst = os.path.join(RAW, name + ".mp4")
        if not os.path.exists(dst):
            ff("-i", download(f["link"], os.path.join(DL, f"pexels_{v['id']}.mp4")), "-t", "15", "-vf", "scale=-2:720,fps=30,setsar=1",
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", dst)
        cands[name] = {"theme": key, "pexels": v.get("url"), "author": (v.get("user") or {}).get("name")}
for key, query in LONG.items():
    got = pick(query, 1, 30, 120)
    if not got:
        continue
    v, f = got[0]
    dst = os.path.join(RAW, key + ".mp4")
    if not os.path.exists(dst):
        ff("-i", download(f["link"], os.path.join(DL, f"pexels_{v['id']}.mp4")), "-t", "60", "-vf", "scale=-2:720,fps=30,setsar=1",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", dst)
    cands[key] = {"theme": "long", "pexels": v.get("url"), "author": (v.get("user") or {}).get("name")}

for name in cands:
    src = os.path.join(RAW, name + ".mp4")
    d = duration(src)
    cands[name]["duration"] = round(d, 2)
    n = 10 if name.startswith("long_") else 6
    strip = os.path.join(STRIPS, name + ".jpg")
    if not os.path.exists(strip):
        fps = n / d
        ff("-i", src, "-vf", f"fps={fps:.5f},scale=320:-2,drawtext=fontfile='C\\:/Windows/Fonts/arialbd.ttf':text='%{{pts\\:hms}}':"
           f"fontcolor=yellow:fontsize=20:box=1:boxcolor=black@0.7:x=4:y=4,tile={n}x1", "-frames:v", "1", strip)
json.dump(cands, open(os.path.join(HARD, "candidates.json"), "w"), indent=1)
print(len(cands), "clips", sum(1 for c in cands.values() if c["theme"] == "long"), "long")
