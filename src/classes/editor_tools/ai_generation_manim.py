"""Manim on this computer: math and science animations, rendered here and imported.

Workstream: ai-generation. For what HTML compositions do poorly -- an equation
morphing into another, a function plotted as a parameter moves, geometry built
step by step, 3D surfaces. The scene is a Python file in a HyperFrames project
folder (written with hyperframes_write_file_tool), rendered with the local Manim
(``classes.manim_local`` owns the install) and imported like any motion graphic,
so place_motion_graphic_tool places it whole.

Threading: the install and the render run on the calling worker thread; only the
consent dialog and the import hop to the GUI thread.
"""

from __future__ import annotations

from classes import manim_local as ml
from classes.editor_tools import ai_generation_hyperframes as hfx
from classes.editor_tools._base import ToolError, boolean, enum, obj, ok, string
from classes.editor_tools._registry import editor_tool


def _status_line(st: dict) -> str:
    return "Manim " + ("ready" if st["manim"] else "MISSING") + "; LaTeX for equations " + (
        "ready" if st["latex"] else "not installed (Text() works, Tex()/MathTex() need it)")


@editor_tool(
    "manim_setup_tool",
    label="Set up math animations",
    schema=obj({
        "install": boolean("true installs whatever is missing (the user is asked to confirm first). "
                           "false only reports.", False),
        "latex": boolean("Also install LaTeX (TinyTeX, about 320 MB) so scenes can use Tex()/MathTex() "
                         "equations.", False),
    }),
    background_safe=True,
    covers=("ai.hyperframes",),
)
def manim_setup(install=False, latex=False):
    """Check, and with install=true install, Manim on this computer (its own Python; LaTeX on request).

    Call it first whenever render_manim_tool says Manim is not set up, or that a
    scene needs LaTeX. Nothing needs admin rights; the user sees a confirmation
    dialog before anything is downloaded -- if they decline, stop and tell them.
    """
    st = ml.status()
    steps = ml.install_plan(st, latex=bool(latex))
    if not steps:
        return ok("Manim is ready. " + _status_line(st), **st)
    if not install:
        return ok("Manim is NOT fully set up: " + _status_line(st) + ". Call manim_setup_tool with install=true "
                  "to install: " + "; ".join(s["label"] for s in steps) + ".", **st)
    if not hfx._ask_install_consent(
            steps, title="Set up math animations",
            intro="Zenvi renders math and science animations on this computer with Manim. The scenes the "
                  "assistant writes run as Python here. This needs a one-time install:"):
        raise ToolError("the user declined the Manim install; math animations are unavailable until it is installed")
    st = hfx._guard(ml.install, latex=bool(latex))
    left = ml.install_plan(st, latex=bool(latex))
    if left:
        raise ToolError("setup finished but is still missing: " + "; ".join(s["label"] for s in left))
    return ok("Manim installed and ready. " + _status_line(st), **st)


@editor_tool(
    "render_manim_tool",
    label="Render math animation",
    schema=obj({
        "project": hfx._PROJECT,
        "scene_file": string("The .py file in the project holding the scene (write it first with "
                             "hyperframes_write_file_tool), e.g. 'scene.py'."),
        "scene": string("The Scene class to render, e.g. 'PythagorasProof'."),
        "label": hfx._LABEL,
        "transparent": hfx._TRANSPARENT,
        "full_frame": boolean("true only when the user asked for a full-screen card with nothing behind it. "
                              "Otherwise the animation stays a transparent overlay, even if transparent is false.",
                              False),
        "quality": enum(["draft", "standard"], "draft = 480p preview, standard = 1080p.", "standard"),
    }, required=["project", "scene_file", "scene", "label"]),
    background_safe=True,
    covers=("ai.hyperframes",),
)
def render_manim(project, scene_file, scene, label, transparent=True, full_frame=False, quality="standard"):
    """Render a Manim scene on this computer and import it into Project Files.

    Use Manim for math and science: an equation morphing into another
    (TransformMatchingTex), plots on axes, geometry, 3D. Use HyperFrames for
    titles, lower thirds and designed graphics. One scene per graphic, a few
    seconds long. It stays a transparent overlay unless full_frame=true, which is
    only for a full-screen card with nothing behind it. Equations need
    LaTeX (manim_setup_tool latex=true); Text() does not. The result carries a
    file_id: place it next with place_motion_graphic_tool mode=overlay. Nothing is uploaded.
    """
    if not full_frame:
        transparent = True
    full = hfx._guard(ml.render, project, scene_file, scene, transparent=bool(transparent), quality=quality)
    return hfx._import(project, full, label, transparent)
