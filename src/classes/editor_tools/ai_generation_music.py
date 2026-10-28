"""Music on this computer: an instrumental bed or a song with lyrics, generated here and imported.

Workstream: ai-generation. ``classes.music_local`` owns the runtimes and models
(ACE-Step 1.5 by default, YuE2 on request). No ComfyUI or server is needed;
create_media_with_comfyui_tool stays the ComfyUI path. The first call asks the
user before the one-time download.

Threading: install and generation run on the calling worker thread; only the
consent dialog and the import hop to the GUI thread.
"""

from __future__ import annotations

import re

from classes import music_local as ml
from classes.editor_tools import ai_generation_hyperframes as hfx
from classes.editor_tools._base import ToolError, enum, number, obj, ok, string
from classes.editor_tools._registry import editor_tool


def th():
    from classes import tool_handlers
    return tool_handlers


class Declined(ToolError):
    """The user said no to the one-time download."""


def ensure_installed(engine: str, progress=None) -> None:
    steps = ml.install_plan(engine)
    if not steps:
        return
    if not hfx._ask_install_consent(
            steps, title="Set up music generation",
            intro="Zenvi generates music on this computer; nothing is uploaded. This needs a one-time download:"):
        raise Declined("the user declined the music download; local music generation is unavailable until it "
                       "is installed")
    hfx._guard(ml.install, engine, progress=progress)


def import_track(path: str) -> str:
    """Import a generated track into Project Files; returns its file_id."""
    out = th().add_stock_media_to_project(local_path=path)
    match = re.search(r"file_id=([\w-]+)", out or "")
    if out.startswith("Error") or not match:
        raise ToolError("import failed: %s" % (out or "no project file was created"))
    return match.group(1)


@editor_tool(
    "generate_music_tool",
    label="Generate music",
    schema=obj({
        "prompt": string("Style tags: genre, mood, instruments, tempo, vocal type. e.g. 'chill lofi hip hop, "
                         "mellow piano, soft drums'."),
        "lyrics": string("Song lyrics with section tags ([Verse], [Chorus], ...). Empty = an instrumental bed.", ""),
        "duration_seconds": number("Length in seconds (5-240).", 30),
        "engine": enum(list(ml.ENGINES), "ace_step (default, MIT, fast) or yue2 (better songs with vocals, much "
                       "slower, non-commercial licence only).", "ace_step"),
    }, required=["prompt"]),
    background_safe=True,
    covers=("ai.music_local",),
)
def generate_music(prompt, lyrics="", duration_seconds=30, engine="ace_step"):
    """Generate a music bed or a song on this computer and import it into Project Files.

    Use for background music when stock search has nothing suitable, or when the
    user asks for an original track or a song with their lyrics. A 30 s bed takes
    about a minute on a laptop GPU; songs with lyrics take longer, and yue2 takes
    several minutes. Only pass engine=yue2 when the user asks for it, and tell them
    its licence is non-commercial. The result carries a file_id: place it with
    add_clip_to_timeline_tool. Nothing is uploaded and there is no undo step.
    """
    engine = engine or "ace_step"
    if engine not in ml.ENGINES:
        raise ToolError("engine must be one of %s" % ", ".join(ml.ENGINES))
    ensure_installed(engine)
    path = hfx._guard(ml.generate, engine, prompt, lyrics or "", float(duration_seconds or 30))
    file_id = import_track(path)
    kind = "song" if (lyrics or "").strip() else "instrumental bed"
    return ok("Generated a %s with %s and imported it into Project Files; place it with "
              "add_clip_to_timeline_tool." % (kind, engine), file_id=file_id, path=path, engine=engine,
              licence=ml.LICENCE[engine])
