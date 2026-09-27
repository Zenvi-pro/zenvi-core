"""Guard rails for the upstream-port agents (scripts/upstream-port/hooks/port-guard.py).

The hook is a Claude Code PreToolUse hook: it reads {"tool_name","tool_input":{"command"}} on stdin and
prints a deny decision, or nothing to allow. These tests pin the allow/deny matrix so a refactor cannot
silently let an agent push to develop or commit as someone else.
"""
import json
import os
import subprocess
import sys

import pytest

GUARD = os.path.join(os.path.dirname(__file__), "..", "scripts", "upstream-port", "hooks", "port-guard.py")


JASHAN = ("Jashan Pratap Singh", "88160290+jashanpratapsingh@users.noreply.github.com")


def run_guard(command, feature="qt-api", env_extra=None, cwd=None):
    env = dict(os.environ, PORT_FEATURE=feature, PORT_ROOT="/tmp/port-root", PORT_MAIN_REPO="/tmp/main-repo")
    env.update(env_extra or {})
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    proc = subprocess.run([sys.executable, GUARD], input=payload, capture_output=True, text=True, env=env, timeout=30, cwd=cwd)
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def _git(repo, *args, **env):
    e = dict(os.environ, GIT_AUTHOR_NAME=JASHAN[0], GIT_AUTHOR_EMAIL=JASHAN[1],
             GIT_COMMITTER_NAME=JASHAN[0], GIT_COMMITTER_EMAIL=JASHAN[1])
    e.update(env)
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=e)


@pytest.fixture
def repo(tmp_path):
    """A tiny repo whose refs/remotes/origin/develop is the base and HEAD has one Jashan-authored commit.

    The guard's push/PR check runs `git log origin/develop..HEAD`; the CI checkout has no such ref, so the
    tests bring their own instead of relying on the real repository."""
    r = tmp_path / "repo"; r.mkdir()
    _git(r, "init", "-q", "-b", "jashan/qt-api")
    (r / "base.txt").write_text("base\n"); _git(r, "add", "base.txt"); _git(r, "commit", "-q", "-m", "base")
    _git(r, "update-ref", "refs/remotes/origin/develop", "HEAD")
    (r / "feature.txt").write_text("feature\n"); _git(r, "add", "feature.txt")
    _git(r, "commit", "-q", "-m", "port(qt-api): feature commit")
    return r


@pytest.mark.parametrize("cmd", [
    "git status && git log -3",
    "git cherry-pick -m 1 -x abc123",
    "git commit --amend --reset-author --no-edit",
    "git rebase origin/develop",
    "git add src/qt_api.py tests/conftest.py",
    "ZENVI_DEPS=~/zenvi-deps-1.0 bash scripts/build-mac-libopenshot.sh",
    "cd /tmp/port-root/qt-api && ls",
    "cat /tmp/port-root/.state/qt-api.json",
    "cat <<'EOF' > note.md\nnever run: git push origin develop\nEOF",
])
def test_allowed(cmd):
    assert run_guard(cmd) is None


@pytest.mark.parametrize("cmd,fragment", [
    ("git push --force origin jashan/qt-api", "force push"),
    ("git push -f origin jashan/qt-api", "force push"),
    ("git push origin HEAD:develop", "never push to develop"),
    ("git push origin jashan/ai-masking", "another feature"),
    ("git checkout develop", "never check out"),
    ("git switch develop", "never check out"),
    ("git branch -D develop", "never delete"),
    ("git add -A", "git add -A"),
    ("git add .", "git add -A"),
    ("pip install PyQt6", "pip install is blocked"),
    (".venv/bin/python -m pip install foo", "pip install is blocked"),
    ("bash scripts/build-mac-libopenshot.sh", "explicit ZENVI_DEPS"),
    ("cd /tmp/main-repo && git status", "main checkout"),
    ("ls /tmp/port-root/ai-masking/src", "another feature's worktree"),
    ("git commit -m x -m 'Co-Authored-By: Someone <x@y.z>'", "Co-Authored-By"),
    ("git commit --author='Other Person <other@example.com>' -m x", "--author must be"),
    ("git worktree remove /tmp/port-root/qt-api", "human task"),
    ("gh pr merge 12", "do not merge"),
    ("git config user.email jashansingh@tenstorrent.com", "forbidden"),
])
def test_denied(cmd, fragment):
    reason = run_guard(cmd)
    assert reason is not None, "expected a denial for: %s" % cmd
    assert fragment in reason


@pytest.mark.parametrize("cmd", [
    "git push -u origin jashan/qt-api",
    "git push --force-with-lease origin jashan/qt-api",
    "gh pr create --draft --base develop --head jashan/qt-api --title t --body-file b.md",
])
def test_push_allowed_when_every_commit_is_jashan(repo, cmd):
    assert run_guard(cmd, cwd=repo) is None


def test_push_denied_for_foreign_author(repo):
    (repo / "x.txt").write_text("x\n"); _git(repo, "add", "x.txt")
    _git(repo, "commit", "-q", "-m", "upstream pick", GIT_AUTHOR_NAME="Jonathan Thomas", GIT_AUTHOR_EMAIL="jonathan@openshot.org")
    reason = run_guard("git push -u origin jashan/qt-api", cwd=repo)
    assert reason is not None and "is not Jashan Pratap Singh" in reason and "--reset-author" in reason


def test_push_denied_for_coauthor_trailer(repo):
    (repo / "y.txt").write_text("y\n"); _git(repo, "add", "y.txt")
    _git(repo, "commit", "-q", "-m", "fix", "-m", "Co-Authored-By: Someone <s@example.com>")
    reason = run_guard("git push -u origin jashan/qt-api", cwd=repo)
    assert reason is not None and "Co-Authored-By" in reason


def test_push_denied_when_base_ref_missing(tmp_path):
    r = tmp_path / "norepo"; r.mkdir(); _git(r, "init", "-q")
    reason = run_guard("git push -u origin jashan/qt-api", cwd=r)
    assert reason is not None and "could not verify authorship" in reason


def test_inactive_outside_agent_sessions():
    env = {k: v for k, v in os.environ.items() if k != "PORT_FEATURE"}
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git add -A"}})
    proc = subprocess.run([sys.executable, GUARD], input=payload, capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_pip_allowed_with_override():
    assert run_guard("pip install pytest", env_extra={"PORT_ALLOW_PIP": "1"}) is None
