"""The edit contract engine: pure rules, no Qt, no project. Every policy cell and every classifier guard is pinned here."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from classes import edit_contract as EC  # noqa: E402

SURVEY = {
    "totals": {"files": 12, "videos": 10, "audio": 2, "images": 0, "video_minutes": 48.0, "usable_minutes": 40.0, "not_indexed": 2},
    "trip": {"days": [{"day": 1, "date": "2026-07-01", "minutes": 20.0, "clips": 5, "places": ["Lisbon"]},
                      {"day": 2, "date": "2026-07-02", "minutes": 28.0, "clips": 5, "places": ["Sintra"]}], "places": []},
    "top_moments": [{"file_id": "f1", "name": "sunset.mp4", "start": 3.0, "end": 9.0, "highlight": 0.93, "why": "golden hour over the river"},
                    {"file_id": "f2", "name": "tram.mp4", "start": 1.0, "end": 5.0, "highlight": 0.8, "why": "tram"}],
    "music": [{"file_id": "m1", "name": "beat.mp3", "bpm": 96}],
    "orientation": {"landscape": 9, "portrait": 1},
    "not_indexed": ["a.mov", "b.mov"],
}


# ------------------------------------------------------------------ classify_intent
@pytest.mark.parametrize("text", [
    "make something cool with my trip", "make a vlog from my trip", "edit my footage into a reel", "cut a trailer", "make me a montage",
    "make a cinematic film from these clips", "create a tiktok", "turn this into a youtube short", "make a promo video",
    "surprise me", "edit my trip", "make an ad for my shop", "build a music video",
])
def test_whole_piece_asks_on_an_empty_timeline_are_finished_pieces(text):
    assert EC.classify_intent(text, timeline_empty=True, media_minutes=40) == EC.MODE_FINISHED


@pytest.mark.parametrize("text", [
    "make it warmer", "cut this clip", "add captions", "make it snappy", "trim the start", "undo that", "export it", "make it louder",
    "rotate this clip", "remove the silence", "zoom in a bit", "make it shorter", "change the title text", "speed up this clip",
])
def test_narrow_asks_are_always_free(text):
    assert EC.classify_intent(text, timeline_empty=True, media_minutes=40) == EC.MODE_FREE
    assert EC.classify_intent(text, timeline_empty=False, media_minutes=40) == EC.MODE_FREE


@pytest.mark.parametrize("text", ["make a vlog", "make something cool with my trip", "cinematic reel please"])
def test_whole_piece_wording_on_an_edit_under_way_is_a_tweak_until_the_user_restarts(text):
    assert EC.classify_intent(text, timeline_empty=False, media_minutes=40) == EC.MODE_FREE


def test_a_restart_phrase_reopens_the_contract_on_a_busy_timeline():
    assert EC.classify_intent("start over and make a vlog from scratch", timeline_empty=False, media_minutes=40) == EC.MODE_FINISHED


def test_nothing_to_survey_is_free_so_begin_edit_can_say_import_first():
    assert EC.classify_intent("make a vlog", timeline_empty=True, media_minutes=0) == EC.MODE_FREE


@pytest.mark.parametrize("text", ["recreate this video from my footage", "remake it like this video", "re-create the reference"])
def test_recreate_wording_is_its_own_mode(text):
    assert EC.classify_intent(text, timeline_empty=True, media_minutes=40) == EC.MODE_RECREATE


@pytest.mark.parametrize("text", ["", "   ", "hello", "what is in my footage?", "how long is the second clip", "thanks", "add a title"])
def test_questions_and_chatter_are_free(text):
    assert EC.classify_intent(text, timeline_empty=True, media_minutes=40) == EC.MODE_FREE


def test_the_word_add_is_not_the_word_ad():
    assert EC.classify_intent("add the clips to the timeline", timeline_empty=True, media_minutes=40) == EC.MODE_FREE


# ------------------------------------------------------------------ readiness
def test_readiness_tiers():
    assert EC.readiness(0, 0) == "none"
    assert EC.readiness(40, 0) == "none"                              # everything queued: wait
    assert EC.readiness(40, 12) == "partial"
    assert EC.readiness(40, 28) == "structure"                        # 70%
    assert EC.readiness(40, 40, searchable=30) == "searchable"
    assert EC.readiness(40, 40, searchable=40, rich=30) == "rich"


def test_enough_structured_minutes_beats_the_fraction():
    assert EC.readiness(40, 12, structured_minutes=30, needed_minutes=20) == "structure"
    assert EC.readiness(40, 12, structured_minutes=10, needed_minutes=20) == "partial"


def test_partial_is_allowed_only_after_the_agent_has_waited():
    assert not EC.can_start("none", 99)
    assert not EC.can_start("partial", 0)
    assert not EC.can_start("partial", EC.WAIT_ATTEMPTS_BEFORE_PARTIAL_OK - 2)
    assert EC.can_start("partial", EC.WAIT_ATTEMPTS_BEFORE_PARTIAL_OK - 1)      # told to wait twice: the third call goes ahead
    assert EC.can_start("partial", EC.WAIT_ATTEMPTS_BEFORE_PARTIAL_OK)
    assert all(EC.can_start(t, 0) for t in ("structure", "searchable", "rich"))


# ------------------------------------------------------------------ forms and brief defaults
def test_the_fallback_forms_match_review_forms():
    from classes.media_index import review
    assert tuple(EC._FALLBACK_FORMS) == tuple(review.FORMS)
    assert EC._FALLBACK_MAX_SECONDS == review.FORM_MAX_SECONDS


@pytest.mark.parametrize("text,form", [("YouTube vlog", "vlog"), ("tik tok", "tiktok"), ("an Instagram reel", "reel"), ("youtube short", "short"),
                                       ("a short film", "film"), ("podcast", "podcast"), ("my trip", "vlog"), ("a promo", "promo")])
def test_free_text_forms_map_to_a_known_form(text, form):
    assert EC.normalize_form(text) == form and form in EC.known_forms()


def test_make_it_shorter_is_not_the_short_form():
    assert EC.normalize_form("make it shorter") == ""


def test_default_brief_is_always_review_compatible():
    for ask in ("make something cool with my trip", "a cinematic warm BTS vlog", "tiktok of the concert", "a podcast clip", ""):
        brief, _q, _i = EC.default_brief(SURVEY, user_ask=ask)
        assert brief["form"] in EC.known_forms()
        assert brief["target_seconds"] > 0 and EC.brief_ready(brief)


def test_default_brief_chooses_and_reports_what_it_assumed():
    brief, questions, inferred = EC.default_brief(SURVEY, user_ask="make something cool with my trip")
    assert brief["form"] == "vlog" and brief["aspect"] == "16:9" and brief["wants_music"] is True and brief["wants_captions"] is False
    assert brief["opening"]["file_id"] == "f1"
    assert {"target_seconds", "aspect", "opening", "beat_map"} <= set(inferred)
    assert "form" not in inferred                                      # "my trip" is the user's own word for it
    assert 45 <= brief["target_seconds"] <= 300
    assert brief["beat_map"] and all("label" in a for a in brief["beat_map"])
    assert questions == []                                             # 40 usable minutes: the 12.5% prior is clear enough, do not interview
    huge = dict(SURVEY, totals=dict(SURVEY["totals"], usable_minutes=240.0))
    _b, q2, _i = EC.default_brief(huge, user_ask="make something cool with my trip")
    assert q2 and q2[0]["id"] == "length"                              # 4 h of raw: 45 s vs 8 min is a real choice
    assert EC.default_brief(huge, user_ask="make a 2 minute vlog")[1] == []


@pytest.mark.parametrize("text,seconds", [("a 2 minute vlog", 120), ("make it 45s", 45), ("90 sec reel", 90), ("about 3 min", 180),
                                          ("1.5 minutes", 90), ("a 10 s clip", 10)])
def test_a_length_the_user_said_is_parsed(text, seconds):
    assert EC.parse_length_seconds(text) == seconds


@pytest.mark.parametrize("text", ["", "make a vlog", "my 3 clips", "1080p video", "2 days in Lisbon", "1 second", "99999 minutes", "version 2.0"])
def test_no_length_when_none_is_said(text):
    assert EC.parse_length_seconds(text) is None


def test_the_stated_length_becomes_the_target_and_is_not_an_assumption():
    brief, questions, inferred = EC.default_brief(SURVEY, user_ask="make a 2 minute vlog of my trip")
    assert brief["target_seconds"] == 120 and "target_seconds" not in inferred and questions == []
    assert EC.default_brief(SURVEY, user_ask="a 2 minute vlog", target_seconds=60)[0]["target_seconds"] == 60.0   # an explicit argument wins


def test_a_form_the_ask_never_named_is_reported_as_assumed():
    brief, _q, inferred = EC.default_brief(SURVEY, user_ask="make something cool")
    assert brief["form"] == "vlog" and "form" in inferred              # two trip days, no form word: assumed, and said so
    portrait = dict(SURVEY, orientation={"portrait": 10}, trip={"days": []})
    assert EC.default_brief(portrait, user_ask="make something cool")[0]["form"] == "reel"
    assert EC.default_brief(dict(SURVEY, trip={"days": []}), user_ask="make something cool")[0]["form"] == "montage"


def test_the_user_setting_beats_every_default():
    brief, questions, inferred = EC.default_brief(SURVEY, form="reel", target_seconds=45, aspect="1:1", vibe="warm and quiet")
    assert (brief["form"], brief["target_seconds"], brief["aspect"], brief["wants_grade"]) == ("reel", 45.0, "1:1", True)
    assert "target_seconds" not in inferred and "aspect" not in inferred and not questions


def test_short_forms_are_capped_and_vertical():
    brief, _q, _i = EC.default_brief(SURVEY, form="short")
    assert brief["target_seconds"] <= 60 and brief["aspect"] == "9:16" and brief["wants_captions"] is True


def test_cinematic_bts_gets_the_three_to_four_minute_prior():
    brief, _q, _i = EC.default_brief(SURVEY, user_ask="cinematic warm BTS", form="vlog")
    assert 180 <= brief["target_seconds"] <= 240


def test_an_empty_survey_still_gives_a_usable_brief():
    brief, _q, _i = EC.default_brief({})
    assert EC.brief_ready(brief) and brief["form"] == "montage"


def test_brief_ready_needs_form_and_length():
    assert not EC.brief_ready(None) and not EC.brief_ready({}) and not EC.brief_ready({"form": "vlog"})
    assert not EC.brief_ready({"target_seconds": 60}) and EC.brief_ready({"form": "vlog", "target_seconds": 60})


# ------------------------------------------------------------------ digest
def test_the_digest_stays_under_its_cap_even_for_a_huge_survey():
    big = dict(SURVEY)
    big["top_moments"] = [{"file_id": f"f{i}", "name": "x" * 80, "start": 1.0, "end": 2.0, "highlight": 0.5, "why": "w" * 300} for i in range(60)]
    big["trip"] = {"days": [{"day": i, "date": "d", "minutes": 1, "clips": 1, "places": ["p" * 40] * 5} for i in range(60)]}
    big["music"] = [{"file_id": f"m{i}", "name": "n" * 60, "bpm": 100} for i in range(40)]
    big["not_indexed"] = ["n" * 90] * 50
    for cap in (EC.DIGEST_CAP_CHARS, 2000):
        assert len(json.dumps(EC.build_digest(big, {"tier": "structure"}, cap=cap), ensure_ascii=False)) <= cap


def test_a_normal_survey_digest_keeps_its_best_moments():
    d = EC.build_digest(SURVEY, {"tier": "structure"})
    assert d["top_moments"][0]["file_id"] == "f1" and d["readiness"] == {"tier": "structure"}


# ------------------------------------------------------------------ the state machine
def test_unknown_or_junk_contracts_are_free():
    for junk in (None, {}, "x", 3, {"version": 99, "mode": "finished_piece"}, {"mode": "finished_piece"}):
        c = EC.normalize(junk)
        assert c["mode"] == EC.MODE_FREE and c["phase"] == "idle"


def test_advance_never_mutates_its_input():
    c = EC.new_contract(EC.MODE_FINISHED)
    before = json.dumps(c, sort_keys=True)
    EC.advance(c, "begin_ok", ready=True)
    assert json.dumps(c, sort_keys=True) == before


def test_the_happy_path_walks_every_phase():
    c = EC.new_contract(EC.MODE_FINISHED)
    c = EC.advance(c, "begin_ok", mode=EC.MODE_FINISHED, readiness={"tier": "structure"}, ready=True)
    assert c["phase"] == "briefed"
    c = EC.apply_observation(c, "add_clips_to_timeline_tool", "applied")
    assert c["phase"] == "assembling"
    c = EC.advance(c, "finish_layer")
    assert c["phase"] == "finishing"
    c = EC.apply_observation(c, "review_edit_tool", "applied", needs=["music is too loud"])
    assert c["phase"] == "audited" and c["audit"]["rounds"] == 1 and c["audit"]["needs"] == ["music is too loud"]
    c = EC.apply_observation(c, "review_edit_tool", "applied", needs=[])
    assert c["phase"] == "done" and c["audit"]["rounds"] == 2


def test_the_audit_round_cap_ends_the_loop_and_keeps_the_open_needs():
    c = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    for _ in range(EC.MAX_AUDIT_ROUNDS):
        c = EC.advance(c, "review_done", needs=["still dark"])
    assert c["phase"] == "done" and c["audit"]["open_needs"] == ["still dark"]


def test_a_failed_or_refused_call_changes_nothing():
    c = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    for status in ("refused", "error"):
        assert EC.apply_observation(c, "add_clips_to_timeline_tool", status)["phase"] == "briefed"
        assert EC.apply_observation(c, "review_edit_tool", status, needs=["x"])["audit"]["rounds"] == 0


def test_waiting_counts_attempts_and_a_later_start_resets_them():
    c = EC.new_contract(EC.MODE_FINISHED)
    for i in range(3):
        c = EC.advance(c, "begin_wait", readiness={"tier": "none"})
        assert c["phase"] == "surveying" and c["attempts"] == i + 1
    c = EC.advance(c, "begin_ok", ready=True)
    assert c["phase"] == "briefed" and c["attempts"] == 0


def test_begin_ok_in_the_middle_of_the_job_does_not_move_the_phase_backwards():
    c = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    for phase_event in ("placed", "finish_layer"):
        c = EC.advance(c, phase_event)
    before = c["phase"]
    assert before == "finishing" and EC.advance(c, "begin_ok", ready=True)["phase"] == "finishing"


def test_brief_saved_lifts_the_phase_only_when_the_brief_is_ready():
    c = EC.new_contract(EC.MODE_FINISHED)
    assert EC.advance(c, "brief_saved", brief={"form": "vlog"})["phase"] == "idle"
    assert EC.advance(c, "brief_saved", brief={"form": "vlog", "target_seconds": 90})["phase"] == "briefed"


def test_user_free_and_new_piece():
    c = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    assert EC.advance(c, "user_free")["mode"] == EC.MODE_FREE
    c = EC.advance(c, "review_done", needs=["x"])
    fresh = EC.advance(c, "new_piece")
    assert fresh["phase"] == "idle" and fresh["audit"]["rounds"] == 0 and fresh["mode"] == EC.MODE_FINISHED


def test_unknown_events_change_nothing():
    c = EC.new_contract(EC.MODE_FINISHED)
    assert EC.advance(c, "nonsense") == EC.normalize(c)


def test_to_json_round_trips_and_is_size_capped():
    c = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    assert EC.normalize(json.loads(json.dumps(EC.to_json(c)))) == c
    huge = dict(c, readiness={"x": "y" * (EC.CONTRACT_MAX_BYTES * 2)})
    with pytest.raises(ValueError):
        EC.to_json(huge)


# ------------------------------------------------------------------ policy: every cell of the matrix
PLACEMENT = sorted(EC.PLACEMENT_TOOLS)
OTHER = ["trim_clips_tool", "color_grade_clip_tool", "add_captions_tool", "export_video_tool", "get_project_overview_tool",
         "search_footage_tool", "begin_edit_tool", "set_edit_brief_tool", "review_edit_tool", "undo_tool", "import_files_tool",
         "generate_video_and_add_to_timeline_tool", "import_stock_media_tool", "slice_clips_tool", "remove_gaps_tool"]


def contract(mode, phase):
    c = EC.new_contract(mode)
    c["phase"] = phase
    return c


@pytest.mark.parametrize("mode", [EC.MODE_FREE, EC.MODE_RECREATE])
@pytest.mark.parametrize("phase", EC.PHASES)
@pytest.mark.parametrize("tool", PLACEMENT + OTHER)
@pytest.mark.parametrize("rollout", EC.ROLLOUT_MODES)
def test_only_finished_pieces_are_ever_gated(mode, phase, tool, rollout):
    assert EC.check_policy(contract(mode, phase), tool, rollout=rollout).action == "allow"


@pytest.mark.parametrize("phase", EC.PHASES)
@pytest.mark.parametrize("tool", OTHER)
@pytest.mark.parametrize("rollout", EC.ROLLOUT_MODES)
def test_non_placement_tools_are_never_gated_even_in_a_finished_piece(phase, tool, rollout):
    assert EC.check_policy(contract(EC.MODE_FINISHED, phase), tool, rollout=rollout).action == "allow"


@pytest.mark.parametrize("tool", PLACEMENT)
def test_placement_before_a_brief_is_refused_when_enforced_and_warned_by_default(tool):
    for phase in EC.PRE_BRIEF_PHASES:
        v = EC.check_policy(contract(EC.MODE_FINISHED, phase), tool, rollout="enforce")
        assert v.action == "refuse" and "begin_edit_tool" in v.message and "set_edit_brief_tool" in v.message
        assert EC.check_policy(contract(EC.MODE_FINISHED, phase), tool).action == "warn"            # default rollout
        assert EC.check_policy(contract(EC.MODE_FINISHED, phase), tool, rollout="off").action == "allow"


@pytest.mark.parametrize("tool", PLACEMENT)
@pytest.mark.parametrize("phase", [p for p in EC.PHASES if p not in EC.PRE_BRIEF_PHASES])
def test_placement_after_the_brief_is_allowed(tool, phase):
    assert EC.check_policy(contract(EC.MODE_FINISHED, phase), tool, rollout="enforce").action == "allow"


def test_a_brief_saved_with_set_edit_brief_tool_unlocks_placement_without_begin_edit():
    c = contract(EC.MODE_FINISHED, "idle")
    assert EC.check_policy(c, "add_clips_to_timeline_tool", rollout="enforce").action == "refuse"
    assert EC.check_policy(c, "add_clips_to_timeline_tool", rollout="enforce", brief={"form": "vlog", "target_seconds": 120}).action == "allow"
    assert EC.check_policy(c, "add_clips_to_timeline_tool", rollout="enforce", brief={"form": "vlog"}).action == "refuse"


def test_the_user_can_always_edit_freely():
    c = contract(EC.MODE_FINISHED, "idle")
    c["overrides"] = {"allow_assemble_without_brief": True}
    assert EC.check_policy(c, "add_clips_to_timeline_tool", rollout="enforce").action == "allow"


def test_the_refusal_explains_a_slow_index_and_names_only_real_core_tools():
    c = contract(EC.MODE_FINISHED, "surveying")
    c["readiness"] = {"tier": "none", "total": 40, "structured": 12}
    msg = EC.check_policy(c, "add_clips_to_timeline_tool", rollout="enforce").message
    assert "12/40" in msg and "begin_edit_tool" in msg and msg.startswith("edit contract:")
    from classes.agent_tools.core_tools import CORE_TOOL_NAMES
    import re
    named = set(re.findall(r"\b[a-z][a-z_]*_tool\b", msg))
    assert named <= set(CORE_TOOL_NAMES) | {"begin_edit_tool", "set_edit_brief_tool", "get_edit_brief_tool"}   # the three Phase 2 makes core


def test_garbage_contracts_never_block():
    for junk in (None, "x", {}, {"mode": "finished_piece"}, {"version": 7}):
        assert EC.check_policy(junk, "add_clips_to_timeline_tool", rollout="enforce").action == "allow"


@pytest.mark.parametrize("env,expected", [({}, "warn"), ({"ZENVI_EDIT_CONTRACT": "off"}, "off"), ({"ZENVI_EDIT_CONTRACT": " ENFORCE "}, "enforce"),
                                          ({"ZENVI_EDIT_CONTRACT": "warn"}, "warn"), ({"ZENVI_EDIT_CONTRACT": "yes"}, "warn"),
                                          ({"ZENVI_EDIT_CONTRACT": ""}, "warn")])
def test_the_rollout_flag_defaults_to_warn_and_ignores_typos(env, expected):
    assert EC.rollout_setting(env) == expected


def test_finishing_tools_move_a_piece_to_finishing_and_only_when_they_applied():
    c = EC.advance(EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True), "placed")
    for tool in sorted(EC.FINISH_TOOLS):
        assert EC.apply_observation(c, tool, "applied")["phase"] == "finishing"
        assert EC.apply_observation(c, tool, "refused")["phase"] == "assembling"
    briefed = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    assert EC.apply_observation(briefed, "color_grade_clip_tool", "applied")["phase"] == "briefed", "grading before placing is not finishing"


def test_only_the_tools_that_matter_are_observed():
    assert EC.OBSERVED_TOOLS == EC.PLACEMENT_TOOLS | EC.FINISH_TOOLS | {"review_edit_tool", "export_video_tool"}
    assert "get_project_overview_tool" not in EC.OBSERVED_TOOLS and "undo_tool" not in EC.OBSERVED_TOOLS


def test_every_tool_the_contract_names_is_a_real_registered_tool():
    from classes import tool_handlers
    from classes.editor_tools import REGISTRY
    real = set(REGISTRY) | set(tool_handlers.AGENT_TOOL_HANDLERS)
    assert not (EC.OBSERVED_TOOLS | EC.PLACEMENT_TOOLS) - real, sorted((EC.OBSERVED_TOOLS | EC.PLACEMENT_TOOLS) - real)


# ------------------------------------------------------------------ export warning and snapshot line
def test_export_is_never_refused_only_warned():
    assert EC.export_warning(contract(EC.MODE_FREE, "idle")) is None
    assert "not audited" in EC.export_warning(contract(EC.MODE_FINISHED, "assembling"))
    done = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    done = EC.advance(done, "review_done", needs=[])
    assert EC.export_warning(done) is None
    open_needs = EC.advance(EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True), "review_done", needs=["dead air at 0:41"])
    assert "dead air" in EC.export_warning(open_needs)


def test_the_edit_state_line_is_short_and_says_what_the_agent_needs():
    c = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    line = EC.edit_state_line(c, {"form": "vlog", "target_seconds": 210, "vibe": "warm cinematic"}, "index_v2: videos=32 structure=28")
    assert "finished_piece/briefed" in line and "form=vlog" in line and "target=210s" in line and "structure=28" in line
    assert len(EC.edit_state_line(c, {"form": "v", "vibe": "x" * 500}, "i" * 900)) <= 600
    assert EC.edit_state_line(None, None) == "contract=free/idle"
