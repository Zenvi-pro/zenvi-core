"""classes.handoff.node_runtime: finding Node.js and running it off the GUI thread."""

import os
import stat
import sys
import textwrap
import threading
import time

import pytest

from classes.handoff import node_runtime as nr

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake node executables are POSIX shell scripts")


def _fake_node(folder, version, with_npm_js=False):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "node")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo v%s; exit 0; fi\nexit 3\n" % version)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if with_npm_js:
        npm_bin = os.path.join(folder, "..", "lib", "node_modules", "npm", "bin")
        os.makedirs(npm_bin, exist_ok=True)
        for name in ("npm-cli.js", "npx-cli.js"):
            open(os.path.join(npm_bin, name), "w").close()
    return path


def _env(tmp_path, **extra):
    env = {"HOME": str(tmp_path / "home"), "PATH": str(tmp_path / "empty-path")}
    env.update(extra)
    return env


def test_search_order_starts_with_override_then_private_copy(tmp_path):
    override = tmp_path / "custom"
    env = _env(tmp_path, ZENVI_NODE=str(override))
    os.makedirs(override)
    paths = nr.candidate_paths(env, "darwin", user_path=str(tmp_path / "user"))
    assert paths[0] == os.path.join(str(override), "node")
    assert paths[1] == os.path.join(str(tmp_path / "user"), "hyperframes", "node", "bin", "node")
    assert "/opt/homebrew/bin/node" in paths and "/usr/local/bin/node" in paths
    assert os.path.join(str(tmp_path / "home"), ".volta", "bin", "node") in paths


def test_newest_nvm_version_comes_first(tmp_path):
    home = tmp_path / "home"
    for v in ("v18.20.0", "v22.3.0", "v20.11.1"):
        os.makedirs(home / ".nvm" / "versions" / "node" / v / "bin")
    paths = nr.candidate_paths(_env(tmp_path), "linux", user_path=str(tmp_path / "user"))
    nvm = [p for p in paths if ".nvm" in p]
    assert [p.split(os.sep)[-3] for p in nvm] == ["v22.3.0", "v20.11.1", "v18.20.0"]


def test_windows_candidates_cover_program_files_and_appdata(tmp_path):
    env = {"USERPROFILE": "C:\\Users\\me", "ProgramFiles": "C:\\Program Files", "APPDATA": "C:\\Users\\me\\AppData"
           "\\Roaming", "PATH": ""}
    paths = nr.candidate_paths(env, "win32", user_path="C:\\Users\\me\\.openshot_qt")
    assert paths[0].endswith(os.path.join("hyperframes", "node", "node.exe"))
    assert any(p.endswith(os.path.join("nodejs", "node.exe")) for p in paths)
    assert any(os.path.join("npm", "node.exe") in p for p in paths)


@pytest.fixture
def only_tmp_candidates(monkeypatch, tmp_path):
    """Keep this machine's real node installs out of find_node's search."""
    real = nr.candidate_paths

    def _only_tmp(*args, **kwargs):
        return [p for p in real(*args, **kwargs) if p.startswith(str(tmp_path))]

    monkeypatch.setattr(nr, "candidate_paths", _only_tmp)


def test_find_node_takes_the_first_new_enough_and_reports_too_old(tmp_path, only_tmp_candidates):
    old = _fake_node(str(tmp_path / "old"), "16.20.0")
    new = _fake_node(str(tmp_path / "new" / "bin"), "22.11.0", with_npm_js=True)
    env = _env(tmp_path, PATH=os.pathsep.join([os.path.dirname(old), os.path.dirname(new)]))
    rt = nr.find_node(18, env=env, user_path=str(tmp_path / "user"))
    assert rt.node == os.path.abspath(new) and rt.version == "22.11.0" and rt.major == 22
    assert rt.npm[0] == rt.node and rt.npm[1].endswith("npm-cli.js")
    assert rt.npx_argv("remotion", "render") [-2:] == ["remotion", "render"]
    assert rt.env({"PATH": "/usr/bin"})["PATH"].split(os.pathsep)[0] == rt.bin_dir

    env_old_only = _env(tmp_path, PATH=os.path.dirname(old))
    with pytest.raises(nr.NodeNotFound) as err:
        nr.find_node(18, env=env_old_only, user_path=str(tmp_path / "user"))
    assert "16.20.0" in str(err.value) and "ZENVI_NODE" in str(err.value) and "nodejs.org" in str(err.value)


def test_override_wins_and_a_broken_node_is_skipped(tmp_path, only_tmp_candidates):
    broken = tmp_path / "broken"
    os.makedirs(broken)
    (broken / "node").write_text("#!/bin/sh\nexit 1\n")
    os.chmod(broken / "node", 0o755)
    good = _fake_node(str(tmp_path / "good"), "20.0.0")
    env = _env(tmp_path, ZENVI_NODE=str(broken / "node"), PATH=os.path.dirname(good))
    rt = nr.find_node(18, env=env, user_path=str(tmp_path / "user"))
    assert rt.node == os.path.abspath(good)
    assert rt.npm == (os.path.join(os.path.dirname(good), "npm"),) or rt.npm[0].endswith("npm")


def test_run_node_streams_lines_and_returns_the_tail(tmp_path):
    seen = []
    script = "import sys\nfor i in range(5): print('line', i, flush=True)\nprint('oops', file=sys.stderr)\nsys.exit(7)"
    code, tail = nr.run_node([sys.executable, "-c", script], str(tmp_path), on_line=seen.append)
    assert code == 7
    assert seen[:5] == ["line 0", "line 1", "line 2", "line 3", "line 4"] and "oops" in tail


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_timeout_kills_the_whole_process_tree(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = textwrap.dedent(f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(pid_file)!r}, "w").write(str(child.pid))
        print("started", flush=True)
        time.sleep(60)
    """)
    started = time.monotonic()
    with pytest.raises(nr.NodeTimeout) as err:
        nr.run_node([sys.executable, "-c", script], str(tmp_path), timeout=2)
    assert time.monotonic() - started < 20
    assert "started" in err.value.tail
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _pid_alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            os.waitpid(child, os.WNOHANG)
        except ChildProcessError:
            pass
    assert not _pid_alive(child)


def test_cancel_stops_the_command(tmp_path):
    flag = threading.Event()
    timer = threading.Timer(0.5, flag.set)
    timer.start()
    try:
        with pytest.raises(nr.NodeCancelled):
            nr.run_node([sys.executable, "-c", "import time; time.sleep(30)"], str(tmp_path),
                        should_cancel=flag.is_set)
    finally:
        timer.cancel()


def test_a_missing_program_is_a_clear_error(tmp_path):
    with pytest.raises(nr.NodeRunError) as err:
        nr.run_node([str(tmp_path / "nope" / "node"), "x.js"], str(tmp_path))
    assert "could not start" in str(err.value)


def test_a_cancelled_node_command_is_a_job_cancellation(tmp_path):
    from classes.handoff import jobs
    assert issubclass(nr.NodeCancelled, jobs.JobCancelled)
    flag = threading.Event()
    flag.set()
    with pytest.raises(jobs.JobCancelled):
        nr.run_node([sys.executable, "-c", "import time; time.sleep(30)"], str(tmp_path), should_cancel=flag.is_set)
