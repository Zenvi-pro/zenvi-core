"""Music on this computer: ACE-Step 1.5 (acestep.cpp) and YuE2 (yue2.cpp), GGUF over ggml.

Zenvi never needs a server for this. ``install(engine)`` puts a pinned runtime
build (Zenvi's own CI release) and the pinned GGUF models under
``USER_PATH/music``; ``generate`` writes a request JSON, runs the binaries and
returns the MP3 in ``USER_PATH/Downloads`` (where stock downloads land).

* ACE-Step 1.5 (MIT) is the default. An instrumental bed renders DiT-only
  (about 40 s for 30 s of audio on an Iris Xe); a song with lyrics runs the
  5 Hz LM first and renders its codes.
* YuE2-3B (Q5_K_M) is CC BY-NC 4.0: non-commercial only. Slower (about 3 min
  for a 30 s bed on an Iris Xe at 16 steps) but full songs with vocals.

Both run on the best ggml device (Vulkan, Metal, CUDA, else CPU). The VAE is
tiled small: on an iGPU the default tile fails to pin memory and a 30 s decode
takes minutes instead of seconds.
"""

from __future__ import annotations

import json
import os
import platform as _platform
import subprocess
import sys
import time
import zipfile
from typing import List, Optional, Tuple

from classes import hyperframes_local as hf
from classes import info
from classes.logger import log

ENGINES = ("ace_step", "yue2")

ACE_REPO, ACE_REVISION = "Serveurperso/ACE-Step-1.5-GGUF", "666ac70204440867d8c01ba4b119cc79c95b370a"
ACE_DIT, ACE_LM = "acestep-v15-turbo-Q8_0.gguf", "acestep-5Hz-lm-1.7B-Q8_0.gguf"
ACE_TEXT, ACE_VAE = "Qwen3-Embedding-0.6B-Q8_0.gguf", "vae-BF16.gguf"
YUE_REPO, YUE_REVISION = "Serveurperso/YuE2-GGUF", "e630f2b8f6aedf8254c3186438633c1b5eeaf80a"
YUE_MODEL, YUE_VAE = "YuE2-3B-Q5_K_M.gguf", "YuE2-Vae-F32.gguf"

MODELS = {              # engine -> [(file, sha256, bytes)], sha256 = the Hugging Face LFS oid
    "ace_step": [
        (ACE_DIT, "288f708a61cfc241013a98a62f98ba331f83fe34d0d3559acdd9b0f6a2f7cd6b", 2549528000),
        (ACE_LM, "726f99a82f050b32ebc5c5e36acaa9d7acd06bfe5e579a62b00e0d0d6d0b7ec6", 1975837568),
        (ACE_TEXT, "972f23255e46adfe744a0eb9a0039f3c63988f65753b0968d776e8b27168c321", 784144960),
        (ACE_VAE, "0599862ac5d15cd308e1d2e368373aea6c02e25ebd1737ad4a4562a0901b0ef8", 337420928),
    ],
    "yue2": [
        (YUE_MODEL, "cd3efd250b734a229172800f08f33a893ad1f66b521666e176cdae4a0729b281", 2622936832),
        (YUE_VAE, "93e49dfb1970e89ad64cacb17cf13b5d05f6bb30ef7ed3adae3050bcb728638a", 530497344),
    ],
}
_MODEL_SOURCE = {"ace_step": (ACE_REPO, ACE_REVISION), "yue2": (YUE_REPO, YUE_REVISION)}
LICENCE = {"ace_step": "MIT (commercial use allowed)",
           "yue2": "CC BY-NC 4.0: non-commercial use only; not for monetised or client work"}

# Built by .github/workflows/music-runtimes.yml and published to this release.
RUNTIME_RELEASE = "https://github.com/Zenvi-pro/zenvi-core/releases/download/music-runtimes-v1/%s"
RUNTIME = {             # (sys.platform, machine) -> (asset, sha256); "" until that build is published
    ("win32", "amd64"): ("zenvi-music-windows-x64-vulkan.zip", ""),
    ("linux", "x86_64"): ("zenvi-music-linux-x64-vulkan.zip", ""),
    ("darwin", "arm64"): ("zenvi-music-macos-arm64-metal.zip", ""),
}
_BINARIES = {"ace_step": ("ace-lm", "ace-synth"), "yue2": ("yue-synth",)}

ACE_VAE_CHUNK = "128"
# Q5_K_M at 16 steps: a 30 s bed in about 165 s at 2.9 GB on an Iris Xe (Q8 at 32 steps: 389 s, 4.7 GB).
YUE_STEPS, YUE_VAE_CORE = 16, "128"
YUE_TOKENS_PER_S = 40   # score + semantic tokens per second of audio, with headroom
RUN_TIMEOUT = 3600


class MusicError(hf.HyperframesError):
    """A music install or generation failed; the message is for the user."""


def home() -> str:
    return os.path.join(info.USER_PATH, "music")


def _exe(name: str) -> str:
    return os.path.join(home(), "bin", name + (".exe" if sys.platform == "win32" else ""))


def _model(name: str) -> str:
    return os.path.join(home(), "models", name)


def status(engine: str) -> dict:
    """{runtime, models}: whether this engine's binaries and pinned models are on disk."""
    return {"runtime": all(os.path.isfile(_exe(b)) for b in _BINARIES[engine]),
            "models": all(os.path.isfile(_model(m)) for m, _, _ in MODELS[engine])}


def install_plan(engine: str) -> List[dict]:
    st, steps = status(engine), []
    if not st["runtime"]:
        steps.append({"id": "runtime", "label": "Zenvi music runtime (about 40 MB)"})
    if not st["models"]:
        size = sum(b for _, _, b in MODELS[engine]) / 1e9
        name = "ACE-Step 1.5" if engine == "ace_step" else "YuE2-3B"
        steps.append({"id": "models", "label": "%s models (%.1f GB; licence %s)" % (name, size, LICENCE[engine])})
    return steps


def _download_pinned(folder: str, files: List[tuple], what: str) -> None:
    hf._download_pinned(folder, files, what)


def _unpack_runtime(engine: str) -> None:
    key = (sys.platform, _platform.machine().lower())
    asset, digest = RUNTIME.get(key, ("", ""))
    if not digest:
        raise MusicError("the Zenvi music runtime is not published for this computer (%s %s) yet" % key)
    downloads = os.path.join(home(), "downloads")
    _download_pinned(downloads, [(RUNTIME_RELEASE % asset, asset, digest)], "music runtime")
    with zipfile.ZipFile(os.path.join(downloads, asset)) as zf:
        zf.extractall(os.path.join(home(), "bin"))
    if sys.platform != "win32":
        for b in _BINARIES[engine]:
            os.chmod(_exe(b), 0o755)


def install(engine: str) -> dict:
    if engine not in ENGINES:
        raise MusicError("unknown music engine %r; use one of %s" % (engine, ", ".join(ENGINES)))
    st = status(engine)
    if not st["models"]:
        repo, rev = _MODEL_SOURCE[engine]
        files = [("https://huggingface.co/%s/resolve/%s/%s" % (repo, rev, name), name, digest)
                 for name, digest, _ in MODELS[engine]]
        _download_pinned(os.path.join(home(), "models"), files, "music model")
    if not st["runtime"]:
        _unpack_runtime(engine)
    return status(engine)


def _run(argv: List[str], cwd: str, timeout: float) -> Tuple[int, str]:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, stdin=subprocess.DEVNULL, creationflags=hf._NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise MusicError("%s did not finish within %d s" % (os.path.basename(argv[0]), timeout))
    except OSError as exc:
        raise MusicError("could not start %s: %s" % (argv[0], exc))
    return proc.returncode, hf._clean((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else ""))


def _check(argv: List[str], cwd: str) -> None:
    code, out = _run(argv, cwd, RUN_TIMEOUT)
    if code != 0:
        raise MusicError("%s failed (exit code %d): %s" % (os.path.basename(argv[0]), code, out[-1500:]))


def _write(path: str, data: dict) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


def _yue_max_seq(duration: float) -> int:
    """KV cache rows: enough for the song's tokens (a smaller cache is less RAM), in 1024 steps."""
    need = int(duration * YUE_TOKENS_PER_S) + 1024
    return max(4096, -(-need // 1024) * 1024)


def generate(engine: str, style: str, lyrics: str = "", duration: float = 30.0, seed: int = -1) -> str:
    """Generate music and return the MP3 path. Empty lyrics = an instrumental bed."""
    if engine not in ENGINES:
        raise MusicError("unknown music engine %r; use one of %s" % (engine, ", ".join(ENGINES)))
    style, lyrics = str(style or "").strip(), str(lyrics or "").strip()
    if not style:
        raise MusicError("describe the music: genre, mood, instruments")
    if not all(status(engine).values()):
        raise MusicError("%s is not installed on this computer yet" % engine)
    duration = max(5.0, min(float(duration or 30.0), 240.0))
    work = os.path.join(home(), "jobs", "%s-%d" % (engine, int(time.time() * 1000)))
    os.makedirs(work, exist_ok=True)
    out_dir = os.path.join(info.USER_PATH, "Downloads")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "zenvi_%s_%s.mp3" % (engine, os.path.basename(work).split("-")[-1]))
    models = os.path.join(home(), "models")

    if engine == "ace_step":
        req = {"caption": style, "lyrics": lyrics or "[Instrumental]", "duration": duration, "seed": seed,
               "inference_steps": 8, "synth_model": ACE_DIT}
        synth = [_exe("ace-synth"), "--models", models, "--vae-chunk", ACE_VAE_CHUNK, "--request"]
        if lyrics:
            req["lm_model"] = ACE_LM
            _check([_exe("ace-lm"), "--models", models, "--request", _write(os.path.join(work, "song.json"), req)],
                   work)
            _check(synth + [os.path.join(work, "song0.json")], work)
            rendered = os.path.join(work, "song00.mp3")
        else:
            _check(synth + [_write(os.path.join(work, "bed.json"), req)], work)
            rendered = os.path.join(work, "bed0.mp3")
        if not os.path.isfile(rendered):
            raise MusicError("ACE-Step finished but wrote no audio")
        os.replace(rendered, out)
    else:
        req = {"style": style, "lyrics": lyrics, "cot": "full" if lyrics else "off", "duration": duration,
               "lm_seed": seed, "seed": seed, "steps": YUE_STEPS, "output_format": "mp3"}
        _check([_exe("yue-synth"), "--model", _model(YUE_MODEL), "--vae", _model(YUE_VAE),
                "--request", _write(os.path.join(work, "song.json"), req), "--out", out,
                "--max-seq", str(_yue_max_seq(duration)), "--vae-core", YUE_VAE_CORE], work)
    if not os.path.isfile(out) or os.path.getsize(out) <= 0:
        raise MusicError("%s finished but the audio file is missing or empty" % engine)
    log.info("Generated %s music: %s", engine, out)
    return out


def generate_bed(query: str, duration: float = 30.0) -> str:
    """A stock-music stand-in: an instrumental ACE-Step bed for a search query."""
    return generate("ace_step", "%s, instrumental background music" % str(query or "").strip(), "", duration)
