"""Lip sync and face swap on a timeline clip: its mouth or face replaced in place, as one undo step.

Workstream: ai-generation. Consent, the engine choice, the install prompt and the placement live here; the
work is classes.dubbing (Grok in chunks through the backend, or this computer through the downloaded worker).
The work runs on the calling worker thread; only the import and the placement hop to the GUI thread.
"""

from __future__ import annotations

import contextlib
import os

from classes import dubbing
from classes.editor_tools import ai_generation_hyperframes as hfx
from classes.editor_tools._base import (
    ToolError,
    boolean,
    ensure_unlocked,
    enum,
    get_app,
    obj,
    ok,
    on_main,
    project_fps,
    resolve_clip,
    string,
    th,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.ai_generation_tts import estimated_cues
from classes.handoff.jobs import track_job

LIP_CONSENT = ("Lip sync re-animates a real person's mouth to new words. Ask the user to confirm that everyone "
               "shown agrees to it, then call again with consent_confirmed=true.")
FACE_CONSENT = ("Face swap puts another person's face on a real clip. Ask the user to confirm that the person in "
                "the image and the people in the clip agree, then call again with consent_confirmed=true.")


def setting_for(engine: str, key: str) -> str:
    """An explicit cloud or local choice wins; anything else reads the setting."""
    if engine in ("cloud", "local"):
        return engine
    return str(get_app().get_settings().get(key) or "auto")


def file_path(file_id: str, what: str) -> str:
    from classes.query import File
    f = File.get(id=str(file_id)) if file_id else None
    path = f.absolute_path() if f else ""
    if not path:
        raise ToolError(f"the {what} is not in Project Files (file_id={file_id!r})")
    return path


def ask_person_consent(text: str) -> bool:
    """The person at the computer says yes or no; an agent's consent_confirmed alone never counts."""
    def _ask():
        from qt_api import QMessageBox

        box = QMessageBox(QMessageBox.Question, get_app()._tr("Confirm consent"), text + "\n\n"
                          + get_app()._tr("Does everyone involved agree?"), QMessageBox.Yes | QMessageBox.No,
                          get_app().window)
        box.setDefaultButton(QMessageBox.No)
        return box.exec_() == QMessageBox.Yes

    return bool(on_main(_ask, timeout=600))


def check_consent(confirmed: bool, refusal: str, question: str) -> None:
    if not confirmed:
        raise ToolError(refusal)
    if not ask_person_consent(question):
        raise ToolError("the person at the computer declined, so nothing was changed")


def _consent(steps) -> bool:
    return hfx._ask_install_consent(
        steps, title="Set up dubbing on this computer",
        intro="Zenvi dubs and swaps faces on this computer; nothing is uploaded. This needs a one-time download:")


def local_picks(features) -> dict:
    """This computer's runtime for these parts (installed only after the person agrees), and its backend picks."""
    try:
        status = dubbing.ensure_local(set(features), consent=_consent)
    except dubbing.DubbingError as exc:
        raise ToolError(str(exc)) from None
    picks = dubbing.picks_of(status)
    if not picks:
        raise ToolError("this computer has no working backend for dubbing yet; run the dubbing setup again")
    return picks


def _target(clip_id: str):
    clip = resolve_clip(timeline_clip_id=clip_id)
    ensure_unlocked(int(clip.data.get("layer") or 0))
    start = float(clip.data.get("start") or 0.0)
    end = float(clip.data.get("end") or 0.0)
    if end - start < 0.2:
        raise ToolError("the clip is too short to dub")
    if not end - start <= dubbing.MAX_JOB_S:           # also catches a NaN or infinite end
        raise ToolError(f"the clip is too long to dub: at most {dubbing.MAX_JOB_S // 60} minutes per call")
    source = th()._get_source_file_for_clip(clip)
    path = source.absolute_path() if source else ""
    if not path:
        raise ToolError("the clip's source file is missing from Project Files")
    size = (int(source.data.get("width") or 1280), int(source.data.get("height") or 720))
    return clip, source, path, start, end, size


def _place(clip, res: dict, start: float, end: float, what: str, source_label: str):
    """Import the result, stamp it and put it over the clip: one undo step with the tool call."""
    def unchanged():
        from classes.query import Clip
        now = Clip.get(id=clip.id)
        return bool(now) and (float(now.data.get("start") or 0.0), float(now.data.get("end") or 0.0),
                              now.data.get("file_id")) == (start, end, clip.data.get("file_id"))

    if not on_main(unchanged):
        raise ToolError("the clip changed while this ran (trimmed, moved to another file or deleted), so nothing "
                        "was replaced; run it again on the clip as it is now")
    f, err = th()._import_generated_video(res["out"])
    if f and os.path.abspath(f.absolute_path() or "") != os.path.abspath(res["out"]):
        with contextlib.suppress(OSError):
            os.remove(res["out"])                     # the import made its own copy in the media folder
    if not f:
        raise ToolError("the result could not be imported (%s)" % (err or "unknown error"))
    th()._stamp_generated_video_metadata(f, what, source=source_label)
    try:
        placed = on_main(lambda: th()._install_baked_head(clip.id, f.id, end - start, res["seconds"]))
    except Exception as exc:
        raise ToolError(f"the result was imported (file_id={f.id}) but could not replace the clip: {exc}") from None
    return placed, f


@editor_tool(
    "lip_sync_clip_tool",
    label="Lip sync clip",
    schema=obj({
        "clip_id": string("Timeline clip id (get_timeline_state_tool lists them). The clip's picture is replaced in place."),
        "text": string("What the person should say, as the script. Tags such as [chuckle] work on this computer only.", ""),
        "audio_file_id": string("Project Files id of a recording to lip sync to; it becomes the clip's sound and runs on "
                                "this computer. Give this or text, not both.", ""),
        "voice": string("Grok voice preset for the cloud, e.g. leo. Empty = leo.", ""),
        "engine": enum(["auto", "cloud", "local"], "auto follows the Lip Sync With setting (Grok, unless the sound is "
                       "yours or credits run out); cloud = Grok (credits); local = this computer.", "auto"),
        "consent_confirmed": boolean("true only after the user confirms that everyone shown agrees to their mouth "
                                     "being re-animated for these words.", False),
    }, required=["clip_id"]),
    background_safe=True,
    covers=("ai.lip_sync",),
)
def lip_sync_clip(clip_id, text="", audio_file_id="", voice="", engine="auto", consent_confirmed=False):
    """Make a timeline clip's mouth say new words, replacing the clip in place (one undo step).

    Use for "have her say this", "dub the clip". Give the words (text) or a recording (audio_file_id),
    not both. Grok speaks the script with a preset voice in chunks (credits). A recording, engine local,
    or a credits shortfall runs on this computer (Ming speech and edtalk lip sync; a one-time download).
    Needs consent_confirmed=true, which you ask the user first. Returns the new timeline_clip_id and file_id.
    """
    check_consent(consent_confirmed, LIP_CONSENT,
                  "Zenvi is about to make a real person's mouth say new words in this clip.")
    text = str(text or "").strip()
    if len(text) > dubbing.MAX_SCRIPT_CHARS:
        raise ToolError(f"the words are too long: at most {dubbing.MAX_SCRIPT_CHARS} characters per call")
    audio_file_id = str(audio_file_id or "").strip()
    if bool(text) == bool(audio_file_id):
        raise ToolError("give the words to say (text) or a recording (audio_file_id): exactly one of them")
    clip, source, path, start, end, size = _target(clip_id)
    audio = file_path(audio_file_id, "recording") if audio_file_id else ""
    setting = setting_for(engine, "lip-sync-engine")
    chosen = dubbing.engine_for("lip_sync", setting, keep_own_audio=bool(audio))
    if chosen == "cloud" and audio:
        raise ToolError("Grok cannot use your recording; give the words in text, or set engine to local")
    fallback = setting != "cloud"
    features = {"face"} | ({"ming"} if text else set())
    picks = local_picks(features) if chosen == "local" else None
    resolve = (lambda: local_picks(features)) if chosen == "cloud" and fallback else None
    out = th()._output_path_for_generated_video(ext=".mp4")
    try:
        with track_job("Lip sync", kind="dubbing") as job:
            res = dubbing.lip_sync(
                source=path, start=start, end=end, size=size, fps=project_fps(), out=out, text=text, audio=audio,
                cues=estimated_cues(text, end - start) if text else None, voice=str(voice or "").strip(),
                engine=chosen, allow_local_fallback=fallback, picks=picks, resolve_picks=resolve, job=job)
    except dubbing.DubbingError as exc:
        raise ToolError(str(exc)) from None
    placed, f = _place(clip, res, start, end, text or "Lip sync from a recording", "lip_sync")
    return ok(f"Lip-synced clip {clip.id} ({res['cloud_chunks']} of {res['chunks']} chunks on the cloud, the rest "
              f"on this computer) and placed it at {placed['position']:.2f} s. One undo restores the original.",
              timeline_clip_id=placed["timeline_clip_id"], file_id=f.id, engine=chosen, chunks=res["chunks"],
              cloud_chunks=res["cloud_chunks"], local_chunks=res["local_chunks"])


@editor_tool(
    "face_swap_clip_tool",
    label="Swap face in clip",
    schema=obj({
        "clip_id": string("Timeline clip id. The clip's face is swapped in place."),
        "face_image_file_id": string("Project Files id of an image of the face to put on the clip."),
        "engine": enum(["auto", "cloud", "local"], "auto follows the Face Swap With setting: this computer for clips with "
                       "speech (lips stay in sync), Grok for silent clips; cloud = Grok (credits); local = this computer.",
                       "auto"),
        "consent_confirmed": boolean("true only after the user confirms that the person in the image, and the people "
                                     "in the clip, agree.", False),
    }, required=["clip_id", "face_image_file_id"]),
    background_safe=True,
    covers=("ai.face_swap",),
)
def face_swap_clip(clip_id, face_image_file_id, engine="auto", consent_confirmed=False):
    """Put another person's face on a timeline clip, replacing the clip in place (one undo step).

    Use for "swap the face in this clip" with a face image in Project Files. Clips with speech run on
    this computer, so the lips stay in sync; silent clips can use Grok (credits). Needs
    consent_confirmed=true, which you ask the user first. Returns the new timeline_clip_id and file_id.
    """
    check_consent(consent_confirmed, FACE_CONSENT, "Zenvi is about to put another person's face on this clip.")
    clip, source, path, start, end, size = _target(clip_id)
    face = file_path(face_image_file_id, "face image")
    # Not yet checked for speech counts as speech: the cloud would regenerate the lips without hearing them.
    speech = (source.data.get("ai_metadata") or {}).get("has_speech") is not False
    setting = setting_for(engine, "face-swap-engine")
    chosen = dubbing.engine_for("face_swap", setting, has_speech=speech)
    picks = local_picks({"face"})
    out = th()._output_path_for_generated_video(ext=".mp4")
    try:
        with track_job("Face swap", kind="dubbing") as job:
            res = dubbing.face_swap(
                source=path, start=start, end=end, size=size, fps=project_fps(), out=out, face_image=face,
                engine=chosen, allow_local_fallback=setting != "cloud", picks=picks, job=job)
    except dubbing.DubbingError as exc:
        raise ToolError(str(exc)) from None
    placed, f = _place(clip, res, start, end, "Face swap", "face_swap")
    return ok(f"Swapped the face in clip {clip.id} ({res['cloud_chunks']} of {res['chunks']} chunks on the cloud) and "
              f"placed it at {placed['position']:.2f} s. One undo restores the original.",
              timeline_clip_id=placed["timeline_clip_id"], file_id=f.id, engine=chosen, chunks=res["chunks"],
              cloud_chunks=res["cloud_chunks"], local_chunks=res["local_chunks"])
