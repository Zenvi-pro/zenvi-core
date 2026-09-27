#!/usr/bin/env python3
"""Guard rails for upstream-port agents (Claude Code PreToolUse hook, Bash).

Active only inside an agent session started by scripts/upstream-port/launch-agent.sh
(it exports PORT_FEATURE / PORT_ROOT / PORT_MAIN_REPO). In any other Claude session
in this repo the hook exits immediately and allows everything.

What it blocks for port agents:
  * any reference to a non-personal git identity
  * pushing anything other than the agent's own jashan/<feature> branch, force pushes
    (except --force-with-lease of that same branch), pushing to develop
  * checking out / resetting / deleting develop, removing worktrees
  * git add -A / --all / . (adds must name paths: keeps .venv, .port-env out)
  * commits carrying Co-Authored-By trailers or a foreign --author
  * pip installs into the shared venv (PORT_ALLOW_PIP=1 lifts this)
  * running scripts/build-mac-libopenshot.sh without an explicit ZENVI_DEPS= prefix
  * cd/paths into the main checkout or another feature's worktree
  * before `git push` / `gh pr create` / `gh pr ready`: every commit on top of the
    base must be authored AND committed by Jashan Pratap Singh, with no trailers.
Heredoc bodies are ignored so agents can still write about these commands.
"""
import json
import os
import re
import subprocess
import sys

FEATURE = os.environ.get("PORT_FEATURE", "").strip()
if not FEATURE:
    sys.exit(0)  # not a port-agent session

PORT_ROOT = os.path.realpath(os.path.expanduser(os.environ.get("PORT_ROOT", "~/Projects/zenvi-worktrees")))
MAIN_REPO = os.path.realpath(os.path.expanduser(os.environ.get("PORT_MAIN_REPO", "~/Projects/zenvi")))
BASE_REF = os.environ.get("PORT_BASE_REF", "origin/develop")
OWN_BRANCH = "jashan/" + FEATURE
PERSONAL_NAME = "Jashan Pratap Singh"
ALLOWED_EMAILS = {
    "jashanpratap123@gmail.com",
    "88160290+jashanpratapsingh@users.noreply.github.com",
}
FORBIDDEN_IDENTITY = re.compile(r"tenstorrent|jashansinghtt|jashansingh@", re.I)
SEGMENT_SPLIT = re.compile(r"(?:&&|\|\||[;&|\n()`]|\$\()")
HEREDOC_START = re.compile(r"<<-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?")
CMD_AT_POS = lambda name: re.compile(r"^\s*(?:\w+=\S*\s+)*" + name + r"(?:\s|$)")  # noqa: E731


def deny(reason):
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": "[upstream-port guard] " + reason}}, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(0)


def strip_heredocs(cmd):
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = HEREDOC_START.search(line)
        i += 1
        if m:
            term = m.group(1)
            while i < len(lines) and lines[i].strip() != term:
                i += 1
            i += 1
    return "\n".join(out)


def words(seg):
    return re.findall(r"""(?:[^\s"']+|"[^"]*"|'[^']*')+""", seg)


def check_authorship():
    """All commits since the base must be by Jashan, no co-author trailers."""
    try:
        out = subprocess.run(["git", "log", BASE_REF + "..HEAD", "--format=%an <%ae>%n%cn <%ce>%n%B%x00"],
                             capture_output=True, text=True, timeout=20)
    except Exception as exc:  # pragma: no cover
        deny("could not verify authorship: %s" % exc)
    if out.returncode != 0:
        deny("could not verify authorship: git log failed (%s). Is %s fetched?" % (out.stderr.strip(), BASE_REF))
    for block in out.stdout.split("\x00"):
        block = block.strip("\n")
        if not block.strip():
            continue
        lines = block.split("\n")
        for ident in lines[:2]:
            m = re.match(r"^(.*) <(.*)>$", ident.strip())
            if not m or m.group(1) != PERSONAL_NAME or m.group(2) not in ALLOWED_EMAILS:
                deny("commit by '%s' is not %s. Run: git commit --amend --reset-author --no-edit "
                     "(or an interactive rebase --exec of the same) before pushing." % (ident.strip(), PERSONAL_NAME))
        body = "\n".join(lines[2:])
        if re.search(r"^co-authored-by:", body, re.I | re.M):
            deny("a commit carries a Co-Authored-By trailer; PRs must come from Jashan only. Amend the message.")
        if FORBIDDEN_IDENTITY.search(body):
            deny("a commit message references a forbidden identity.")


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if data.get("tool_name") != "Bash":
        sys.exit(0)
    cmd = data.get("tool_input", {}).get("command", "") or ""
    cmd_nohd = strip_heredocs(cmd)

    if FORBIDDEN_IDENTITY.search(cmd_nohd):
        deny("forbidden (work) identity in command.")

    # Path containment: no wandering into the main checkout or sibling worktrees.
    own_wt = os.path.join(PORT_ROOT, FEATURE)
    for p in re.findall(r"(?<![\w.-])(?:~|/)[^\s\"';|&)<>]*", cmd_nohd):
        rp = os.path.realpath(os.path.expanduser(p.rstrip("/)")))
        if rp == MAIN_REPO or rp.startswith(MAIN_REPO + "/"):
            deny("path %s is the main checkout; work only inside %s." % (p, own_wt))
        if rp.startswith(PORT_ROOT + "/"):
            rest = rp[len(PORT_ROOT) + 1:].split("/")[0]
            if rest and not rest.startswith(".") and rest != FEATURE:
                deny("path %s belongs to another feature's worktree." % p)

    needs_author_check = False
    for seg in SEGMENT_SPLIT.split(cmd_nohd):
        if not seg.strip():
            continue
        w = words(seg)
        if CMD_AT_POS("pip3?").match(seg) or re.search(r"\bpip3?\s+install\b", seg) or re.search(r"python3?\s+-m\s+pip\s+install", seg):
            if os.environ.get("PORT_ALLOW_PIP") != "1":
                deny("pip install is blocked: .venv is shared with the main checkout. If the feature needs a new "
                     "dependency, add it to requirements*.txt, set status needs_human and stop.")
        if "build-mac-libopenshot.sh" in seg and "ZENVI_DEPS=" not in seg:
            deny("run build-mac-libopenshot.sh only with an explicit ZENVI_DEPS=<prefix> (never overwrite ~/zenvi-deps).")
        if CMD_AT_POS("gh").match(seg):
            if re.search(r"\bpr\s+(create|ready|merge)\b", seg):
                if "merge" in seg:
                    deny("agents do not merge PRs; a human does.")
                needs_author_check = True
                if re.search(r"--base\s+(\S+)", seg) and not re.search(r"--head\s+", seg):
                    pass
        if not CMD_AT_POS("git").match(seg):
            continue
        # ---- git subcommands ----
        low = seg.lower()
        if re.search(r"\bgit\s+(-C\s+\S+\s+)?push\b", seg):
            if re.search(r"\s(-f|--force)(\s|$)", seg) and "--force-with-lease" not in seg:
                deny("force push is blocked; use --force-with-lease on your own branch only.")
            if re.search(r"\bdevelop\b|\breleases\b|\bmain\b", seg):
                deny("never push to develop/releases/main. Push only %s." % OWN_BRANCH)
            if "--all" in w or "--mirror" in w or "--tags" in w:
                deny("push only %s, not --all/--tags/--mirror." % OWN_BRANCH)
            refs = [x for x in w[w.index("push") + 1:] if not x.startswith("-")] if "push" in w else []
            for r in refs[1:]:  # first non-flag is the remote
                dst = r.split(":")[-1]
                if dst not in ("HEAD", OWN_BRANCH, "refs/heads/" + OWN_BRANCH) and not dst.startswith("jashan/"):
                    deny("push target '%s' is not your branch %s." % (r, OWN_BRANCH))
                if dst.startswith("jashan/") and dst != OWN_BRANCH:
                    deny("push target '%s' belongs to another feature (yours is %s)." % (r, OWN_BRANCH))
            needs_author_check = True
        if re.search(r"\bgit\s+(checkout|switch)\s+(?!.*\s--\s)", seg) and re.search(r"\s(origin/)?(develop|releases|main)(\s|$)", seg) and " -- " not in seg and "-b " not in seg and "-c " not in seg:
            deny("never check out develop/releases/main in a port worktree; stay on %s." % OWN_BRANCH)
        if re.search(r"\bgit\s+branch\s+(-D|-d|-f|--force|-M|-m)\s+.*\b(develop|releases|main)\b", seg):
            deny("never delete/move/force develop, releases or main.")
        if re.search(r"\bgit\s+worktree\s+(remove|prune|move)\b", seg):
            deny("worktree removal is a human task (scripts/upstream-port/shutdown.sh).")
        if re.search(r"\bgit\s+add\b", seg) and (re.search(r"\s(-A|--all)(\s|$)", seg) or re.search(r"\bgit\s+add\s+\.(\s|$)", seg)):
            deny("git add -A/--all/. is blocked; add explicit paths (keeps .venv/.port-env/graphify-out out).")
        if re.search(r"\bgit\s+(commit|cherry-pick|rebase|am|merge)\b", seg):
            if re.search(r"co-authored-by", low):
                deny("Co-Authored-By trailers are not allowed on port commits.")
            m = re.search(r"--author[= ]\s*[\"']?([^\"']+)", seg)
            if m and not (PERSONAL_NAME in m.group(1) and any(e in m.group(1) for e in ALLOWED_EMAILS)):
                deny("--author must be Jashan Pratap Singh with an allowed email.")
        if re.search(r"\bgit\s+(reset\s+--hard|rebase|filter-repo|filter-branch)\b", seg) and re.search(r"\s(origin/)?(develop|releases|main)(\s|$)", seg):
            # rebasing ONTO develop is fine; rewriting develop is not. Allow when we're on our own branch.
            try:
                cur = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
            except Exception:
                cur = ""
            if cur in ("develop", "releases", "main"):
                deny("HEAD is %s; port worktrees must stay on %s." % (cur, OWN_BRANCH))
    if needs_author_check:
        check_authorship()
    sys.exit(0)


if __name__ == "__main__":
    main()
