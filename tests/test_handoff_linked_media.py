"""classes.handoff.linked_media: the zenvi_link schema, providers and the one-undo-step operations."""

import json
import os
import threading

import pytest

from classes.handoff import jobs
from classes.handoff import linked_media as lm
from handoff_fakes import FakeProvider, linked, remotion_link, tt  # noqa: F401  (fixtures)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_normalize_fills_defaults_and_keeps_unknown_keys(tmp_path):
    link = lm.normalize_link({"kind": "Remotion", "source": {"project_dir": str(tmp_path / "proj"),
                                                             "file": str(tmp_path / "proj" / "src" / "A.tsx")},
                              "custom": {"from": "zenvi-web"}})
    assert link["kind"] == "remotion" and link["version"] == 1 and link["state"] == "fresh"
    assert link["error"] is None and link["props"] == {} and link["custom"] == {"from": "zenvi-web"}
    assert link["source"]["file"] == "src/A.tsx" and link["source"]["line"] is None
    assert set(link["render"]) == set(lm.RENDER_KEYS)


@pytest.mark.parametrize("bad, words", [
    ({"kind": "flash"}, "kind must be one of"),
    ({"kind": "remotion", "source": {"path": "/x"}}, "must not use the key 'path'"),
    ({"kind": "remotion", "render": {"codec": "vp9"}}, "never WebM"),
    ({"kind": "remotion", "render": {"fps": {"num": 0, "den": 1}}}, "positive"),
    ({"kind": "remotion", "source": {"line": 0}}, "1-based"),
    ({"kind": "remotion", "state": "melted"}, "state must be one of"),
    ({"kind": "remotion", "version": 2}, "link version 1"),
    ("nope", "must be an object"),
])
def test_normalize_refuses_what_save_or_exporters_would_mangle(bad, words):
    with pytest.raises(lm.LinkError, match=words):
        lm.normalize_link(bad)


def test_props_with_path_like_keys_are_encoded_and_survive_a_save_load_round_trip(tmp_path):
    props = {"image": "https://cdn.example.com/logo.png", "nested": {"path": "a/b"}, "title": "Hi"}
    stored = lm.normalize_link(dict(remotion_link(props=props), render={"output": "@assets/links/remotion/x.mov"}))
    assert set(stored["props"]) == {lm.PROPS_ESCAPE_KEY}
    assert lm.read_link({"zenvi_link": stored})["props"] == props

    from classes.json_data import JsonDataStore
    project_file = str(tmp_path / "proj" / "trip.zvn")
    media = str(tmp_path / "proj" / "trip_assets" / "links" / "remotion" / "Intro-3f9a1c2b.mov")
    data = {"files": [{"id": "F1", "path": media, "zenvi_link": stored}]}
    store = JsonDataStore()
    text = store.convert_paths_to_relative(project_file, None, json.dumps(data))
    saved = json.loads(text)
    assert saved["files"][0]["path"] == "@assets/links/remotion/Intro-3f9a1c2b.mov"
    loaded = json.loads(store.convert_paths_to_absolute(project_file, text))
    assert loaded["files"][0]["path"] == media
    assert loaded["files"][0]["zenvi_link"] == stored
    assert lm.read_link(loaded["files"][0])["props"] == props


def test_fps_forms_and_fingerprint_helpers(tmp_path):
    from fractions import Fraction
    assert lm.normalize_link({"kind": "remotion", "render": {"fps": 29.97}})["render"]["fps"] == {"num": 2997,
                                                                                                    "den": 100}
    assert lm.normalize_link({"kind": "remotion", "render": {"fps": Fraction(30000, 1001)}})["render"]["fps"] == {
        "num": 30000, "den": 1001}
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "node_modules").mkdir()
    (root / "src" / "A.tsx").write_text("export const A = 1;")
    (root / "node_modules" / "x.js").write_text("ignored")
    first = lm.fingerprint_sources(str(root), extra={"props": {"a": 1}})
    (root / "node_modules" / "x.js").write_text("still ignored")
    os.utime(root / "src" / "A.tsx", (1, 1))  # touched, same content
    assert lm.fingerprint_sources(str(root), extra={"props": {"a": 1}}) == first
    assert lm.fingerprint_sources(str(root), extra={"props": {"a": 2}}) != first
    (root / "src" / "A.tsx").write_text("export const A = 2;")
    assert lm.fingerprint_sources(str(root), extra={"props": {"a": 1}}) != first
    with pytest.raises(lm.SourceMissing):
        lm.fingerprint_sources(str(tmp_path / "gone"))
    assert lm.short_fingerprint("sha256:3f9a1c2bdeadbeef") == "3f9a1c2b"


def test_provider_registry():
    provider = FakeProvider(kind="hyperframes", label="HyperFrames")
    lm.register_provider(provider)
    try:
        assert lm.provider_for("HyperFrames") is provider and "hyperframes" in lm.registered_kinds()
        assert lm.supports_studio("hyperframes") and lm.kind_label("hyperframes") == "HyperFrames"
        with pytest.raises(ValueError):
            lm.register_provider(object())
    finally:
        lm.unregister_provider("hyperframes")
    assert lm.provider_for("hyperframes") is None
    with pytest.raises(lm.LinkError, match="cannot render"):
        lm.require_provider("hyperframes")


def test_output_tokens_and_render_names(tmp_path):
    project = str(tmp_path / "trip.zvn")
    inside = str(tmp_path / "trip_assets" / "links" / "remotion" / "Intro-1.mov")
    assert lm.output_token(inside, project) == "@assets/links/remotion/Intro-1.mov"
    assert lm.output_token(str(tmp_path / "elsewhere.mov"), project) == str(tmp_path / "elsewhere.mov")
    assert lm.links_dir("remotion", project) == str(tmp_path / "trip_assets" / "links" / "remotion")
    name = lm.render_file_name({"source": {"composition": "My Intro!"}}, "sha256:3f9a1c2b00", ".MOV")
    assert name == "My-Intro-3f9a1c2b.mov"


# ---------------------------------------------------------------------------
# Project operations (one undo step each; refusals leave history alone)
# ---------------------------------------------------------------------------

def test_add_linked_media_is_one_undo_step_over_the_video(linked):
    video = linked.add_file("video")
    linked.add_clip(video, position=0.0, layer=1000000)
    path = linked.media("Intro-3f9a1c2b.mov", seconds=4.0)
    out = lm.add_linked_media(path, remotion_link(), position=1.02)
    assert linked.undo_steps_since_mark() == 1
    f = linked.file(out["file_id"])
    assert f["zenvi_link"]["kind"] == "remotion" and f["name"] == "Intro" and f["path"] == path
    clip = linked.clip(out["timeline_clip_id"])
    assert clip["layer"] == 2000000 and out["new_track"] is False  # the first free track above the video
    assert clip["position"] == pytest.approx(31 / 30) and out["position"] == 1.033  # snapped to the 30 fps grid
    assert clip["end"] - clip["start"] == pytest.approx(4.0)
    linked.undo()
    assert linked.file(out["file_id"]) is None and linked.clip(out["timeline_clip_id"]) is None


@pytest.mark.parametrize("make_path, link, words", [
    (lambda lk: lk.media("x.webm"), remotion_link(), "cannot be WebM"),
    (lambda lk: "/nope/missing.mov", remotion_link(), "no rendered media"),
    (lambda lk: lk.media("ok.mov"), {"kind": "remotion", "source": {"image": "x"}}, "must not use the key"),
])
def test_add_linked_media_refusals_change_nothing(linked, make_path, link, words):
    path = make_path(linked)
    with pytest.raises(lm.LinkError, match=words):
        lm.add_linked_media(path, link)
    assert linked.undo_steps_since_mark() == 0 and not linked.get("files")


def test_swap_to_a_shorter_render_clamps_clips_and_warns(linked):
    out = lm.add_linked_media(linked.media("a.mov", seconds=6.0), remotion_link(), position=0.0)
    clip_id = out["timeline_clip_id"]
    linked.mark()
    new_path = linked.media("b.mov", seconds=3.0)
    res = lm.swap_linked_media(out["file_id"], new_path, {"props": {"title": "Bye"}, "render": {"codec": "h264"}})
    assert linked.undo_steps_since_mark() == 1
    assert res["warnings"] and "shortened" in res["warnings"][0]
    clip = linked.clip(clip_id)
    assert clip["end"] == pytest.approx(3.0) and clip["reader"]["path"] == new_path
    f = linked.file(out["file_id"])
    assert f["path"] == new_path and f["zenvi_link"]["props"] == {"title": "Bye"}
    assert f["zenvi_link"]["render"]["codec"] == "h264" and f["zenvi_link"]["source"]["composition"] == "Intro"
    linked.undo()
    f = linked.file(out["file_id"])
    assert f["path"].endswith("a.mov") and f["zenvi_link"]["props"] == {"title": "Hello"}
    assert linked.clip(clip_id)["end"] == pytest.approx(6.0)


def test_swap_moves_a_clip_that_would_start_past_the_new_end(linked):
    out = lm.add_linked_media(linked.media("a.mov", seconds=10.0), remotion_link(), position=0.0)
    cid = out["timeline_clip_id"]
    from classes.query import Clip
    c = Clip.get(id=cid)
    c.data.update(start=7.0, end=9.0)
    c.save()
    linked.mark()
    res = lm.swap_linked_media(out["file_id"], linked.media("b.mov", seconds=4.0), {})
    clip = linked.clip(cid)
    assert (clip["start"], clip["end"]) == (pytest.approx(2.0), pytest.approx(4.0))
    assert "started past the end" in res["warnings"][0] and linked.undo_steps_since_mark() == 1


def test_update_link_and_no_op(linked):
    out = lm.add_linked_media(linked.media("a.mov"), remotion_link(), position=0.0)
    linked.mark()
    res = lm.update_link(out["file_id"], {"props": {"title": "New"}})
    assert res["changed"] and res["link"]["props"] == {"title": "New"} and linked.undo_steps_since_mark() == 1
    clip = linked.clip(out["timeline_clip_id"])
    assert clip["reader"]["zenvi_link"]["props"] == {"title": "New"}  # readers follow the file
    linked.mark()
    assert lm.update_link(out["file_id"], {"props": {"title": "New"}})["changed"] is False
    assert linked.undo_steps_since_mark() == 0


def test_unlink_keeps_media_and_undo_restores_the_link(linked):
    out = lm.add_linked_media(linked.media("a.mov"), remotion_link(), position=0.0)
    linked.mark()
    res = lm.unlink(out["file_id"])
    assert res["kind"] == "remotion" and res["clip_ids"] == [out["timeline_clip_id"]]
    f = linked.file(out["file_id"])
    assert "zenvi_link" not in f and os.path.isfile(f["path"])
    assert "zenvi_link" not in linked.clip(out["timeline_clip_id"])["reader"]
    assert linked.undo_steps_since_mark() == 1
    linked.undo()
    assert linked.file(out["file_id"])["zenvi_link"]["kind"] == "remotion"
    linked.redo()
    assert "zenvi_link" not in linked.file(out["file_id"])
    with pytest.raises(lm.LinkError, match="not a linked clip"):
        lm.unlink(out["file_id"])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_render_link_stages_then_installs(linked):
    provider = FakeProvider(probe=linked.probe)
    lm.register_provider(provider)
    seen = {}
    provider.during_render = lambda link, out_dir: seen.update(out_dir=out_dir)
    path, link = lm.render_link(remotion_link())
    folder = os.path.join(linked.user_path, "links", "remotion")
    assert os.path.dirname(path) == folder and os.path.basename(path).startswith("Intro-")
    assert os.path.dirname(seen["out_dir"]) == folder and not os.path.exists(seen["out_dir"])
    render = link["render"]
    assert render["fingerprint"] == provider.fingerprint(remotion_link() | {"props": {"title": "Hello"}})
    assert render["output"] == "@assets/links/remotion/" + os.path.basename(path)
    assert render["codec"] == "prores4444" and render["duration_frames"] == 150 and link["state"] == "fresh"
    # the same render again lands next to it, never over a file that may be in use
    path2, _ = lm.render_link(remotion_link())
    assert path2 != path and os.path.isfile(path)


@pytest.mark.parametrize("configure, error", [
    (lambda p: setattr(p, "fail", RuntimeError("tsc: type error")), RuntimeError),
    (lambda p: (setattr(p, "codec", "vp9"), setattr(p, "ext", ".webm")), lm.LinkError),
])
def test_failed_renders_leave_nothing_behind(linked, configure, error):
    provider = FakeProvider(probe=linked.probe)
    configure(provider)
    lm.register_provider(provider)
    with pytest.raises(error):
        lm.render_link(remotion_link())
    folder = os.path.join(linked.user_path, "links", "remotion")
    assert os.listdir(folder) == []


def test_rerender_is_one_undo_step_and_reports_rendering_while_it_runs(linked):
    provider = FakeProvider(probe=linked.probe, seconds=5.0)
    lm.register_provider(provider)
    out = lm.import_linked(remotion_link(), position=0.0)
    file_id = out["file_id"]
    assert lm.link_state(linked.file(file_id)) == "fresh"
    states = []
    provider.during_render = lambda link, out_dir: states.append(lm.link_state(linked.file(file_id),
                                                                               compute=False))
    linked.mark()
    res = lm.rerender_linked(file_id, props={"title": "Again"})
    assert states == ["rendering"] and linked.undo_steps_since_mark() == 1
    assert res["props"] == {"title": "Again"} and provider.renders[-1]["props"] == {"title": "Again"}
    f = linked.file(file_id)
    assert f["zenvi_link"]["props"] == {"title": "Again"} and lm.link_state(f) == "fresh"
    linked.undo()
    assert linked.file(file_id)["zenvi_link"]["props"] == {"title": "Hello"}


def test_failed_rerender_leaves_history_and_marks_error(linked):
    provider = FakeProvider(probe=linked.probe)
    lm.register_provider(provider)
    out = lm.import_linked(remotion_link(), position=0.0)
    linked.mark()
    provider.fail = RuntimeError("Composition 'Intro' not found")
    with pytest.raises(RuntimeError):
        lm.rerender_linked(out["file_id"])
    assert linked.undo_steps_since_mark() == 0
    check = lm.check_link(linked.file(out["file_id"]))
    assert check.state == "error" and "not found" in check.detail
    provider.fail = None
    lm.rerender_linked(out["file_id"])
    assert lm.link_state(linked.file(out["file_id"])) == "fresh"


def test_rerender_can_be_cancelled_without_an_undo_step(linked):
    provider = FakeProvider(probe=linked.probe)
    lm.register_provider(provider)
    out = lm.import_linked(remotion_link(), position=0.0)
    linked.mark()
    provider.gate = threading.Event()
    result = {}

    def _run():
        try:
            lm.rerender_linked(out["file_id"])
        except jobs.JobCancelled as exc:
            result["cancelled"] = exc

    t = threading.Thread(target=_run)
    t.start()
    for _ in range(500):
        job = jobs.job_for(out["file_id"])
        if job is not None:
            break
        threading.Event().wait(0.01)
    assert job is not None and job.cancel()
    t.join(10)
    assert "cancelled" in result and linked.undo_steps_since_mark() == 0
    assert lm.link_state(linked.file(out["file_id"]), compute=False) == "fresh"


def test_check_link_states(linked, tmp_path):
    provider = FakeProvider(probe=linked.probe)
    lm.register_provider(provider)
    src = tmp_path / "remotion-project"
    src.mkdir()
    out = lm.import_linked(remotion_link(project_dir=str(src)), position=0.0)
    f = lambda: linked.file(out["file_id"])  # noqa: E731
    assert lm.link_state(f()) == "fresh"
    provider.version = 2  # the code changed
    check = lm.check_link(f())
    assert check.state == "stale" and check.fingerprint != check.stored_fingerprint
    src.rmdir()
    assert lm.link_state(f()) == "missing_source"
    assert lm.check_link({"id": "x", "path": "/a.mov"}) is None
    lm.unregister_provider("remotion")
    src.mkdir()
    check = lm.check_link(f())
    assert check.state == "fresh" and "no remotion provider" in check.detail


# ---------------------------------------------------------------------------
# Save: renders of an unsaved project move into <project>_assets/links
# ---------------------------------------------------------------------------

def _write(path, body=b"render"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body)


def test_adopt_moves_unsaved_renders_and_copies_on_save_as(tmp_path, monkeypatch):
    from classes import info
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    src = str(tmp_path / "user" / "links" / "remotion" / "Intro-1.mov")
    plain = str(tmp_path / "user" / "generated" / "x.mp4")
    _write(src)
    _write(plain)
    link = lm.normalize_link(remotion_link())
    files = [{"id": "F1", "path": src, "zenvi_link": link}, {"id": "F2", "path": plain},
             {"id": "F3", "path": src, "zenvi_link": link}]
    clips = [{"id": "C1", "file_id": "F1", "reader": {"path": src}},
             {"id": "C2", "file_id": "F2", "reader": {"path": plain}}]
    project = str(tmp_path / "Trip.zvn")
    moves = lm.adopt_linked_renders(files, clips, project)
    dest = str(tmp_path / "Trip_assets" / "links" / "remotion" / "Intro-1.mov")
    assert moves == [(src, dest)] and os.path.isfile(dest) and not os.path.exists(src)
    assert files[0]["path"] == dest and files[2]["path"] == dest and clips[0]["reader"]["path"] == dest
    assert files[1]["path"] == plain and clips[1]["reader"]["path"] == plain

    # Save As: copied, the old project keeps its render
    project2 = str(tmp_path / "Trip v2.zvn")
    moves = lm.adopt_linked_renders(files, clips, project2, previous_path=project)
    dest2 = str(tmp_path / "Trip v2_assets" / "links" / "remotion" / "Intro-1.mov")
    assert moves == [] and os.path.isfile(dest) and os.path.isfile(dest2) and files[0]["path"] == dest2


def test_save_rolls_back_adopted_renders_when_the_write_fails(tmp_path, monkeypatch):
    import openshot
    from classes import info
    from classes import project_data as pd
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    src = str(tmp_path / "user" / "links" / "remotion" / "Intro-1.mov")
    _write(src)
    files = [{"id": "f1", "path": src, "zenvi_link": lm.normalize_link(remotion_link())}]
    clips = [{"id": "c1", "file_id": "f1", "reader": {"path": src}}]
    store = pd.ProjectDataStore.__new__(pd.ProjectDataStore)
    store._data = {"files": files, "clips": clips, "version": {}}
    store.current_filepath = None
    monkeypatch.setattr(store, "move_temp_paths_to_project_folder", lambda *a, **k: None)
    monkeypatch.setattr(openshot, "OPENSHOT_VERSION_FULL", "0", raising=False)
    written = {}

    def _boom(path, data, **kwargs):
        written["path"] = data["files"][0]["path"]
        raise OSError("disk full")

    monkeypatch.setattr(store, "write_to_file", _boom)
    with pytest.raises(OSError, match="disk full"):
        store.save(str(tmp_path / "Trip.zvn"))
    assert written["path"] == str(tmp_path / "Trip_assets" / "links" / "remotion" / "Intro-1.mov")
    assert files[0]["path"] == src and clips[0]["reader"]["path"] == src and os.path.isfile(src)


def test_a_second_rerender_of_the_same_clip_is_refused_while_one_runs(linked):
    provider = FakeProvider(probe=linked.probe)
    lm.register_provider(provider)
    out = lm.import_linked(remotion_link(), position=0.0)
    provider.gate = threading.Event()
    first = threading.Thread(target=lambda: lm.rerender_linked(out["file_id"]))
    first.start()
    try:
        for _ in range(500):
            if jobs.job_for(out["file_id"]) is not None:
                break
            threading.Event().wait(0.01)
        with pytest.raises(lm.LinkError, match="already rendering"):
            lm.rerender_linked(out["file_id"])
    finally:
        provider.gate.set()
        first.join(10)
    assert lm.link_state(linked.file(out["file_id"]), compute=False) == "fresh"
    lm.rerender_linked(out["file_id"])  # free again once the first one swapped its media


def test_a_provider_failing_on_its_way_out_of_a_cancel_is_a_cancel(linked):
    from classes.handoff.node_runtime import NodeCancelled
    provider = FakeProvider(probe=linked.probe)
    lm.register_provider(provider)
    out = lm.import_linked(remotion_link(), position=0.0)
    linked.mark()
    provider.fail = NodeCancelled("cancelled", "tail")
    with pytest.raises(jobs.JobCancelled):
        lm.rerender_linked(out["file_id"])
    provider.fail = RuntimeError("Chrome went away")  # what a provider raises after its process was killed
    with pytest.raises(jobs.JobCancelled):
        lm.rerender_linked(out["file_id"], should_cancel=lambda: True)
    assert lm.link_state(linked.file(out["file_id"]), compute=False) == "fresh"  # not "error"
    assert linked.undo_steps_since_mark() == 0


def test_linking_an_existing_plain_file_undoes_cleanly(linked):
    path = linked.media("plain.mov", seconds=4.0)
    plain = linked.add_file("video", path=path, duration=4.0)
    out = lm.add_linked_media(path, remotion_link(), position=0.0)
    assert out["file_id"] == plain and linked.file(plain)["zenvi_link"]["kind"] == "remotion"
    assert linked.undo_steps_since_mark() == 1
    linked.undo()
    f = linked.file(plain)
    assert f is not None and lm.read_link(f) is None and not lm.is_linked(f)  # undo removed the link
    assert linked.clip(out["timeline_clip_id"]) is None
    linked.redo()
    assert lm.read_link(linked.file(plain))["kind"] == "remotion"


def test_import_refuses_a_bad_track_before_rendering(linked):
    provider = FakeProvider(probe=linked.probe)
    lm.register_provider(provider)
    with pytest.raises(lm.LinkError):
        lm.import_linked(remotion_link(), position=0.0, track="99")
    assert provider.renders == [] and not os.path.exists(os.path.join(linked.user_path, "links", "remotion"))
    with pytest.raises(lm.LinkError, match="position"):
        lm.import_linked(remotion_link(), position=-1.0)
    assert linked.undo_steps_since_mark() == 0


def test_a_render_that_cannot_be_added_or_swapped_is_deleted(linked, monkeypatch):
    provider = FakeProvider(probe=linked.probe)
    lm.register_provider(provider)
    folder = os.path.join(linked.user_path, "links", "remotion")
    real_add = lm.add_linked_media
    monkeypatch.setattr(lm, "add_linked_media", lambda *a, **k: (_ for _ in ()).throw(lm.LinkError("track filled")))
    with pytest.raises(lm.LinkError):
        lm.import_linked(remotion_link(), position=0.0)
    assert os.listdir(folder) == []
    monkeypatch.setattr(lm, "add_linked_media", real_add)
    out = lm.import_linked(remotion_link(), position=0.0)
    before = set(os.listdir(folder))
    monkeypatch.setattr(lm, "swap_linked_media", lambda *a, **k: (_ for _ in ()).throw(lm.LinkError("refused")))
    with pytest.raises(lm.LinkError):
        lm.rerender_linked(out["file_id"])
    assert set(os.listdir(folder)) == before  # the new render was removed


def test_bad_provider_results_are_refused_before_install(linked):
    provider = FakeProvider(probe=linked.probe)
    lm.register_provider(provider)
    real_render = provider.render

    def bad_fps(link, out_dir, **kw):
        result = real_render(link, out_dir, **kw)
        result.fps = {"num": 0, "den": 1}
        return result

    provider.render = bad_fps
    with pytest.raises(lm.LinkError, match="fps"):
        lm.render_link(remotion_link())
    assert os.listdir(os.path.join(linked.user_path, "links", "remotion")) == []


def test_swap_never_leaves_a_sub_frame_sliver(linked):
    out = lm.add_linked_media(linked.media("a.mov", seconds=6.0), remotion_link(), position=0.0)
    from classes.query import Clip
    c = Clip.get(id=out["timeline_clip_id"])
    c.data.update(start=3.98, end=5.0)  # 0.6 frame would remain at 4.0 s
    c.save()
    res = lm.swap_linked_media(out["file_id"], linked.media("b.mov", seconds=4.0), {})
    clip = linked.clip(out["timeline_clip_id"])
    assert clip["end"] - clip["start"] >= 1 / 30 - 1e-9 and clip["end"] == pytest.approx(4.0)
    assert clip["start"] == pytest.approx(round(clip["start"] * 30) / 30)  # on the frame grid
    assert "started past the end" in res["warnings"][0]


def test_check_link_without_compute_never_touches_the_disk(linked, monkeypatch):
    provider = FakeProvider(probe=linked.probe)
    lm.register_provider(provider)
    out = lm.import_linked(remotion_link(project_dir=linked.user_path), position=0.0)
    data = linked.file(out["file_id"])

    def no_disk(*a, **k):
        raise AssertionError("compute=False touched the file system")

    for name in ("isdir", "isfile", "exists"):
        monkeypatch.setattr(os.path, name, no_disk)
    monkeypatch.setattr(provider, "fingerprint", no_disk)
    assert lm.link_state(data, compute=False) == "fresh"


def test_save_adopts_renders_only_with_instant_operations(tmp_path, monkeypatch):
    import errno
    from classes import info
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    src = str(tmp_path / "user" / "links" / "remotion" / "Intro-1.mov")
    _write(src)
    link = lm.normalize_link(remotion_link())
    files = [{"id": "F1", "path": src, "zenvi_link": link}]
    clips = [{"id": "C1", "file_id": "F1", "reader": {"path": src}}]
    real_rename = os.rename

    def cross_device(a, b):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", cross_device)
    assert lm.adopt_linked_renders(files, clips, str(tmp_path / "Trip.zvn")) == []
    assert files[0]["path"] == src and os.path.isfile(src)  # another volume: stays, no copy on the GUI thread
    monkeypatch.setattr(os, "rename", real_rename)
    lm.adopt_linked_renders(files, clips, str(tmp_path / "Trip.zvn"))
    first = files[0]["path"]
    lm.adopt_linked_renders(files, clips, str(tmp_path / "Copy.zvn"), previous_path=str(tmp_path / "Trip.zvn"))
    assert os.stat(files[0]["path"]).st_ino == os.stat(first).st_ino  # Save As hard-links, no byte copy
