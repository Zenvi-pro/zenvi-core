"""classes.handoff.remotion.provider / studio: fingerprints, the auto codec, render results, open code / studio."""

import os
import sys
import time

import pytest

from classes.handoff import alpha, jobs
from classes.handoff import linked_media as lm
from classes.handoff.remotion import detect, helper, provider, studio
from handoff_fakes import linked, tt  # noqa: F401  (fixtures)
from remotion_fakes import FakeHelper, make_project


@pytest.fixture
def project(tmp_path):
    return detect.inspect_project(make_project(str(tmp_path / "promo")))


@pytest.fixture
def fake_helper(monkeypatch):
    fake = FakeHelper()
    monkeypatch.setattr(helper, "run_helper", fake)
    monkeypatch.setattr(helper, "prune_bundles", lambda *a, **k: [])
    return fake


def _link(project, comp="TitleCard", **kw):
    defaults = {"title": "Hello Zenvi", "subtitle": "From Remotion", "accent": "#FF5A36"} if comp == "TitleCard" else {}
    return lm.read_link({"zenvi_link": lm.normalize_link(provider.make_link(project, comp, defaults=defaults, **kw))})


# ---------------------------------------------------------------------------
# Links and fingerprints
# ---------------------------------------------------------------------------

def test_make_link_stores_full_props_and_the_requested_codec(project):
    link = _link(project, props={"title": "Launch day"}, codec="h264")
    assert link["props"] == {"title": "Launch day", "subtitle": "From Remotion", "accent": "#FF5A36"}
    assert provider.requested_codec(link) == "h264" and provider.settings(link)["frames"] is None
    assert link["source"]["composition"] == "TitleCard" and link["source"]["entry"] == "src/index.ts"
    assert provider.default_props(link)["title"] == "Hello Zenvi"
    with pytest.raises(lm.LinkError, match="never WebM"):
        provider.make_link(project, "TitleCard", codec="vp9")
    # props whose keys the project file would rewrite as paths survive (encoded)
    link = _link(project, props={"image": "logo.png"})
    assert link["props"]["image"] == "logo.png"


def test_editable_props_include_defaults_added_in_code_later(project):
    link = _link(project, props={"title": "Launch day"})
    link["source"]["default_props"] = lm.encode_props({"title": "x", "subtitle": "y", "accent": "z", "logo": True})
    assert provider.RemotionProvider().editable_props(link) == {
        "title": "Launch day", "subtitle": "From Remotion", "accent": "#FF5A36", "logo": True}


def test_fingerprint_follows_code_assets_props_and_settings_but_not_installs_or_outputs(project):
    p = provider.RemotionProvider()
    link = _link(project)
    first = p.fingerprint(link)
    root = project.root
    os.utime(os.path.join(root, "src", "TitleCard.tsx"), (1, 1))          # touched, same content
    for junk in ("node_modules/remotion/new.js", "out/render.mp4", "README.md", "public/.DS_Store"):
        path = os.path.join(root, *junk.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("x")
    assert p.fingerprint(link) == first
    with open(os.path.join(root, "src", "TitleCard.tsx"), "a") as fh:
        fh.write("// edited\n")
    code_changed = p.fingerprint(link)
    assert code_changed != first
    with open(os.path.join(root, "public", "logo.png"), "wb") as fh:
        fh.write(b"png")
    asset_added = p.fingerprint(link)
    assert asset_added != code_changed
    assert p.fingerprint(_link(project, props={"title": "Other"})) != asset_added
    assert p.fingerprint(_link(project, codec="prores4444")) != asset_added
    with open(os.path.join(root, "package-lock.json"), "w") as fh:
        fh.write("{}")
    assert p.fingerprint(link) != asset_added
    import shutil
    shutil.rmtree(root)
    with pytest.raises(lm.SourceMissing):
        p.fingerprint(link)


def test_fingerprint_ignores_zenvi_assets_reads_public_by_stat_and_never_starves_the_code(project, monkeypatch):
    p = provider.RemotionProvider()
    link = _link(project)
    root = project.root
    first = p.fingerprint(link)
    # a Zenvi project saved inside the Remotion project keeps its renders next to it: not a source
    render = os.path.join(root, "promo_assets", "links", "remotion", "TitleCard-1a2b.mov")
    os.makedirs(os.path.dirname(render))
    with open(render, "wb") as fh:
        fh.write(b"movie")
    os.makedirs(os.path.join(root, "public", "trip_assets"))
    with open(os.path.join(root, "public", "trip_assets", "x.mp4"), "wb") as fh:
        fh.write(b"movie")
    assert p.fingerprint(link) == first
    # public/ is read by size and mtime only (no rehashing of every image each sweep)
    frames = os.path.join(root, "public", "frames")
    os.makedirs(frames)
    for i in range(12):
        with open(os.path.join(frames, "f%03d.png" % i), "wb") as fh:
            fh.write(b"png")
    with_frames = p.fingerprint(link)
    assert with_frames != first
    reads = []
    real_open = open

    def spy(path, *a, **k):
        reads.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    assert p.fingerprint(link) == with_frames
    monkeypatch.setattr("builtins.open", real_open)
    assert not [r for r in reads if os.sep + "public" + os.sep in r]
    # a huge public/ cannot hide a code edit (each part has its own file limit)
    monkeypatch.setattr(provider, "PUBLIC_MAX_FILES", 3)
    capped = p.fingerprint(link)
    with open(os.path.join(root, "src", "Scene.tsx"), "a") as fh:
        fh.write("// edited\n")
    assert p.fingerprint(link) != capped


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_auto_renders_prores_when_a_still_is_transparent(project, fake_helper, monkeypatch, tmp_path):
    checked = []
    monkeypatch.setattr(alpha, "has_transparency", lambda p: checked.append(p) or True)
    out = tmp_path / "staging"
    out.mkdir()
    result = provider.RemotionProvider().render(_link(project, props={"title": "Launch day"}), str(out),
                                                on_progress=lambda f, m: None, should_cancel=lambda: False)
    assert fake_helper.commands() == ["still", "render"]
    still, render = fake_helper.calls
    assert still["options"]["frames"] == "first,middle,last" and len(checked) == 1  # stops at the first alpha
    assert render["options"]["codec"] == "prores4444" and render["options"]["output"].endswith("TitleCard.mov")
    assert render["props"]["title"] == "Launch day" and render["options"]["bundle"] == still["options"]["bundle"]
    assert result.codec == "prores4444" and result.path.endswith(".mov") and os.path.isfile(result.path)
    assert (result.width, result.height, result.duration_frames) == (1920, 1080, 90)
    assert result.source_updates["file"] == "src/TitleCard.tsx" and result.source_updates["line"] == 3
    assert result.source_updates["folder"] == "Graphics"
    assert lm.decode_props(result.source_updates["default_props"])["title"] == "Hello Zenvi"
    assert not os.path.exists(str(out / "stills"))


def test_auto_renders_h264_when_every_still_is_opaque_and_explicit_codecs_skip_the_check(project, fake_helper,
                                                                                       monkeypatch, tmp_path):
    monkeypatch.setattr(alpha, "has_transparency", lambda p: False)
    result = provider.RemotionProvider().render(_link(project, "Scene"), str(tmp_path), on_progress=lambda f, m: None,
                                                should_cancel=lambda: False)
    assert result.codec == "h264" and result.path.endswith("Scene.mp4")
    monkeypatch.setattr(alpha, "has_transparency", lambda p: pytest.fail("explicit codec must not probe alpha"))
    result = provider.RemotionProvider().render(_link(project, "Scene", codec="prores4444"), str(tmp_path),
                                                on_progress=lambda f, m: None, should_cancel=lambda: False)
    assert result.codec == "prores4444" and fake_helper.calls[-1]["options"]["codec"] == "prores4444"


def test_qtrle_is_prores_reencoded_and_stills_become_five_second_clips(project, fake_helper, monkeypatch, tmp_path):
    made = []

    def fake_ffmpeg(tail, output, on_progress, should_cancel):
        made.append(tail)
        with open(output, "wb") as fh:
            fh.write(b"mov")
        return output

    monkeypatch.setattr(provider, "_ffmpeg_movie", fake_ffmpeg)
    result = provider.RemotionProvider().render(_link(project, "TitleCard", codec="qtrle"), str(tmp_path),
                                                on_progress=lambda f, m: None, should_cancel=lambda: False)
    assert result.codec == "qtrle" and result.path.endswith("-qtrle.mov")
    assert fake_helper.calls[-1]["options"]["codec"] == "prores4444" and "qtrle" in made[-1]
    fake_helper.compositions.append({"id": "Thumb", "width": 640, "height": 360, "fps": 30, "durationInFrames": 1,
                                     "defaultProps": {}})
    monkeypatch.setattr(alpha, "has_transparency", lambda p: False)
    result = provider.RemotionProvider().render(_link(project, "Thumb"), str(tmp_path), on_progress=lambda f, m: None,
                                                should_cancel=lambda: False)
    assert fake_helper.commands()[-1] == "still" and result.duration_frames == 150 and result.codec == "h264"
    assert "-loop" in made[-1] and any("still" in w for w in result.warnings)


def test_cancel_propagates_as_job_cancelled(project, fake_helper, monkeypatch, tmp_path):
    fake_helper.fail, fake_helper.fail_on = jobs.JobCancelled("cancelled"), "still"
    with pytest.raises(jobs.JobCancelled):
        provider.RemotionProvider().render(_link(project), str(tmp_path), on_progress=lambda f, m: None,
                                           should_cancel=lambda: False)
    monkeypatch.setattr(alpha, "has_transparency", lambda p: True)
    fake_helper.fail_on = "render"
    with pytest.raises(jobs.JobCancelled):
        provider.RemotionProvider().render(_link(project), str(tmp_path), on_progress=lambda f, m: None,
                                           should_cancel=lambda: False)


def test_render_link_installs_the_render_and_the_clip_reads_fresh(linked, project, fake_helper, monkeypatch):  # noqa: F811
    monkeypatch.setattr(alpha, "has_transparency", lambda p: True)
    lm.register_provider(provider.RemotionProvider())
    path, completed = lm.render_link(_link(project, props={"title": "Launch day"}))
    assert os.path.dirname(path) == os.path.join(linked.user_path, "links", "remotion")
    assert os.path.basename(path).startswith("TitleCard-") and path.endswith(".mov")
    assert completed["render"]["codec"] == "prores4444" and completed["render"]["duration_frames"] == 90
    assert completed["render"]["fps"] == {"num": 30, "den": 1} and completed["props"]["title"] == "Launch day"
    assert not [n for n in os.listdir(os.path.dirname(path)) if n.startswith(lm.STAGING_PREFIX)]
    file_data = {"id": "F1", "path": path, "zenvi_link": {k: v for k, v in completed.items() if k != "warnings"}}
    check = lm.check_link(file_data)
    assert check.state == "fresh", check.detail
    with open(os.path.join(project.root, "src", "TitleCard.tsx"), "a") as fh:
        fh.write("// code changed\n")
    assert lm.check_link(file_data).state == "stale"


# ---------------------------------------------------------------------------
# Open code / studio
# ---------------------------------------------------------------------------

def test_open_source_opens_the_component_at_its_line(project, monkeypatch):
    from classes.handoff import open_source
    opened = []
    monkeypatch.setattr(open_source, "open_in_editor", lambda f, line=None, **k: opened.append((f, line, k)))
    link = _link(project)
    link["source"].update(file="src/TitleCard.tsx", line=3)
    provider.RemotionProvider().open_source(link)
    assert opened[-1][:2] == (os.path.join(project.root, "src", "TitleCard.tsx"), 3)
    assert opened[-1][2]["folder"] == project.root
    link["source"].update(file="src/Moved.tsx", line=9)  # gone: the scan finds the component again
    provider.RemotionProvider().open_source(link)
    assert opened[-1][:2] == (os.path.join(project.root, "src", "TitleCard.tsx"), 3)
    from classes.handoff.open_source import EditorError

    def boom(*a, **k):
        raise EditorError("Cursor is not installed")

    monkeypatch.setattr(open_source, "open_in_editor", boom)
    with pytest.raises(lm.LinkError, match="Cursor is not installed"):
        provider.RemotionProvider().open_source(link)


def test_open_studio_uses_the_project_studio(project, monkeypatch):
    seen = []
    monkeypatch.setattr(studio, "open_studio", lambda proj, comp=None, **k: seen.append((proj.root, comp)))
    provider.RemotionProvider().open_studio(_link(project, "Scene"))
    assert seen == [(project.root, "Scene")]


def test_studio_argv_runs_the_projects_own_cli_with_the_watchdog(project):
    from classes.handoff.node_runtime import NodeRuntime
    runtime = NodeRuntime(node="/usr/bin/node", npm=("npm",), npx=("/usr/bin/npx",), version="22.0.0")
    cli = os.path.join(project.root, "node_modules", "@remotion", "cli")
    npx = studio.studio_argv(runtime, project, 3001)
    assert npx[:2] == ["/usr/bin/npx", "remotion"]  # no remotion-cli.js
    env = studio.studio_env(runtime, npx, parent_pid=4242)
    assert env["ZENVI_PARENT_PID"] == "4242" and env["BROWSER"] == "none"
    assert env["NODE_OPTIONS"].endswith('--require "%s"' % studio.WATCHDOG_FILE)  # npx: preloaded via NODE_OPTIONS
    with open(os.path.join(cli, "remotion-cli.js"), "w") as fh:
        fh.write("")
    argv = studio.studio_argv(runtime, project, 3001)
    assert argv == ["/usr/bin/node", "--require", studio.WATCHDOG_FILE, os.path.join(cli, "remotion-cli.js"),
                    "studio", "src/index.ts", "--port", "3001", "--no-open"]
    assert "NODE_OPTIONS" not in studio.studio_env(runtime, argv) or \
        studio.WATCHDOG_FILE not in studio.studio_env(runtime, argv)["NODE_OPTIONS"]


# A fake Remotion Studio: answers /__remotion_config like the real one ({"isRemotion": true, "cwd": ...})
FAKE_STUDIO = """import http.server, json, os, sys
port = int(sys.argv[sys.argv.index('--port') + 1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'isRemotion': True, 'cwd': os.getcwd()}).encode() if self.path == '/__remotion_config' \\
            else b'<title>Remotion Studio</title>'
        self.send_response(200); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a):
        pass
print('Server ready - Local: http://localhost:%d' % port, flush=True)
http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()
"""


def _python_as_node(tmp_path):
    """A "node" that drops ``--require <watchdog>`` and runs the (Python) fake CLI."""
    from classes.handoff.node_runtime import NodeRuntime
    wrapper = tmp_path / "fake-node"
    wrapper.write_text('#!/bin/sh\nif [ "$1" = "--require" ]; then shift 2; fi\nexec "%s" "$@"\n' % sys.executable)
    wrapper.chmod(0o755)
    return NodeRuntime(node=str(wrapper), npm=(), npx=(), version="22.0.0")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_open_studio_starts_once_waits_for_the_port_and_stops_on_exit(project, monkeypatch, tmp_path):
    from classes import info
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    cli = os.path.join(project.root, "node_modules", "@remotion", "cli", "remotion-cli.js")
    with open(cli, "w") as fh:
        fh.write(FAKE_STUDIO)  # "node" below runs Python, so the fake CLI is a Python HTTP server
    runtime = _python_as_node(tmp_path)
    monkeypatch.setattr(studio, "_install_exit_hooks", lambda: None)
    import threading
    results = []
    try:
        # two requests at once (double-click, or the menu and an agent): one Studio, the second reuses it
        threads = [threading.Thread(target=lambda c=c: results.append(
            studio.open_studio(project, c, open_browser=False, runtime=runtime, timeout=30))) for c in ("TitleCard", "Scene")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert len(results) == 2 and len({r["pid"] for r in results}) == 1
        assert sorted(r["reused"] for r in results) == [False, True]
        first = [r for r in results if not r["reused"]][0]
        assert first["url"] == "http://localhost:%d/%s" % (first["port"], first["url"].rsplit("/", 1)[-1])
        again = studio.open_studio(project, "Scene", open_browser=False, runtime=runtime)
        assert again["reused"] and again["pid"] == first["pid"] and again["url"].endswith("/Scene")
        assert len(studio.running_studios()) == 1 and studio.running_studios()[0]["alive"]
    finally:
        studio.stop_all()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(first["pid"], 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the studio process survived stop_all")
    assert studio.running_studios() == []


def _serve(handler_body: bytes, config_status: int = 404, config_body: bytes = b""):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = (config_status, config_body) if self.path == "/__remotion_config" else (200, handler_body)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_answers_only_for_this_projects_remotion_studio(tmp_path):
    import json
    other = _serve(b"<h1>my dev server</h1>")  # some other app on the port
    try:
        assert not studio._answers(other.server_address[1], str(tmp_path))
    finally:
        other.shutdown()
    elsewhere = _serve(b"", 200, json.dumps({"isRemotion": True, "cwd": str(tmp_path / "another")}).encode())
    try:
        assert not studio._answers(elsewhere.server_address[1], str(tmp_path))  # a Studio of another project
    finally:
        elsewhere.shutdown()
    ours = _serve(b"", 200, json.dumps({"isRemotion": True, "cwd": str(tmp_path)}).encode())
    try:
        assert studio._answers(ours.server_address[1], str(tmp_path))
    finally:
        ours.shutdown()
    old = _serve(b"<title>Remotion Studio</title>")  # an older Studio without /__remotion_config
    try:
        assert studio._answers(old.server_address[1], str(tmp_path))
    finally:
        old.shutdown()


def test_free_port_skips_ports_taken_on_any_interface():
    import socket
    taken = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    taken.bind(("0.0.0.0", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    try:
        assert not studio.port_is_free(port)
        assert studio.free_port(port, 5) != port
    finally:
        taken.close()
    if socket.has_ipv6:
        six = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            six.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            six.bind(("::", 0))
        except OSError:
            six.close()
            return
        six.listen(1)
        try:
            assert not studio.port_is_free(six.getsockname()[1])  # Studio binds :: too
        finally:
            six.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX processes")
def test_studio_watchdog_exits_when_zenvi_is_gone(tmp_path):
    import subprocess
    from remotion_fakes import node_runtime_or_none
    runtime = node_runtime_or_none()
    if runtime is None:
        pytest.skip("needs Node.js 18+ on PATH")
    zenvi = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    env = dict(runtime.env(), ZENVI_PARENT_PID=str(zenvi.pid), ZENVI_WATCHDOG_MS="100")
    started = time.monotonic()
    node = subprocess.Popen([runtime.node, "--require", studio.WATCHDOG_FILE, "-e", "setInterval(() => {}, 1000)"],
                            env=env)
    try:
        zenvi.wait(10)
        assert node.wait(15) == 0
    finally:
        if node.poll() is None:
            node.kill()
    assert time.monotonic() - started < 15


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell wrapper as node")
def test_studio_that_dies_reports_its_output(project, monkeypatch, tmp_path):
    from classes import info
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    cli = os.path.join(project.root, "node_modules", "@remotion", "cli", "remotion-cli.js")
    with open(cli, "w") as fh:
        fh.write("import sys\nprint('Error: Port 3000 is in use and --port was given')\nsys.exit(1)\n")
    runtime = _python_as_node(tmp_path)
    with pytest.raises(lm.LinkError, match="stopped before it was ready.*in use"):
        studio.open_studio(project, "TitleCard", open_browser=False, runtime=runtime, timeout=20)


def test_the_package_registers_its_provider_and_menu_entries():
    import importlib
    from classes.handoff import ui_registry
    import classes.handoff.remotion as package
    importlib.reload(package)
    assert isinstance(lm.provider_for("remotion"), provider.RemotionProvider) and lm.supports_studio("remotion")
    imports = {a.id: a for a in ui_registry.import_actions()}
    exports = {a.id: a for a in ui_registry.export_actions()}
    assert imports["remotion"].label == exports["remotion"].label == "Remotion Project..."
    assert imports["remotion"].order == exports["remotion"].order == 30
