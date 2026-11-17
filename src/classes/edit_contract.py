"""The edit contract: how a *finished piece* (a vlog, a reel, a film) is kept from skipping survey and brief.

Pure Python on purpose: no Qt, no project, no disk. The executor (``agent_tools/execute.py``) and the editor tools
(``editor_tools/media_index_tools_edit.py``) feed it plain dicts and it answers with plain dicts or a ``Verdict``, so every
rule here is unit-testable without the app.

What it does
- ``classify_intent``: is this ask a whole piece, a recreation, or a narrow tweak? Ambiguous means ``free``.
- ``readiness``: is the footage indexed enough to plan from (minimum viable, never "fully indexed").
- ``default_brief``: taste defaults (form, length, aspect, music, opening, beat map) from the survey, so a vague prompt
  starts from the decisions a hand-written prompt would have made.
- ``check_policy``: the one hard rule of v1: in a ``finished_piece``, placing clips waits for a brief.
- ``advance`` / ``apply_observation``: the small state machine (phase, audit rounds) kept in the project next to the brief.

The playbook (``edit_playbook.py``) stays the soft doctrine; this module is the part that can say no.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

CONTRACT_KEY = "edit_contract"
CONTRACT_VERSION = 1

MODE_FREE = "free"
MODE_FINISHED = "finished_piece"
MODE_RECREATE = "recreate"
MODES = (MODE_FREE, MODE_FINISHED, MODE_RECREATE)

PHASES = ("idle", "surveying", "briefed", "assembling", "finishing", "audited", "done")
PRE_BRIEF_PHASES = ("idle", "surveying")

MAX_AUDIT_ROUNDS = 3
CONTRACT_MAX_BYTES = 8 * 1024

# v1 gates only the tools that place footage on the timeline. Everything else stays open.
PLACEMENT_TOOLS = frozenset({"add_clips_to_timeline_tool", "add_clip_to_timeline_tool"})

# Tools whose success moves a finished piece along. Everything else is never looked at (no project read on the hot path).
FINISH_TOOLS = frozenset({"color_grade_clip_tool", "add_captions_tool", "balance_mix_tool", "harmonize_look_tool", "duck_under_speech_tool"})
OBSERVED_TOOLS = PLACEMENT_TOOLS | FINISH_TOOLS | frozenset({"review_edit_tool", "export_video_tool"})

ROLLOUT_MODES = ("off", "warn", "enforce")
DEFAULT_ROLLOUT = "warn"


def rollout_setting(env: Optional[Dict[str, str]] = None) -> str:
    """``off`` | ``warn`` | ``enforce``, from ``ZENVI_EDIT_CONTRACT`` or the ``edit-contract`` desktop setting; ``warn`` when unset.

    Reads no files, so it is safe on the GUI thread. Anything unrecognised means the default: a typo must not switch the gate.
    """
    import os
    value = str((env if env is not None else os.environ).get("ZENVI_EDIT_CONTRACT", "")).strip().lower()
    if not value and env is None:
        try:
            from classes.app import get_app
            stored = get_app().get_settings().get("edit-contract")
            value = stored.strip().lower() if isinstance(stored, str) else ""
        except Exception:
            value = ""
    return value if value in ROLLOUT_MODES else DEFAULT_ROLLOUT

# Readiness: minimum viable, so a slow indexer never deadlocks the agent.
READY_FRACTION = 0.70
WAIT_ATTEMPTS_BEFORE_PARTIAL_OK = 3

DIGEST_CAP_CHARS = 3500
DIGEST_FULL_CAP_CHARS = 8000

# Fallback when media_index.review cannot be imported (it normally can); a test pins this to review.FORMS.
_FALLBACK_FORMS = ("vlog", "tiktok", "reel", "short", "montage", "trailer", "recap", "highlight", "music video", "film",
                   "movie", "scene", "ad", "promo", "commercial", "travel",
                   "podcast", "interview", "tutorial", "lecture", "talk", "webinar", "news", "broadcast")
_FALLBACK_MAX_SECONDS = {"tiktok": 180.0, "reel": 90.0, "short": 60.0}
_MUSIC_FORMS = ("vlog", "tiktok", "reel", "short", "montage", "trailer", "recap", "highlight", "music video", "film",
                "movie", "scene", "ad", "promo", "commercial", "travel")
_SHORT_FORMS = ("tiktok", "reel", "short")


def known_forms() -> Tuple[str, ...]:
    try:
        from classes.media_index import review
        return tuple(review.FORMS)
    except Exception:
        return _FALLBACK_FORMS


def form_max_seconds() -> Dict[str, float]:
    try:
        from classes.media_index import review
        return dict(review.FORM_MAX_SECONDS)
    except Exception:
        return dict(_FALLBACK_MAX_SECONDS)


# ============================ intent ============================
_WORD = r"(?<![a-z0-9])%s(?![a-z0-9])"


def _rx(*phrases: str) -> "re.Pattern[str]":
    return re.compile("|".join(_WORD % p for p in phrases), re.I)


_RECREATE_RX = _rx(r"recreate", r"re-create", r"remake", r"like this video", r"same as this video", r"copy this video")
_FORM_RX = _rx(r"vlog", r"reels?", r"tik ?tok", r"trailer", r"montage", r"film", r"movie", r"music video", r"promo",
               r"commercial", r"recap", r"highlight reel", r"short film", r"youtube shorts?", r"yt shorts?", r"documentary",
               r"travel video", r"trip video", r"ad")
_PIECE_RX = re.compile(
    r"(?<![a-z0-9])(make|edit|cut|build|create|put together|turn)\b.{0,60}?"
    r"(?<![a-z0-9])(vlog|reel|trailer|montage|film|movie|video|short|ad|recap|promo|something|story)(?![a-z0-9])", re.I)
_VAGUE_RX = re.compile(
    r"(make|do|create)\s+(me\s+)?something\s+(cool|nice|good|great|awesome|fun|epic|cinematic)|surprise me|"
    r"edit (all of )?(my|the|these) (trip|footage|clips|videos|vacation|holiday|day)", re.I)
_NARROW_RX = _rx(r"warmer", r"cooler", r"brighter", r"darker", r"cut this", r"trim", r"captions?", r"snappy", r"zoom",
                 r"fade", r"volume", r"louder", r"quieter", r"speed", r"slow(er)?", r"remove", r"delete", r"undo", r"export",
                 r"crop", r"rotate", r"mute", r"title", r"this clip", r"that clip", r"split", r"color", r"colour")
_RESTART_RX = _rx(r"from scratch", r"start over", r"start again", r"new video", r"new edit", r"redo it all")


def classify_intent(text: str, *, timeline_empty: bool, media_minutes: float = 0.0) -> str:
    """``free`` | ``finished_piece`` | ``recreate`` for a user ask. Ambiguous asks are ``free``: never trap a user."""
    t = (text or "").strip()
    if not t:
        return MODE_FREE
    if _RECREATE_RX.search(t):
        return MODE_RECREATE
    restart = bool(_RESTART_RX.search(t))
    whole = bool(_FORM_RX.search(t) or _PIECE_RX.search(t) or _VAGUE_RX.search(t))
    if not whole:
        return MODE_FREE
    if media_minutes <= 0 and timeline_empty:
        return MODE_FREE                       # nothing to survey yet: begin_edit_tool will say "import first"
    if not timeline_empty and not restart:
        return MODE_FREE                       # an edit is under way: whole-piece wording is a tweak until the user restarts
    if _NARROW_RX.search(t) and not (_FORM_RX.search(t) or _VAGUE_RX.search(t)):
        return MODE_FREE                       # "make it snappy": narrow words without a whole-piece form
    return MODE_FINISHED


# ============================ readiness ============================
def readiness(total: int, structured: int, *, searchable: int = 0, rich: int = 0,
              structured_minutes: Optional[float] = None, needed_minutes: Optional[float] = None) -> str:
    """``none`` | ``partial`` | ``structure`` | ``searchable`` | ``rich``: how much of the footage can be planned from.

    ``structure`` (cuts and quality known) is enough to plan; ``searchable`` adds vectors, ``rich`` adds speech/look/music.
    ``partial`` means some, but under the threshold: wait a little, then proceed on the structured subset.
    """
    if total <= 0 or structured <= 0:
        return "none"
    frac = structured / float(total)
    enough_minutes = (structured_minutes is not None and needed_minutes is not None and structured_minutes >= needed_minutes)
    if frac < READY_FRACTION and not enough_minutes:
        return "partial"
    if rich / float(total) >= READY_FRACTION:
        return "rich"
    if searchable / float(total) >= READY_FRACTION:
        return "searchable"
    return "structure"


def can_start(tier: str, attempts: int = 0) -> bool:
    """May a brief be committed and assembly unlocked? *attempts* is how many times the agent was already told to wait.

    ``partial`` (some footage structured, under the threshold) is allowed on the WAIT_ATTEMPTS_BEFORE_PARTIAL_OK-th call,
    so a slow indexer never deadlocks the agent. ``none`` (nothing structured) never starts: there is nothing to plan from.
    """
    if tier in ("structure", "searchable", "rich"):
        return True
    return tier == "partial" and attempts + 1 >= WAIT_ATTEMPTS_BEFORE_PARTIAL_OK


# ============================ forms and brief defaults ============================
# Whole words only: "make it shorter" must not become the 'short' form. First match wins, so specific phrases come first.
_FORM_ALIASES = tuple((re.compile(_WORD % pattern, re.I), form) for pattern, form in (
    (r"music videos?", "music video"), (r"short films?", "film"), (r"documentar(y|ies)", "film"), (r"tik ?tok", "tiktok"),
    (r"(youtube|yt) shorts?", "short"), (r"(instagram )?reels?", "reel"), (r"shorts", "short"), (r"vlogs?", "vlog"),
    (r"trailers?", "trailer"), (r"montages?", "montage"), (r"recaps?", "recap"), (r"highlights?", "highlight"),
    (r"promos?", "promo"), (r"commercials?", "commercial"), (r"movies?", "movie"), (r"films?", "film"),
    (r"podcasts?", "podcast"), (r"interviews?", "interview"), (r"tutorials?", "tutorial"), (r"lectures?", "lecture"),
    (r"webinars?", "webinar"), (r"travel( video)?", "travel"), (r"trip( video)?", "vlog"), (r"ads?", "ad"),
))


def normalize_form(text: str) -> str:
    """A member of ``review.FORMS`` for free text ('YouTube vlog' -> 'vlog'), or ''. Free text sets no form rules."""
    t = (text or "").lower().strip()
    forms = known_forms()
    if t in forms:
        return t
    for rx, form in _FORM_ALIASES:
        if form in forms and rx.search(t):
            return form
    return ""


def _majority(orientation: Dict[str, int]) -> Tuple[str, float]:
    total = sum(int(v) for v in (orientation or {}).values())
    if total <= 0:
        return "", 0.0
    key = max(orientation, key=lambda k: orientation[k])
    return key, orientation[key] / float(total)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


_LENGTH_RX = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m)(?![a-z0-9])", re.I)


def parse_length_seconds(text: str) -> Optional[float]:
    """A length the user said ('a 2 minute vlog', '45s', '90 sec'), in seconds; None when none is said or it is absurd."""
    m = _LENGTH_RX.search(text or "")
    if not m:
        return None
    n = float(m.group(1))
    seconds = n * (60.0 if m.group(2).lower().startswith("m") else 1.0)
    return seconds if 5.0 <= seconds <= 4 * 3600.0 else None


def default_brief(survey: Dict[str, Any], *, user_ask: str = "", form: str = "", vibe: str = "",
                  target_seconds: Optional[float] = None, aspect: str = "",
                  bans: Optional[List[str]] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    """``(brief, questions, inferred)``: the taste defaults a hand-written prompt would have set, from the survey.

    ``inferred`` names every key the contract chose itself, so the agent (and the user) can see what was assumed.
    ``questions`` is non-empty only when a missing choice changes the film (length or aspect), as the playbook says.
    """
    survey = survey or {}
    totals = survey.get("totals") or {}
    usable = float(totals.get("usable_minutes") or totals.get("video_minutes") or 0.0)
    trip = survey.get("trip") or {}
    days = list(trip.get("days") or [])
    orientation = survey.get("orientation") or {}
    moments = list(survey.get("top_moments") or [])
    text = f"{user_ask or ''} {vibe or ''}"
    inferred: List[str] = []
    brief: Dict[str, Any] = {}

    chosen = normalize_form(form) or normalize_form(text)
    if not chosen:
        major, frac = _majority(orientation)
        chosen = "reel" if (major == "portrait" and frac >= 0.8) else ("vlog" if days else "montage")
        inferred.append("form")
    brief["form"] = chosen

    maxes = form_max_seconds()
    said = parse_length_seconds(text)
    if target_seconds and float(target_seconds) > 0:
        brief["target_seconds"] = float(target_seconds)
    elif said:
        brief["target_seconds"] = round(said)              # the user's own number is not an assumption
    else:
        base = usable * 60.0 * 0.125                      # keep ~10-15% of the usable raw (the playbook's rule)
        if chosen in maxes:
            seconds = _clamp(base, 20.0, maxes[chosen])
        elif re.search(r"cinematic|connected|behind the scenes|bts|warm", text, re.I):
            seconds = _clamp(base, 180.0, 240.0)           # the playbook's "~3-4 min" intimate creator-doc
        else:
            seconds = _clamp(base, 45.0, 300.0)
        brief["target_seconds"] = round(seconds)
        inferred.append("target_seconds")

    major, frac = _majority(orientation)
    if aspect:
        brief["aspect"] = aspect
    else:
        brief["aspect"] = "9:16" if (chosen in _SHORT_FORMS or (major == "portrait" and frac >= 0.6)) else (
            "1:1" if major == "square" and frac >= 0.6 else "16:9")
        inferred.append("aspect")

    if vibe:
        brief["vibe"] = vibe.strip()[:200]
        brief["wants_grade"] = True
    brief["wants_music"] = chosen in _MUSIC_FORMS
    brief["wants_captions"] = chosen in _SHORT_FORMS
    inferred += ["wants_music", "wants_captions"]
    brief["bans"] = [str(b) for b in (bans or [])][:10]

    if moments:
        m = moments[0]
        brief["opening"] = {k: m.get(k) for k in ("file_id", "name", "start", "end", "why") if m.get(k) is not None}
        inferred.append("opening")
    beat_map = _beat_map(days, moments)
    if beat_map:
        brief["beat_map"] = beat_map
        inferred.append("beat_map")

    questions: List[Dict[str, Any]] = []
    has_length = bool(target_seconds) or bool(said)
    if not has_length and chosen not in maxes and usable >= 90.0:
        questions.append({"id": "length", "ask": "How long should it be?", "options": ["about 45 s", "about 3 min", "about 8 min"],
                          "default": f"{int(brief['target_seconds'])} s"})
    mixed = sum(1 for k in ("landscape", "portrait") if (orientation.get(k) or 0) > 0) == 2
    if not aspect and chosen not in _SHORT_FORMS and mixed and frac < 0.7:
        questions.append({"id": "aspect", "ask": "Vertical (9:16) or widescreen (16:9)?", "options": ["9:16", "16:9"], "default": brief["aspect"]})
    return brief, questions[:2], inferred


def _beat_map(days: List[Dict[str, Any]], moments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """3-5 act labels: trip days (grouped when there are many), else highlight order. A stub the agent refines."""
    acts: List[Dict[str, Any]] = []
    if days:
        n = min(5, max(3, len(days))) if len(days) >= 3 else len(days)
        per = max(1, -(-len(days) // n))
        for i in range(0, len(days), per):
            chunk = days[i:i + per]
            places = [str(p) for d in chunk for p in (d.get("places") or [])][:2]
            label = f"Day {chunk[0].get('day')}" + (f"-{chunk[-1].get('day')}" if len(chunk) > 1 else "")
            acts.append({"label": label + (f" · {', '.join(places)}" if places else ""),
                         "notes": f"{sum(int(d.get('clips') or 0) for d in chunk)} clips, {round(sum(float(d.get('minutes') or 0) for d in chunk), 1)} min"})
        return acts[:5]
    if moments:
        names = ("Opening", "Build", "Peak", "Close")
        third = max(1, len(moments) // 3)
        for i, name in enumerate(names[:3]):
            sl = moments[i * third:(i + 1) * third]
            if sl:
                acts.append({"label": name, "notes": "; ".join(str(m.get("name") or m.get("file_id")) for m in sl[:2])})
    return acts


def brief_ready(brief: Optional[Dict[str, Any]]) -> bool:
    """The minimum that unlocks assembly: a form and a target length."""
    b = brief or {}
    return bool(str(b.get("form") or "").strip()) and bool(b.get("target_seconds") or b.get("target_length"))


# ============================ digest (what begin_edit_tool hands the model) ============================
def build_digest(survey: Dict[str, Any], tier_info: Dict[str, Any], *, cap: int = DIGEST_CAP_CHARS) -> Dict[str, Any]:
    """A compact survey the model can plan from without calling the overview again. Trimmed to *cap* characters."""
    s = survey or {}
    totals = s.get("totals") or {}
    trip = s.get("trip") or {}

    def make(moments: int, music: int, days: int, ni: int, why: int) -> Dict[str, Any]:
        return {
            "totals": {k: totals.get(k) for k in ("files", "videos", "audio", "images", "video_minutes", "usable_minutes", "not_indexed")},
            "readiness": tier_info,
            "top_moments": [{"file_id": m.get("file_id"), "name": m.get("name"), "start": m.get("start"), "end": m.get("end"),
                             "highlight": m.get("highlight"), "why": str(m.get("why") or "")[:why]}
                            for m in (s.get("top_moments") or [])[:moments]],
            "days": [{"day": d.get("day"), "date": d.get("date"), "minutes": d.get("minutes"), "clips": d.get("clips"),
                      "places": d.get("places")} for d in (trip.get("days") or [])[:days]],
            "music": [{"file_id": m.get("file_id"), "name": m.get("name"), "bpm": m.get("bpm")} for m in (s.get("music") or [])[:music]],
            "orientation": s.get("orientation") or {},
            "not_indexed": (s.get("not_indexed") or [])[:ni],
        }

    ladder = ((15, 8, 14, 8, 160), (8, 5, 10, 5, 100)) if cap > DIGEST_CAP_CHARS else ((8, 5, 10, 5, 100),)
    for args in ladder + ((6, 3, 8, 3, 60), (4, 2, 5, 2, 40), (3, 0, 3, 0, 20), (2, 0, 0, 0, 0)):
        digest = make(*args)
        if len(json.dumps(digest, ensure_ascii=False)) <= cap:
            return digest
    return digest


# ============================ the contract and its state machine ============================
def new_contract(mode: str = MODE_FREE, *, source: str = "classifier") -> Dict[str, Any]:
    return {"version": CONTRACT_VERSION, "mode": mode if mode in MODES else MODE_FREE, "phase": "idle", "source": source,
            "readiness": {}, "attempts": 0, "audit": {"needs": [], "unknown": [], "rounds": 0, "fix_with": [], "open_needs": []},
            "overrides": {}}


def normalize(contract: Any) -> Dict[str, Any]:
    """A usable contract from whatever was stored. Missing, malformed or newer-than-known means ``free``: never block on junk."""
    if not isinstance(contract, dict) or contract.get("version") != CONTRACT_VERSION:
        return new_contract(MODE_FREE, source="none")
    out = new_contract(str(contract.get("mode") or MODE_FREE), source=str(contract.get("source") or "stored"))
    if contract.get("phase") in PHASES:
        out["phase"] = contract["phase"]
    for key in ("readiness", "overrides"):
        if isinstance(contract.get(key), dict):
            out[key] = dict(contract[key])
    try:
        out["attempts"] = max(0, int(contract.get("attempts") or 0))
    except (TypeError, ValueError):
        pass
    if isinstance(contract.get("audit"), dict):
        a = contract["audit"]
        out["audit"] = {"needs": list(a.get("needs") or [])[:20], "unknown": list(a.get("unknown") or [])[:20],
                        "rounds": int(a.get("rounds") or 0), "fix_with": list(a.get("fix_with") or [])[:20],
                        "open_needs": list(a.get("open_needs") or [])[:20]}
    return out


def to_json(contract: Dict[str, Any]) -> Dict[str, Any]:
    """The contract as plain JSON for the project, or ValueError over the size cap."""
    c = normalize(contract)
    if len(json.dumps(c, ensure_ascii=False).encode("utf-8")) > CONTRACT_MAX_BYTES:
        raise ValueError(f"the edit contract is over {CONTRACT_MAX_BYTES // 1024} KB")
    return c


def effective_phase(contract: Dict[str, Any], brief: Optional[Dict[str, Any]]) -> str:
    """The phase, lifted to ``briefed`` when the brief already has a form and length (set_edit_brief_tool counts too)."""
    c = normalize(contract)
    phase = c["phase"]
    if phase in PRE_BRIEF_PHASES and brief_ready(brief):
        return "briefed"
    return phase


def advance(contract: Dict[str, Any], event: str, **kw: Any) -> Dict[str, Any]:
    """A new contract after *event*; the input is never changed. Unknown events change nothing."""
    c = normalize(contract)
    phase = c["phase"]
    if event == "begin_ok":
        c["mode"] = kw.get("mode") or c["mode"]
        c["readiness"] = dict(kw.get("readiness") or c["readiness"])
        c["source"] = "begin_edit"
        if phase in PRE_BRIEF_PHASES:                       # a call in the middle of the job never moves the phase backwards
            c["phase"] = "briefed" if kw.get("ready", True) else "surveying"
        c["attempts"] = 0 if kw.get("ready", True) else c["attempts"]
    elif event == "begin_wait":
        c["mode"] = kw.get("mode") or c["mode"]
        c["readiness"] = dict(kw.get("readiness") or c["readiness"])
        c["phase"] = "surveying"
        c["attempts"] += 1
    elif event == "brief_saved":
        if phase in PRE_BRIEF_PHASES and brief_ready(kw.get("brief")):
            c["phase"] = "briefed"
    elif event == "placed":
        if phase in ("briefed", "audited", "done"):
            c["phase"] = "assembling"
    elif event == "finish_layer":
        if phase in ("assembling", "audited", "done"):
            c["phase"] = "finishing"
    elif event == "review_done":
        needs = [str(x) for x in (kw.get("needs") or [])][:20]
        rounds = int(c["audit"]["rounds"]) + 1
        c["audit"] = {"needs": needs, "unknown": [str(x) for x in (kw.get("unknown") or [])][:20], "rounds": rounds,
                      "fix_with": [str(x) for x in (kw.get("fix_with") or [])][:20], "open_needs": []}
        if not needs:
            c["phase"] = "done"
        elif rounds >= MAX_AUDIT_ROUNDS:
            c["phase"] = "done"
            c["audit"]["open_needs"] = needs          # report these; do not loop forever
        else:
            c["phase"] = "audited"
    elif event == "user_free":
        c["mode"], c["phase"] = MODE_FREE, "idle"
    elif event == "new_piece":
        c["mode"], c["phase"] = MODE_FINISHED, "idle"
        c["audit"] = new_contract()["audit"]
        c["attempts"] = 0
    return c


def apply_observation(contract: Dict[str, Any], tool_name: str, status: str, *, needs: Optional[List[str]] = None,
                      unknown: Optional[List[str]] = None, fix_with: Optional[List[str]] = None) -> Dict[str, Any]:
    """What a finished tool call means for the contract. Only successful calls count."""
    if status not in ("applied", "unchanged"):
        return normalize(contract)
    if tool_name in PLACEMENT_TOOLS and status == "applied":
        return advance(contract, "placed")
    if tool_name in FINISH_TOOLS and status == "applied":
        return advance(contract, "finish_layer")
    if tool_name == "review_edit_tool":
        return advance(contract, "review_done", needs=needs or [], unknown=unknown or [], fix_with=fix_with or [])
    return normalize(contract)


# ============================ policy ============================
@dataclass(frozen=True)
class Verdict:
    action: str            # allow | warn | refuse
    message: str = ""


ALLOW = Verdict("allow")


def _refusal_text(c: Dict[str, Any]) -> str:
    r = c.get("readiness") or {}
    tier, total, structured = r.get("tier"), r.get("total"), r.get("structured")
    wait = ""
    if tier in ("none", "partial") and total:
        wait = f" {structured or 0}/{total} videos have a structure index yet: call begin_edit_tool again shortly. Do not sample frames."
    return ("edit contract: placing clips waits for a brief. Call begin_edit_tool (or set_edit_brief_tool with form + target_seconds) first, "
            f"then place. phase={c.get('phase')}.{wait}")


def check_policy(contract: Any, tool_name: str, *, brief: Optional[Dict[str, Any]] = None, rollout: str = DEFAULT_ROLLOUT) -> Verdict:
    """May *tool_name* run now? v1: in a ``finished_piece``, placement waits for a brief; everything else is allowed."""
    if rollout == "off" or tool_name not in PLACEMENT_TOOLS:
        return ALLOW
    c = normalize(contract)
    if c["mode"] != MODE_FINISHED or c["overrides"].get("allow_assemble_without_brief"):
        return ALLOW
    if effective_phase(c, brief) not in PRE_BRIEF_PHASES:
        return ALLOW
    return Verdict("refuse" if rollout == "enforce" else "warn", _refusal_text(c))


def export_warning(contract: Any) -> Optional[str]:
    """A note for an export receipt (never a refusal): the finished piece was not audited, or still has open needs."""
    c = normalize(contract)
    if c["mode"] != MODE_FINISHED:
        return None
    a = c["audit"]
    if c["phase"] not in ("audited", "done"):
        return "edit contract: this finished piece was not audited; run review_edit_tool and fix what it names, or say you skipped it."
    open_needs = a.get("open_needs") or a.get("needs") or []
    if open_needs:
        return "edit contract: review_edit_tool still names open needs: " + "; ".join(str(x) for x in open_needs[:3]) + ". Report them."
    return None


def edit_state_line(contract: Any, brief: Optional[Dict[str, Any]], index_line: str = "") -> str:
    """One line for the editor snapshot: the contract, the brief's headline and the audit state."""
    c = normalize(contract)
    parts = [f"contract={c['mode']}/{effective_phase(c, brief)}"]
    b = brief or {}
    if b.get("form"):
        bits = [f"form={b.get('form')}"]
        if b.get("target_seconds"):
            bits.append(f"target={int(float(b['target_seconds']))}s")
        if b.get("vibe"):
            bits.append(f"vibe={str(b['vibe'])[:40]!r}")
        parts.append("brief: " + " ".join(bits))
    if index_line:
        parts.append(index_line)
    a = c["audit"]
    if a["rounds"]:
        parts.append(f"audit: needs={len(a['needs'])} rounds={a['rounds']}")
    return " · ".join(parts)[:600]
