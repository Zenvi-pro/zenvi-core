"""scripts/bundle_engine_cli.py: a release build ships the zenvi CLI where the frozen app finds it.

freeze.py copies all of src/ into the app's lib/ (info.PATH when frozen), so a
CLI placed in src/engine is found as <app>/engine/zenvi.mjs. The CLI is the
fake one (tests/fixtures/engine_bridge/fake_zenvi.py), started by this Python.
"""

import os
import subprocess
import sys

from test_engine_bridge import FAKE

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(ROOT, "scripts", "bundle_engine_cli.py")


def _dist(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets" / "colors").mkdir(parents=True)
    (dist / "zenvi.mjs").write_text(open(FAKE).read())
    (dist / "zenvi.mjs.map").write_text("{}")
    (dist / "assets" / "colors" / "look.cube").write_text("LUT")
    return dist


def _bundle(dist, dest, protocol=None):
    env = dict(os.environ)
    env.pop("FAKE_ZENVI_PROTOCOL", None)
    if protocol is not None:
        env["FAKE_ZENVI_PROTOCOL"] = str(protocol)
    return subprocess.run([sys.executable, SCRIPT, str(dist), "--node", sys.executable, "--dest", str(dest)],
                          capture_output=True, text=True, env=env)


def test_a_build_speaking_this_protocol_is_bundled_where_the_frozen_app_finds_it(tmp_path, monkeypatch):
    from classes import engine_bridge, info

    app = tmp_path / "lib"
    done = _bundle(_dist(tmp_path), app / "engine")
    assert done.returncode == 0, done.stderr
    assert (app / "engine" / "assets" / "colors" / "look.cube").read_text() == "LUT"
    assert not (app / "engine" / "zenvi.mjs.map").exists()
    monkeypatch.setattr(info, "PATH", str(app))
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    monkeypatch.delenv("ZENVI_ENGINE_CLI", raising=False)
    monkeypatch.setattr(engine_bridge, "_setting", lambda key: "")
    assert engine_bridge.find_cli() == str(app / "engine" / "zenvi.mjs")


def test_a_build_speaking_another_protocol_fails_the_release(tmp_path):
    from classes import engine_bridge

    done = _bundle(_dist(tmp_path), tmp_path / "lib" / "engine", protocol=99)
    assert done.returncode != 0
    assert "99" in done.stderr and str(engine_bridge.PROTOCOL) in done.stderr
    assert not (tmp_path / "lib" / "engine").exists()
