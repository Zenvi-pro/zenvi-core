"""The engine bridge runtime installed on demand (classes/engine_runtime.py), with fake downloads.

Everything is served from file:// URLs under tmp_path: a fake Node.js archive
(a shell script that answers ``--version`` and hands everything else to this
Python) and a CLI bundle whose zenvi.mjs is the fake CLI
(tests/fixtures/engine_bridge/fake_zenvi.py). No network, no real Node.
"""

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import urllib.error

import pytest

from test_engine_bridge import FAKE

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake Node is a shell script")


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _tar(path, top, files):
    """A .tar.gz holding *files* ({relative name: (text, mode)}) under the folder *top*."""
    with tarfile.open(path, "w:gz") as bundle:
        for name, (text, mode) in files.items():
            data = text.encode()
            member = tarfile.TarInfo(f"{top}/{name}")
            member.size, member.mode = len(data), mode
            bundle.addfile(member, io.BytesIO(data))
    return str(path)


def _fake_node_script():
    return (f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo v24.22.1; exit 0; fi\n'
            f'exec "{sys.executable}" "$@"\n')


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """engine_runtime with its folders, Node download and node search confined to tmp_path."""
    from classes import engine_bridge, engine_runtime, info

    monkeypatch.setattr(info, "PATH", str(tmp_path / "app"))
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    for name in ("ZENVI_ENGINE_BRIDGE", "ZENVI_ENGINE_CLI", "ZENVI_NODE", "ZENVI_ENGINE_SOURCE"):
        monkeypatch.delenv(name, raising=False)
    # No Node of this machine's (PATH, Homebrew, Volta): only what the test installs.
    real = engine_bridge.node_candidates
    monkeypatch.setattr(engine_bridge, "node_candidates",
                        lambda env=None: [p for p in real(env) if p.startswith(str(tmp_path))])
    engine_bridge.forget_node_versions()
    engine_bridge.set_client(None)

    serve = tmp_path / "serve"
    serve.mkdir()
    asset = engine_runtime.node_asset()
    node_name = f"node-v{engine_runtime.NODE_VERSION}-{asset}"
    node_archive = _tar(serve / node_name, node_name.replace(".tar.gz", ""), {
        "bin/node": (_fake_node_script(), 0o755), "LICENSE": ("MIT", 0o644)})
    monkeypatch.setattr(engine_runtime, "NODE_DIST", serve.as_uri() + "/")
    monkeypatch.setattr(engine_runtime, "NODE_SHA256", {asset: _sha(node_archive)})

    opened = []
    real_open = engine_runtime._open

    def counting_open(url):
        opened.append(url.rsplit("/", 1)[-1])
        return real_open(url)
    monkeypatch.setattr(engine_runtime, "_open", counting_open)

    class Runtime:
        module = engine_runtime
        bridge = engine_bridge
        root = tmp_path
        downloads = opened

        def release(self, version="0.1.0", protocol=None, corrupt=False, extra=()):
            """Publish a CLI archive + manifest under serve/; the manifest's path."""
            name = f"zenvi-cli-{version}.tar.gz"
            archive = _tar(serve / name, f"zenvi-cli-{version}", {
                "zenvi.mjs": (open(FAKE).read(), 0o755), "assets/colors/look.cube": ("LUT", 0o644)})
            entry = {"version": version, "protocol": engine_bridge.PROTOCOL if protocol is None else protocol,
                     "url": name, "sha256": "0" * 64 if corrupt else _sha(archive)}
            manifest = serve / "engine-cli.json"
            manifest.write_text(json.dumps({"releases": [*extra, entry]}))
            return str(manifest)

    yield Runtime()
    engine_bridge.set_client(None)
    engine_bridge.forget_node_versions()


def test_first_use_downloads_node_and_the_cli_and_turns_the_bridge_on(runtime):
    rt, bridge = runtime.module, runtime.bridge
    assert bridge.bridge_mode() == "" and not rt.status()["ready"]
    seen = []
    status = rt.install(runtime.release(), progress=lambda text, done, total: seen.append((text, done, total)))
    assert status["ready"] and status["managed"] and status["cli_version"] == "0.1.0"
    assert status["node"] == bridge.managed_node() and status["node_version"] == "v24"
    assert os.path.isfile(os.path.join(rt.home(), "cli", "assets", "colors", "look.cube"))
    assert rt.installed()["cli"]["protocol"] == bridge.PROTOCOL
    assert any(text.startswith("Downloading Node.js") and done for text, done, _ in seen)
    assert any(text.startswith("Downloading the zenvi CLI") for text, _, _ in seen)
    # Nothing staged is left behind.
    assert sorted(os.listdir(rt.home())) == ["cli", "node", "runtime.json"]
    # On by default now; the client runs the installed CLI on the installed Node.
    assert bridge.bridge_mode() == "server"
    client = bridge.get_client()
    assert (client.node, client.cli) == (bridge.managed_node(), bridge.managed_cli())
    assert any(t["name"] == "add_motion_template_tool" for t in client.tools())


def test_second_use_is_instant_and_an_update_reuses_the_node(runtime, monkeypatch):
    rt, bridge = runtime.module, runtime.bridge
    rt.install(runtime.release())
    online = rt._open

    def offline(url):
        raise urllib.error.URLError("no network")
    monkeypatch.setattr(rt, "_open", offline)
    # A later launch only looks at the disk.
    bridge.forget_node_versions()
    assert bridge.bridge_mode() == "server" and rt.status()["ready"]
    monkeypatch.setattr(rt, "_open", online)
    runtime.downloads.clear()
    rt.install(runtime.release("0.2.0"))
    assert runtime.downloads == ["engine-cli.json", "zenvi-cli-0.2.0.tar.gz"]
    assert rt.installed()["cli"]["version"] == "0.2.0"


def test_offline_is_refused_clearly_and_installs_nothing(runtime, monkeypatch):
    rt = runtime.module
    manifest = runtime.release()

    def offline(url):
        raise urllib.error.URLError(OSError(8, "nodename nor servname provided"))
    monkeypatch.setattr(rt, "_open", offline)
    with pytest.raises(rt.RuntimeInstallError, match="Could not reach .*one-time download"):
        rt.install("https://releases.example.invalid/engine-cli.json")
    with pytest.raises(rt.RuntimeInstallError, match="Could not reach"):
        rt.install(manifest)
    assert not os.path.exists(os.path.join(rt.home(), "cli")) and not rt.installed()


def test_a_download_that_fails_its_checksum_keeps_the_installed_runtime(runtime):
    rt = runtime.module
    rt.install(runtime.release("0.1.0"))
    with pytest.raises(rt.RuntimeInstallError, match="checksum"):
        rt.install(runtime.release("0.2.0", corrupt=True))
    assert rt.installed()["cli"]["version"] == "0.1.0" and rt.status()["ready"]
    assert sorted(os.listdir(rt.home())) == ["cli", "node", "runtime.json"]


def test_a_tampered_node_download_is_refused(runtime, monkeypatch):
    rt = runtime.module
    monkeypatch.setattr(rt, "NODE_SHA256", {rt.node_asset(): "f" * 64})
    with pytest.raises(rt.RuntimeInstallError, match="checksum"):
        rt.install(runtime.release())
    assert not os.path.exists(os.path.join(rt.home(), "node")) and not rt.installed()


def test_the_newest_release_speaking_this_protocol_is_installed(runtime):
    rt, bridge = runtime.module, runtime.bridge
    newer_but_other = {"version": "9.0.0", "protocol": bridge.PROTOCOL + 1, "url": "nope.tar.gz", "sha256": "0" * 64}
    rt.install(runtime.release("0.3.0", extra=[newer_but_other]))
    assert rt.installed()["cli"]["version"] == "0.3.0"
    # Only other protocols on offer: refused before anything is downloaded.
    runtime.downloads.clear()
    with pytest.raises(rt.RuntimeInstallError, match=f"bridge protocol {bridge.PROTOCOL}; it offers 1"):
        rt.install(runtime.release("0.4.0", protocol=1))
    assert runtime.downloads == ["engine-cli.json"]


def test_a_cli_answering_another_protocol_is_not_installed(runtime, monkeypatch):
    rt = runtime.module
    monkeypatch.setenv("FAKE_ZENVI_PROTOCOL", "99")
    with pytest.raises(rt.RuntimeInstallError, match="protocol 99"):
        rt.install(runtime.release())
    assert not os.path.exists(os.path.join(rt.home(), "cli")) and not rt.installed()


def test_cancel_stops_the_download_and_leaves_nothing(runtime):
    rt = runtime.module
    cancel = threading.Event()

    def progress(text, done, total):
        if done:
            cancel.set()
    with pytest.raises(rt.InstallCancelled):
        rt.install(runtime.release(), progress=progress, cancel=cancel)
    assert os.listdir(rt.home()) == [] and not rt.installed()


def test_an_existing_node_is_used_instead_of_downloading_one(runtime):
    rt, bridge = runtime.module, runtime.bridge
    hyperframes = runtime.root / "user" / "hyperframes" / "node" / "bin"
    hyperframes.mkdir(parents=True)
    (hyperframes / "node").write_text(_fake_node_script())
    (hyperframes / "node").chmod(0o755)
    status = rt.install(runtime.release())
    assert status["node"] == str(hyperframes / "node")
    assert not os.path.exists(bridge.managed_node_dir())
    assert not any(name.startswith("node-v") for name in runtime.downloads)


def test_a_local_cli_build_is_copied(runtime, tmp_path):
    rt = runtime.module
    dist = tmp_path / "dist"
    (dist / "fonts").mkdir(parents=True)
    shutil.copy(FAKE, dist / "zenvi.mjs")
    (dist / "fonts" / "x.ttf").write_text("font")
    status = rt.install(str(dist / "zenvi.mjs"))
    assert status["ready"] and status["cli_version"] == "fake"
    assert os.path.isfile(os.path.join(rt.home(), "cli", "fonts", "x.ttf"))
    with pytest.raises(rt.RuntimeInstallError, match="No zenvi CLI build"):
        rt.install(str(tmp_path / "nowhere"))


def test_without_a_source_the_install_says_where_to_set_one(runtime):
    with pytest.raises(runtime.module.RuntimeInstallError, match="Zenvi Engine Tools Source"):
        runtime.module.install("")


def test_the_kill_switch_lists_no_engine_tools_even_when_installed(runtime, monkeypatch):
    from classes.editor_tools import engine_bridge as bridged

    rt, bridge = runtime.module, runtime.bridge
    monkeypatch.setattr(bridged, "BRIDGED", {})
    assert bridged.register_tools(set()) == {}          # not installed: nothing listed
    rt.install(runtime.release())
    monkeypatch.setenv("ZENVI_ENGINE_BRIDGE", "off")
    assert bridge.bridge_mode() == "" and bridged.register_tools(set()) == {}
    with pytest.raises(bridge.BridgeUnavailable, match="turned off"):
        bridge.get_client()
    monkeypatch.delenv("ZENVI_ENGINE_BRIDGE")           # back to auto: listed
    assert "add_motion_template_tool" in bridged.register_tools(set())


def test_the_packaging_script_makes_a_release_the_desktop_installs(runtime, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    shutil.copy(FAKE, dist / "zenvi.mjs")
    node = tmp_path / "fake-node"
    node.write_text(_fake_node_script())
    node.chmod(node.stat().st_mode | stat.S_IEXEC)
    out = tmp_path / "release"
    subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "package_engine_cli.py"), str(dist),
                    "--out", str(out), "--node", str(node), "--version", "0.5.0"], check=True, capture_output=True)
    manifest = json.loads((out / "engine-cli.json").read_text())
    assert [r["version"] for r in manifest["releases"]] == ["0.5.0"]
    assert manifest["releases"][0]["protocol"] == runtime.bridge.PROTOCOL
    status = runtime.module.install(str(out / "engine-cli.json"))
    assert status["ready"] and status["cli_version"] == "0.5.0"


def test_an_unknown_tool_says_where_engine_tools_come_from_while_none_is_listed(monkeypatch):
    from classes import tool_handlers
    from classes.editor_tools import engine_bridge as bridged

    monkeypatch.setattr(bridged, "BRIDGED", {})
    monkeypatch.setenv("ZENVI_ENGINE_BRIDGE", "auto")
    receipt = json.loads(tool_handlers.execute_tool("add_motion_block_tool", {}))
    assert "Unknown tool 'add_motion_block_tool'" in receipt["summary"]
    assert "AI Tools > Zenvi Engine Tools... installs them" in receipt["summary"]
    monkeypatch.setenv("ZENVI_ENGINE_BRIDGE", "off")
    receipt = json.loads(tool_handlers.execute_tool("add_motion_block_tool", {}))
    assert receipt["summary"].endswith("Unknown tool 'add_motion_block_tool'.")


def test_a_remote_source_must_be_https(runtime):
    rt = runtime.module
    with pytest.raises(rt.RuntimeInstallError, match="only downloaded over https"):
        rt.install("http://releases.example.com/engine-cli.json")
    assert runtime.downloads == []
