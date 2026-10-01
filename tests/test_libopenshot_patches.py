"""Zenvi's libopenshot source patches and the helper every build path applies them with.

installer/apply-libopenshot-patches.sh is called by scripts/build-mac-libopenshot.sh, the Windows
release job (installer/ci-win-msys-libopenshot.sh), the macOS release job in
.github/workflows/release.yml and run-win.ps1. It must apply a patch, skip it when it is already
applied (so a re-run is safe), and fail the build when it no longer applies. The old rule skipped
such a patch with a notice, which ships the bug the patch fixes.
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HELPER = os.path.join(ROOT, "installer", "apply-libopenshot-patches.sh")
PATCH_DIR = os.path.join(ROOT, "installer", "mac-patches")
DISCARD_PATCH = os.path.join(PATCH_DIR, "libopenshot-v1.0.0-discard-preroll.patch")

JASHAN = ("Jashan Pratap Singh", "88160290+jashanpratapsingh@users.noreply.github.com")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git")

UPSTREAM = "line one\nline two\nline three\n"
PATCHED = "line one\nline two, fixed\nline three\n"


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME=JASHAN[0], GIT_AUTHOR_EMAIL=JASHAN[1],
               GIT_COMMITTER_NAME=JASHAN[0], GIT_COMMITTER_EMAIL=JASHAN[1])
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env)


def run_helper(src, *patches):
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
    return subprocess.run(["bash", HELPER, str(src), *map(str, patches)],
                          capture_output=True, text=True, timeout=60, env=env)


@pytest.fixture
def src(tmp_path):
    """A one-file checkout standing in for a fresh upstream libopenshot clone."""
    repo = tmp_path / "libopenshot"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "FFmpegReader.cpp").write_text(UPSTREAM)
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "upstream")
    return repo


@pytest.fixture
def patch(src, tmp_path):
    """A patch with a prose header, like the real ones (git apply ignores text before the diff)."""
    target = src / "src" / "FFmpegReader.cpp"
    target.write_text(PATCHED)
    diff = _git(src, "diff").stdout
    target.write_text(UPSTREAM)
    path = tmp_path / "libopenshot-v1.0.0-fix.patch"
    path.write_text("Why this patch exists.\n\n" + diff)
    return path


def _source(src):
    return (src / "src" / "FFmpegReader.cpp").read_text()


def test_applies_a_patch(src, patch):
    proc = run_helper(src, patch)
    assert proc.returncode == 0, proc.stderr
    assert "Applying libopenshot-v1.0.0-fix.patch" in proc.stdout
    assert _source(src) == PATCHED


def test_rerun_skips_an_already_applied_patch(src, patch):
    assert run_helper(src, patch).returncode == 0
    proc = run_helper(src, patch)
    assert proc.returncode == 0, proc.stderr
    assert "already applied" in proc.stdout
    assert _source(src) == PATCHED


def test_patch_that_no_longer_applies_fails_the_build(src, patch):
    (src / "src" / "FFmpegReader.cpp").write_text("line one\nline 2, rewritten upstream\nline three\n")
    proc = run_helper(src, patch)
    assert proc.returncode != 0
    assert "libopenshot-v1.0.0-fix.patch does not apply" in proc.stderr
    assert _source(src) == "line one\nline 2, rewritten upstream\nline three\n"


def test_failure_is_a_github_actions_error_annotation(src, patch):
    (src / "src" / "FFmpegReader.cpp").write_text("rewritten upstream\n")
    proc = subprocess.run(["bash", HELPER, str(src), str(patch)], capture_output=True, text=True,
                          timeout=60, env=dict(os.environ, GITHUB_ACTIONS="true"))
    assert proc.returncode != 0
    assert "::error::libopenshot-v1.0.0-fix.patch does not apply" in proc.stdout


def test_crlf_patch_fails_with_a_line_ending_hint(src, patch, tmp_path):
    # What a Windows checkout with core.autocrlf=true produced before .gitattributes pinned LF.
    crlf = tmp_path / "crlf.patch"
    crlf.write_bytes(patch.read_bytes().replace(b"\n", b"\r\n"))
    proc = run_helper(src, crlf)
    assert proc.returncode != 0
    assert "CRLF line endings" in proc.stderr
    assert _source(src) == UPSTREAM


def test_missing_patch_file_fails(src, tmp_path):
    proc = run_helper(src, tmp_path / "nope.patch")
    assert proc.returncode != 0
    assert "patch not found" in proc.stderr


def test_no_patches_is_a_no_op(src):
    proc = run_helper(src)
    assert proc.returncode == 0, proc.stderr
    assert _source(src) == UPSTREAM


def test_discard_preroll_patch_touches_only_the_ffmpeg_reader():
    proc = subprocess.run(["git", "apply", "--numstat", DISCARD_PATCH], capture_output=True, text=True,
                          timeout=30, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    files = [line.split("\t")[2] for line in proc.stdout.splitlines()]
    assert files == ["src/FFmpegReader.cpp"]


def test_patches_are_kept_lf():
    with open(DISCARD_PATCH, "rb") as fh:
        assert b"\r" not in fh.read()
    proc = subprocess.run(["git", "check-attr", "eol", "--", "installer/mac-patches/any.patch"],
                          capture_output=True, text=True, timeout=30, cwd=ROOT)
    if proc.returncode != 0:
        pytest.skip("not a git checkout")
    assert proc.stdout.strip().endswith("eol: lf")


@pytest.mark.parametrize("path", [
    "scripts/build-mac-libopenshot.sh",
    "installer/ci-win-msys-libopenshot.sh",
    ".github/workflows/release.yml",
    "run-win.ps1",
])
def test_every_libopenshot_build_applies_patches_through_the_helper(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        text = fh.read()
    assert "installer/apply-libopenshot-patches.sh" in text


def test_run_win_applies_the_discard_preroll_patch():
    # run-win.ps1 builds libopenshot's default branch, not a tag, so it names the patch directly.
    with open(os.path.join(ROOT, "run-win.ps1"), encoding="utf-8") as fh:
        assert "installer/mac-patches/libopenshot-v1.0.0-discard-preroll.patch" in fh.read()
