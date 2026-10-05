"""Running the HyperFrames CLI: which CLI, the quiet environment, JSON output, renders, the Studio."""

import json
import os
import sys
import textwrap

import pytest

from classes.handoff import node_runtime
from classes.handoff.hyperframes import cli as hf_cli


@pytest.fixture
def node(monkeypatch, tmp_path):
    runtime = node_runtime.NodeRuntime(node=sys.executable, npm=(sys.executable, "npm-cli.js"),
                                       npx=(sys.executable, "npx-cli.js"), version="22.11.0")
    monkeypatch.setattr(node_runtime, "find_node", lambda min_major=18, env=None, **kw: runtime)
    from classes import info
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    monkeypatch.delenv(hf_cli.CLI_ENV_OVERRIDE, raising=False)
    monkeypatch.delenv(hf_cli.WORKERS_ENV, raising=False)
    return runtime


def _package(root, version):
    os.makedirs(os.path.join(root, "bin"), exist_ok=True)
    open(os.path.join(root, "bin", "hyperframes.mjs"), "w").write("// cli")
    json.dump({"name": "hyperframes", "version": version}, open(os.path.join(root, "package.json"), "w"))


def _project(tmp_path, pin=None):
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    scripts = {"render": "npx --yes hyperframes@%s render" % pin} if pin else {"build": "vite"}
    json.dump({"name": "p", "scripts": scripts}, open(root / "package.json", "w"))
    return str(root)


def test_project_pin(tmp_path):
    assert hf_cli.project_pin(_project(tmp_path, "0.8.120")) == "0.8.120"
    assert hf_cli.project_pin(_project(tmp_path)) is None
    assert hf_cli.project_pin(str(tmp_path / "missing")) is None and hf_cli.project_pin(None) is None


def test_resolution_order(tmp_path, node, monkeypatch):
    proj = _project(tmp_path, "0.8.120")
    cli = hf_cli.resolve_cli(proj)
    assert (cli.source, cli.version) == ("npx", "0.8.120")
    assert cli.argv == (sys.executable, "npx-cli.js", "--yes", "hyperframes@0.8.120")
    assert hf_cli.resolve_cli(_project(tmp_path)).version == hf_cli.PINNED_VERSION
    # Zenvi's private install (motion graphics) counts when the project pins its version, or pins
    # nothing and it is at least the version Zenvi verified (older CLIs may lack the flags used)
    private = hf_cli.private_install()
    _package(private, "0.8.115")
    assert hf_cli.resolve_cli(_project(tmp_path)).source == "npx"
    assert hf_cli.resolve_cli(_project(tmp_path, "0.8.115")).source == "zenvi"
    for version, source in ((hf_cli.PINNED_VERSION, "zenvi"), ("0.9.0", "zenvi"), ("1.0.0-beta.1", "npx")):
        _package(private, version)
        assert hf_cli.resolve_cli(_project(tmp_path)).source == source, version
    assert hf_cli.resolve_cli(_project(tmp_path, "0.8.120")).source == "npx"
    # the project's own install is the project's code: never run on its own (review C5-1 #8)
    _package(os.path.join(proj, "node_modules", "hyperframes"), "0.8.121")
    cli = hf_cli.resolve_cli(proj)
    assert (cli.source, cli.version) == ("npx", "0.8.120") and "node_modules" not in " ".join(cli.argv)
    # ZENVI_HYPERFRAMES_CLI (the user's choice) over everything
    other = str(tmp_path / "other")
    _package(other, "9.9.9")
    monkeypatch.setenv(hf_cli.CLI_ENV_OVERRIDE, other)
    assert (hf_cli.resolve_cli(proj).source, hf_cli.resolve_cli(proj).version) == ("env", "9.9.9")
    monkeypatch.setenv(hf_cli.CLI_ENV_OVERRIDE, str(tmp_path / "nope"))
    with pytest.raises(hf_cli.CliError, match="does not exist"):
        hf_cli.resolve_cli(proj)


def test_no_node_is_a_clear_error(monkeypatch):
    def missing(*a, **kw):
        raise node_runtime.NodeNotFound("Node.js 22 or newer was not found. Install Node.js")
    monkeypatch.setattr(node_runtime, "find_node", missing)
    with pytest.raises(hf_cli.CliError, match="HyperFrames needs Node.js 22"):
        hf_cli.resolve_cli(None)


def test_every_command_runs_quiet(tmp_path, node, monkeypatch):
    seen = {}

    def fake_run(argv, cwd, env=None, timeout=None, on_line=None, should_cancel=None, runtime=None):
        seen.update(argv=list(argv), env=dict(env), cwd=cwd)
        for line in ("\x1b[1G\x1b[J◒  Checking browser", '{"ok": true, "errorCount": 0, "findings": []}'):
            on_line(line)
        return 0, ""
    monkeypatch.setattr(node_runtime, "run_node", fake_run)
    proj = _project(tmp_path)
    open(os.path.join(proj, ".npmrc"), "w").write("registry=https://evil.example/\n")
    report = hf_cli.lint(proj)
    assert report["ok"] is True and seen["argv"][-3:] == ["lint", proj, "--json"]
    # run from Zenvi's own folder with the project as an argument: the project's .npmrc never applies
    assert seen["cwd"] == hf_cli.work_dir() and not seen["cwd"].startswith(proj)
    for key, value in hf_cli.QUIET_ENV.items():
        assert seen["env"][key] == value
    assert seen["env"]["HYPERFRAMES_NO_TELEMETRY"] == "1" and seen["env"]["DO_NOT_TRACK"] == "1"


def test_json_is_found_in_noisy_output_and_missing_json_is_an_error(tmp_path, node, monkeypatch):
    cli = hf_cli.Cli((sys.executable, "hf.mjs"), "0.8.126", "env", node)
    out = ["[INFO] something {not json}", json.dumps({"timeline": {"duration": 3, "tracks": []}}), "done"]
    monkeypatch.setattr(node_runtime, "run_node",
                        lambda argv, cwd, **kw: ([kw["on_line"](x) for x in out], (0, ""))[1])
    assert hf_cli.timeline(_project(tmp_path), cli=cli)["timeline"]["duration"] == 3
    monkeypatch.setattr(node_runtime, "run_node",
                        lambda argv, cwd, **kw: ([kw["on_line"](x) for x in ["Unknown command"]], (1, ""))[1])
    with pytest.raises(hf_cli.CliError, match="printed no JSON"):
        hf_cli.timeline(_project(tmp_path), cli=cli)


def test_timeline_runs_plain_node_in_the_project_never_npm(tmp_path, node, monkeypatch):
    """0.8.126's `timeline` reads the project from its working folder only (a folder argument is taken for a
    sub-command): it runs there, but as `node <entry>` -- an npx CLI is found in npm's cache first."""
    proj = _project(tmp_path, "0.8.126")
    cache = tmp_path / "npm-cache"
    monkeypatch.setenv("npm_config_cache", str(cache))
    calls = []

    def fake_run(argv, cwd, env=None, timeout=None, on_line=None, should_cancel=None, runtime=None):
        calls.append((list(argv), cwd))
        if "--version" in argv:  # what npx --yes does on first use: download into the cache
            _package(str(cache / "_npx" / "abc123" / "node_modules" / "hyperframes"), "0.8.126")
            return 0, "0.8.126"
        on_line(json.dumps({"timeline": {"duration": 2, "tracks": []}}))
        return 0, ""
    monkeypatch.setattr(node_runtime, "run_node", fake_run)
    cli = hf_cli.resolve_cli(proj)
    assert cli.source == "npx"
    assert hf_cli.timeline(proj, cli=cli)["timeline"]["duration"] == 2
    (download, download_cwd), (listing, listing_cwd) = calls
    assert download[-1] == "--version" and download_cwd == hf_cli.work_dir()          # npx: Zenvi's folder
    assert listing[0] == sys.executable and "npx-cli.js" not in listing                 # then plain node...
    assert listing[1].endswith(os.path.join("_npx", "abc123", "node_modules", "hyperframes", "bin",
                                            "hyperframes.mjs")) and listing_cwd == proj  # ...in the project
    with pytest.raises(hf_cli.CliError, match="never runs inside a project folder"):
        hf_cli.run(cli, ["timeline"], cwd=proj)


def test_clean_line():
    assert hf_cli.clean_line("\x1b[1G\x1b[J◐  Checking\x1b[1G\x1b[J◓  Checking browser") == "◓  Checking browser"
    assert hf_cli.clean_line("\x1b[90m  26%  Streaming frame 1/78\x1b[39m") == "26%  Streaming frame 1/78"


def test_render_arguments_progress_and_failure(tmp_path, node, monkeypatch):
    proj = _project(tmp_path)
    calls = []

    def fake_run(argv, cwd, env=None, timeout=None, on_line=None, should_cancel=None, runtime=None):
        calls.append(list(argv))
        out = argv[argv.index("--output") + 1]
        on_line("  ██░░  26%  Streaming frame 1/78")
        on_line("  ██░░  80%  Streaming frame 78/78")
        if "fail" in out:
            return 1, "[INFO] noise\nError: Chrome cannot start"
        open(out, "wb").write(b"mov")
        return 0, ""
    monkeypatch.setattr(node_runtime, "run_node", fake_run)
    monkeypatch.setenv(hf_cli.WORKERS_ENV, "1")
    progress = []
    out = str(tmp_path / "r.mov")
    hf_cli.render(proj, ".render-zenvi-1/index.html", out, fmt="mov", variables={"t": "A"}, fps="25",
                  on_progress=lambda f, m: progress.append((f, m)))
    argv = calls[0]
    assert argv[argv.index("--composition") + 1] == ".render-zenvi-1/index.html"
    assert argv[argv.index("--format") + 1] == "mov" and argv[argv.index("--fps") + 1] == "25"
    assert argv[argv.index("--workers") + 1] == "1"
    assert json.load(open(argv[argv.index("--variables-file") + 1])) == {"t": "A"}
    assert progress == [(0.26, "Streaming frame 1/78"), (0.8, "Streaming frame 78/78")]
    with pytest.raises(hf_cli.CliError, match="Chrome cannot start"):
        hf_cli.render(proj, "index.html", str(tmp_path / "fail.mp4"), fmt="mp4")


def test_studio_starts_once_reuses_and_stops(tmp_path, node, monkeypatch):
    script = tmp_path / "fake_preview.py"
    script.write_text(textwrap.dedent("""
        import json, sys, time
        port = sys.argv[sys.argv.index("--port") + 1]
        print("[INFO] warming up", flush=True)
        print(json.dumps({"schemaVersion": 1, "operation": "start", "ok": True, "result": {
            "state": "started", "ready": True, "studioUrl": "http://127.0.0.1:%s/#project/p" % port}}), flush=True)
        time.sleep(60)
    """))
    cli = hf_cli.Cli((sys.executable, str(script)), "0.8.126", "env", node)
    monkeypatch.setattr(hf_cli, "resolve_cli", lambda project_dir=None, **kw: cli)
    proj = _project(tmp_path)
    studio = hf_cli.start_studio(proj, timeout=30)
    try:
        assert studio.url.startswith("http://127.0.0.1:%d/#project/" % studio.port)
        assert hf_cli.start_studio(proj) is studio and hf_cli.running_studio(proj) is studio
    finally:
        assert hf_cli.stop_studios() == 1
    studio.proc.wait(timeout=10)
    assert studio.proc.poll() is not None and hf_cli.running_studio(proj) is None


def test_two_quick_opens_start_one_studio(tmp_path, node, monkeypatch):
    import threading
    script = tmp_path / "slow_preview.py"
    script.write_text(textwrap.dedent("""
        import json, sys, time
        port = sys.argv[sys.argv.index("--port") + 1]
        time.sleep(1.0)
        print(json.dumps({"ok": True, "result": {"ready": True,
                          "studioUrl": "http://127.0.0.1:%s/#project/p" % port}}), flush=True)
        time.sleep(60)
    """))
    cli = hf_cli.Cli((sys.executable, str(script)), "0.8.126", "env", node)
    monkeypatch.setattr(hf_cli, "resolve_cli", lambda project_dir=None, **kw: cli)
    proj = _project(tmp_path)
    got = []
    threads = [threading.Thread(target=lambda: got.append(hf_cli.start_studio(proj, timeout=30))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(40)
    try:
        assert len(got) == 2 and got[0] is got[1]
    finally:
        assert hf_cli.stop_studios() == 1
    got[0].proc.wait(timeout=10)


def test_studio_that_never_gets_ready(tmp_path, node, monkeypatch):
    script = tmp_path / "broken.py"
    script.write_text("print('Error: port in use', flush=True)\n")
    cli = hf_cli.Cli((sys.executable, str(script)), "0.8.126", "env", node)
    monkeypatch.setattr(hf_cli, "resolve_cli", lambda project_dir=None, **kw: cli)
    with pytest.raises(hf_cli.CliError, match="port in use"):
        hf_cli.start_studio(_project(tmp_path), timeout=10)


def test_a_project_cannot_pick_the_programs_hyperframes_runs(tmp_path, node, monkeypatch):
    """verify-C5-1 #1: HyperFrames applies <cwd>/.env to every key the environment lacks (and looks for
    <cwd>/.hyperframes/bin/ffprobe), and `timeline` runs in the project folder and probes media: the project
    could make Import run a program of its own. Zenvi's ffmpeg / ffprobe are pinned for every command and
    every key of the folder's .env is already present, so HyperFrames' loader applies none of them."""
    proj = _project(tmp_path, "0.8.126")
    open(os.path.join(proj, ".env"), "w").write(
        "# set by the project\nHYPERFRAMES_FFPROBE_PATH=./evil-ffprobe\nexport NODE_OPTIONS=--require ./x.js\n"
        "HYPERFRAMES_BROWSER_PATH = './chrome'\nPRODUCER_HEADLESS_SHELL_PATH=./shell # comment\n"
        "HYPERFRAMES_NO_TELEMETRY=0\n=nokey\nnot a line\n")
    os.makedirs(os.path.join(proj, ".hyperframes", "bin"))
    open(os.path.join(proj, ".hyperframes", "bin", "ffprobe"), "w").write("#!/bin/sh\ntouch pwned\n")
    zenvi = {"ffmpeg": str(tmp_path / "zbin" / "ffmpeg"), "ffprobe": str(tmp_path / "zbin" / "ffprobe")}
    monkeypatch.setattr(hf_cli, "zenvi_ffmpeg", lambda name: zenvi.get(name))
    monkeypatch.delenv("NODE_OPTIONS", raising=False)
    monkeypatch.delenv("HYPERFRAMES_BROWSER_PATH", raising=False)
    monkeypatch.delenv("PRODUCER_HEADLESS_SHELL_PATH", raising=False)
    assert hf_cli.dotenv_keys(proj) == ["HYPERFRAMES_FFPROBE_PATH", "NODE_OPTIONS", "HYPERFRAMES_BROWSER_PATH",
                                        "PRODUCER_HEADLESS_SHELL_PATH", "HYPERFRAMES_NO_TELEMETRY"]
    seen = []

    def fake_run(argv, cwd, env=None, timeout=None, on_line=None, should_cancel=None, runtime=None):
        seen.append((list(argv), cwd, dict(env)))
        out = argv[argv.index("--output") + 1] if "--output" in argv else None
        if out:
            open(out, "wb").write(b"x")
        on_line(json.dumps({"ok": True, "timeline": {"tracks": []}}))
        return 0, ""
    monkeypatch.setattr(node_runtime, "run_node", fake_run)
    cli = hf_cli.Cli((sys.executable, "hf.mjs"), "0.8.126", "env", node)
    hf_cli.timeline(proj, cli=cli)
    hf_cli.lint(proj, cli=cli)
    hf_cli.render(proj, "index.html", str(tmp_path / "r.mov"), fmt="mov", cli=cli)
    assert [cwd for _a, cwd, _e in seen] == [proj, hf_cli.work_dir(), hf_cli.work_dir()]
    for _argv, _cwd, env in seen:
        assert env["HYPERFRAMES_FFPROBE_PATH"] == zenvi["ffprobe"]           # absolute, Zenvi's own
        assert env["HYPERFRAMES_FFMPEG_PATH"] == zenvi["ffmpeg"]
        assert env["HYPERFRAMES_NO_TELEMETRY"] == "1"
    timeline_env = seen[0][2]
    for key in hf_cli.dotenv_keys(proj):                                       # HyperFrames: `!(key in env)`
        assert key in timeline_env
    assert timeline_env["NODE_OPTIONS"] == "" and timeline_env["HYPERFRAMES_BROWSER_PATH"] == ""


def test_no_ffprobe_of_zenvis_own_means_no_cli_timeline(tmp_path, node, monkeypatch):
    monkeypatch.setattr(hf_cli, "zenvi_ffmpeg", lambda name: None)
    monkeypatch.setattr(node_runtime, "run_node", lambda *a, **k: pytest.fail("the CLI must not run"))
    cli = hf_cli.Cli((sys.executable, "hf.mjs"), "0.8.126", "env", node)
    with pytest.raises(hf_cli.CliError, match="no ffprobe of its own"):
        hf_cli.timeline(_project(tmp_path), cli=cli)
