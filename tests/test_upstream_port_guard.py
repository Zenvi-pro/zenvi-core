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


def run_guard(command, feature="qt-api", env_extra=None):
    env = dict(os.environ, PORT_FEATURE=feature, PORT_ROOT="/tmp/port-root", PORT_MAIN_REPO="/tmp/main-repo")
    env.update(env_extra or {})
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    proc = subprocess.run([sys.executable, GUARD], input=payload, capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("cmd", [
    "git status && git log -3",
    "git cherry-pick -m 1 -x abc123",
    "git commit --amend --reset-author --no-edit",
    "git rebase origin/develop",
    "git add src/qt_api.py tests/conftest.py",
    "git push -u origin jashan/qt-api",
    "git push --force-with-lease origin jashan/qt-api",
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


def test_inactive_outside_agent_sessions():
    env = {k: v for k, v in os.environ.items() if k != "PORT_FEATURE"}
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git add -A"}})
    proc = subprocess.run([sys.executable, GUARD], input=payload, capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_pip_allowed_with_override():
    assert run_guard("pip install pytest", env_extra={"PORT_ALLOW_PIP": "1"}) is None
