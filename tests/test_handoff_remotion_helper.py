"""classes.handoff.remotion.helper (+ helper.mjs): argv, events, progress, errors, bundle cache, cancel.

The second half runs helper.mjs under real Node against a fake Remotion in
node_modules (tests/remotion_fakes.py), so the protocol, config loading,
bundle cache, staging and cancellation are exercised without Remotion or
Chrome. Those tests skip when Node 18+ is not installed.
"""

import json
import os
import sys
import threading
import time

import pytest

from classes.handoff import jobs
from classes.handoff.remotion import helper
from remotion_fakes import COMPOSITIONS, make_project, node_runtime_or_none, read_log, write_fake_remotion


# ---------------------------------------------------------------------------
# Pure parts
# ---------------------------------------------------------------------------

def test_build_argv_keeps_values_as_separate_items(tmp_path):
    argv = helper.build_argv("/usr/bin/node", "render", project_dir=str(tmp_path), entry="src/index.ts",
                             composition="Title Card", codec="prores4444", output=str(tmp_path / "o u t.mov"),
                             props_file=str(tmp_path / "p.json"), frames="0-29", concurrency=2,
                             bundle=str(tmp_path / "b"))
    assert argv[:3] == ["/usr/bin/node", helper.HELPER_FILE, "render"]
    pairs = dict(zip(argv[3::2], argv[4::2]))
    assert pairs["--composition"] == "Title Card" and pairs["--output"].endswith("o u t.mov")
    assert pairs["--frames"] == "0-29" and pairs["--concurrency"] == "2" and pairs["--bundle-dir"].endswith("b")
    assert "--out-dir" not in pairs  # empty options are left out
    with pytest.raises(ValueError):
        helper.build_argv("node", "explode", project_dir=str(tmp_path))
    with pytest.raises(ValueError):
        helper.build_argv("node", "still", project_dir=str(tmp_path), composition="--evil")


def test_events_progress_and_error_messages():
    assert helper.parse_event('@@zenvi {"event":"progress","stage":"bundling","progress":0.5}')["stage"] == "bundling"
    assert helper.parse_event("Bundling 50%") is None and helper.parse_event("@@zenvi {broken") is None
    assert helper.overall_progress("render", "rendering", 0.5) == pytest.approx(0.24 + 0.73 * 0.5)
    assert helper.overall_progress("render", "bundling", None) == pytest.approx(0.01)
    assert helper.overall_progress("render", "mystery", 0.5) is None
    msg, code = helper.error_message({"code": "NOT_FOUND", "message": "Could not find X\n  at stack"}, "", "still", 2)
    assert (msg, code) == ("Could not find X at stack", "NOT_FOUND")
    msg, code = helper.error_message(None, "a\nb\nError: boom", "render", 1)
    assert code == "FAILED" and "exit 1" in msg and "boom" in msg
    msg, _ = helper.error_message({"code": "RENDER_FAILED", "message": "bad", "browserLogs": ["TypeError: x"]}, "",
                                  "render", 2)
    assert "browser console: TypeError: x" in msg


def test_bundle_dir_is_keyed_by_sources_and_prune_keeps_recent_bundles(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "cache_root", lambda: str(tmp_path / "cache"))
    a = helper.bundle_dir("/p", "src/index.ts", "sha256:aa", "4.0.532")
    assert a == helper.bundle_dir("/p", "src/index.ts", "sha256:aa", "4.0.532")
    assert a != helper.bundle_dir("/p", "src/index.ts", "sha256:bb", "4.0.532")
    assert a != helper.bundle_dir("/p", "src/index.ts", "sha256:aa", "4.0.533")
    folder = tmp_path / "cache" / "bundles"
    folder.mkdir(parents=True)
    now = time.time()
    for i in range(9):
        d = folder / ("b%d" % i)
        d.mkdir()
        os.utime(d, (now - 7200 - i * 60, now - 7200 - i * 60))
    fresh = folder / "fresh"
    fresh.mkdir()
    partial = folder / "x.partial-1-2"
    partial.mkdir()
    os.utime(partial, (now - 7200, now - 7200))
    removed = helper.prune_bundles(keep=3, now=now, root=str(tmp_path / "cache"))
    left = sorted(p.name for p in folder.iterdir())
    assert "fresh" in left and "x.partial-1-2" not in left and len(left) == 3
    assert len(removed) == 8


def test_orphan_reaper_matches_only_orphans_of_this_project(tmp_path):
    root = str(tmp_path / "proj")
    ps = "\n".join([
        f"  101     1 {root}/node_modules/.remotion/chrome-headless-shell/mac-arm64/chrome-headless-shell --headless",
        f"  102   555 {root}/node_modules/.remotion/chrome-headless-shell/mac-arm64/chrome-headless-shell --type=gpu",
        "  103     1 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        f"  104     1 {root}/node_modules/@remotion/compositor-darwin-arm64/remotion",
        f"  105     1 {tmp_path}/other/node_modules/.remotion/chrome-headless-shell/x",
    ])
    assert helper.orphan_pids(root, ps) == [101, 104]


def test_run_helper_without_node_says_how_to_install(monkeypatch, tmp_path):
    from classes.handoff import node_runtime
    from classes.handoff.linked_media import LinkError

    def missing(*_a, **_k):
        raise node_runtime.NodeNotFound("Node.js 18 or newer was not found. Install Node.js 18 ...")

    monkeypatch.setattr(node_runtime, "find_node", missing)
    with pytest.raises(LinkError, match="Remotion needs Node.js: Node.js 18 or newer was not found"):
        helper.run_helper("probe", project_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# helper.mjs under real Node, against a fake Remotion
# ---------------------------------------------------------------------------

NODE = node_runtime_or_none()
needs_node = pytest.mark.skipif(NODE is None, reason="needs Node.js 18+ on PATH")


@pytest.fixture
def fake_project(tmp_path, monkeypatch):
    root = make_project(str(tmp_path / "proj"), installed=False)
    write_fake_remotion(root)
    with open(os.path.join(root, "remotion.config.js"), "w") as fh:
        fh.write("const {Config} = require('@remotion/cli/config');\n"
                 "Config.overrideWebpackConfig((c) => ({...c, zenviMarker: 'from-config'}));\n")
    with open(os.path.join(root, ".env"), "w") as fh:
        fh.write("# comment\nAPI_KEY=\"abc\"\nexport MODE=test\n")
    log = str(tmp_path / "fake.log")
    monkeypatch.setenv("FAKE_REMOTION_LOG", log)
    monkeypatch.setenv("FAKE_REMOTION_COMPS", json.dumps(COMPOSITIONS))
    return root, log


@needs_node
def test_probe_and_compositions_apply_the_config_and_reuse_the_bundle(fake_project, tmp_path):
    root, log = fake_project
    probe = helper.run_helper("probe", project_dir=root, entry="src/index.ts", runtime=NODE)
    assert probe.result["remotion"] == "4.0.532" and probe.result["entryExists"]
    assert probe.result["configFile"] == "remotion.config.js" and probe.remotion_version == "4.0.532"
    bundle = str(tmp_path / "bundles" / "k1")
    progress = []
    run = helper.run_helper("compositions", project_dir=root, entry="src/index.ts", options={"bundle": bundle},
                            on_progress=lambda f, m: progress.append((f, m)), runtime=NODE)
    assert [c["id"] for c in run.result["compositions"]] == ["TitleCard", "Scene"]
    assert run.result["compositions"][0]["defaultProps"]["title"] == "Hello Zenvi"
    assert os.path.isfile(os.path.join(bundle, "index.html"))
    calls = read_log(log)
    bundled = [c for c in calls if c["call"] == "bundle"]
    assert len(bundled) == 1 and bundled[0]["marker"] == "from-config"  # remotion.config's webpack override
    assert bundled[0]["outDir"].startswith(bundle + ".partial-")        # built aside, then renamed
    listed = [c for c in calls if c["call"] == "getCompositions"][0]
    assert listed["env"] == {"API_KEY": "abc", "MODE": "test"}        # the project's .env
    fractions = [f for f, _m in progress if f is not None]
    assert fractions == sorted(fractions) and fractions[-1] <= 1.0
    assert any("headless browser" in m for _f, m in progress)
    # second listing: the cached bundle, no webpack
    helper.run_helper("compositions", project_dir=root, entry="src/index.ts", options={"bundle": bundle}, runtime=NODE)
    assert len([c for c in read_log(log) if c["call"] == "bundle"]) == 1


@needs_node
def test_render_passes_codec_props_frames_and_renames_the_finished_file(fake_project, tmp_path):
    root, log = fake_project
    out = str(tmp_path / "out" / "TitleCard.mov")
    run = helper.run_helper("render", project_dir=root, entry="src/index.ts", props={"title": "Launch day"},
                            options={"composition": "TitleCard", "codec": "prores4444", "output": out,
                                     "frames": "0-29", "concurrency": 2}, runtime=NODE)
    assert run.result["output"] == out and open(out).read() == "movie"
    assert run.result["durationInFrames"] == 30 and run.result["compositionDurationInFrames"] == 90
    assert run.result["props"]["title"] == "Launch day" and run.result["props"]["accent"] == "#FF5A36"
    call = [c for c in read_log(log) if c["call"] == "renderMedia"][0]
    assert (call["codec"], call["proResProfile"], call["pixelFormat"], call["imageFormat"]) == \
        ("prores", "4444", "yuva444p10le", "png")
    assert call["frameRange"] == [0, 29] and call["concurrency"] == 2
    assert call["outputLocation"] == str(tmp_path / "out" / "TitleCard.partial.mov")
    assert not os.path.exists(call["outputLocation"])
    h264 = str(tmp_path / "out" / "Scene.mp4")
    helper.run_helper("render", project_dir=root, entry="src/index.ts",
                      options={"composition": "Scene", "codec": "h264", "output": h264}, runtime=NODE)
    call = [c for c in read_log(log) if c["call"] == "renderMedia"][-1]
    assert (call["codec"], call["crf"], call["pixelFormat"], call["colorSpace"], call["imageFormat"]) == \
        ("h264", 18, "yuv420p", "bt709", "jpeg")


@needs_node
def test_stills_resolve_first_middle_last(fake_project, tmp_path):
    root, log = fake_project
    run = helper.run_helper("still", project_dir=root, entry="src/index.ts",
                            options={"composition": "TitleCard", "frames": "first,middle,last",
                                     "out_dir": str(tmp_path / "stills")}, runtime=NODE)
    assert [s["frame"] for s in run.result["stills"]] == [0, 44, 89]
    assert all(os.path.isfile(s["output"]) for s in run.result["stills"])
    assert {c["imageFormat"] for c in read_log(log) if c["call"] == "renderStill"} == {"png"}


@needs_node
def test_failures_come_back_as_helper_errors_with_codes(fake_project, tmp_path, monkeypatch):
    root, _log = fake_project
    with pytest.raises(helper.HelperError) as err:
        helper.run_helper("still", project_dir=root, entry="src/index.ts",
                          options={"composition": "Nope", "out_dir": str(tmp_path / "s")}, runtime=NODE)
    assert err.value.code == "NOT_FOUND" and "TitleCard, Scene" in str(err.value)
    with pytest.raises(helper.HelperError, match="--output must end in .mov"):
        helper.run_helper("render", project_dir=root, entry="src/index.ts",
                          options={"composition": "TitleCard", "codec": "prores4444",
                                   "output": str(tmp_path / "x.mp4")}, runtime=NODE)
    monkeypatch.setenv("FAKE_REMOTION_FAIL", "Error: delayRender() timed out")
    out = str(tmp_path / "f.mov")
    with pytest.raises(helper.HelperError, match="delayRender"):
        helper.run_helper("render", project_dir=root, entry="src/index.ts",
                          options={"composition": "TitleCard", "codec": "prores4444", "output": out}, runtime=NODE)
    assert not os.path.exists(out) and not os.path.exists(str(tmp_path / "f.partial.mov"))
    monkeypatch.delenv("FAKE_REMOTION_FAIL")
    monkeypatch.setenv("FAKE_REMOTION_BUNDLE_FAIL", "Module not found: ./Missing")
    with pytest.raises(helper.HelperError) as err:
        helper.run_helper("compositions", project_dir=root, entry="src/index.ts",
                          options={"bundle": str(tmp_path / "b2")}, runtime=NODE)
    assert err.value.code == "BUNDLE_FAILED" and "Module not found" in str(err.value)
    assert not any(n.startswith("b2") for n in os.listdir(str(tmp_path)))


@needs_node
def test_missing_packages_are_reported_with_the_fix(tmp_path):
    root = make_project(str(tmp_path / "bare"), installed=False)
    with pytest.raises(helper.HelperError) as err:
        helper.run_helper("compositions", project_dir=root, entry="src/index.ts", runtime=NODE)
    assert err.value.code == "MISSING_DEPENDENCY" and "npm install" in str(err.value)


@needs_node
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_cancel_kills_the_render_and_leaves_no_partial_file(fake_project, tmp_path, monkeypatch):
    root, _log = fake_project
    monkeypatch.setenv("FAKE_REMOTION_SLOW", "1")
    out = str(tmp_path / "c.mov")
    stop = threading.Event()
    seen = []

    def progress(fraction, message):
        seen.append(fraction)
        if fraction is not None and fraction > 0.3:
            stop.set()

    started = time.monotonic()
    with pytest.raises(jobs.JobCancelled):
        helper.run_helper("render", project_dir=root, entry="src/index.ts",
                          options={"composition": "TitleCard", "codec": "prores4444", "output": out},
                          on_progress=progress, should_cancel=stop.is_set, runtime=NODE)
    assert time.monotonic() - started < 20
    assert not os.path.exists(out)


# ---------------------------------------------------------------------------
# install.py
# ---------------------------------------------------------------------------

def test_install_uses_the_projects_package_manager_when_it_is_there():
    from classes.handoff.node_runtime import NodeRuntime
    from classes.handoff.remotion import install
    rt = NodeRuntime(node="/n/node", npm=("/n/node", "/n/npm-cli.js"), npx=(), version="22.0.0")
    argv, used, warnings = install.install_argv(rt, "npm")
    assert argv == ["/n/node", "/n/npm-cli.js", "install", "--no-audit", "--no-fund", "--prefer-offline"]
    assert used == "npm" and warnings == []
    argv, used, _ = install.install_argv(rt, "pnpm", which=lambda name, path=None: "/opt/bin/pnpm")
    assert argv == ["/opt/bin/pnpm", "install", "--prefer-offline"] and used == "pnpm"
    argv, used, warnings = install.install_argv(rt, "yarn", which=lambda name, path=None: None)
    assert used == "npm" and "yarn is not installed" in warnings[0]


FAKE_NPM = """import json, os, sys
print("npm warn deprecated something", flush=True)
if os.environ.get("FAKE_NPM_FAIL"):
    print("npm error network request to https://registry.npmjs.org/remotion failed", flush=True)
    sys.exit(1)
for name in ("remotion", "@remotion/renderer", "@remotion/bundler", "@remotion/cli"):
    folder = os.path.join("node_modules", *name.split("/"))
    os.makedirs(folder, exist_ok=True)
    json.dump({"name": name, "version": "4.0.532"}, open(os.path.join(folder, "package.json"), "w"))
print("added 261 packages in 3s", flush=True)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX script as npm")
def test_install_dependencies_reports_progress_and_failures(tmp_path, monkeypatch):
    from classes.handoff.linked_media import LinkError
    from classes.handoff.node_runtime import NodeRuntime
    from classes.handoff.remotion import install
    root = make_project(str(tmp_path / "p"), installed=False)
    script = tmp_path / "npm-cli.py"
    script.write_text(FAKE_NPM)
    rt = NodeRuntime(node=sys.executable, npm=(sys.executable, str(script)), npx=(), version="22.0.0")
    lines = []
    result = install.install_dependencies(root, on_progress=lambda f, m: lines.append(m), runtime=rt)
    assert result == {"manager": "npm", "warnings": [], "remotion_version": "4.0.532"}
    assert any("added 261 packages" in m for m in lines)
    monkeypatch.setenv("FAKE_NPM_FAIL", "1")
    with pytest.raises(LinkError, match="npm install` failed .*registry.npmjs.org"):
        install.install_dependencies(root, runtime=rt)
    with pytest.raises(LinkError, match="no package.json"):
        install.install_dependencies(str(tmp_path), runtime=rt)
