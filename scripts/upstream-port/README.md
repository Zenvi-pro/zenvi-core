# Upstream port tooling (Zenvi <- OpenShot 4.0)

Ports OpenShot 4.0 features into Zenvi one PR per feature, each built by a Claude Code agent in its own git
worktree inside a tmux window. Branches are `jashan/<feature>`, cut from `develop` (or stacked on a
dependency's branch until it merges). All commits and PRs are authored by Jashan Pratap Singh only.

## Files
| file | purpose |
|---|---|
| `manifest.json` / `manifest.py` | the features: upstream merge commits to cherry-pick, dependencies, libopenshot prefix, human test outline (`manifest.py list|describe <f>|verify`) |
| `AGENT_PROMPT.md` | procedure + hard rules appended to every agent's system prompt |
| `PR_TEMPLATE.md` | PR body skeleton (follows `docs/PR_FORMAT.md`) |
| `hooks/port-guard.py` | PreToolUse hook (wired in `.claude/settings.json`): blocks wrong branches, force pushes, foreign authors, `pip install`, `git add -A`, wandering into other worktrees. Only active when `PORT_FEATURE` is set, i.e. inside agent sessions. |
| `launch-all.sh` | pre-flight checks, starts the `zenvi-port` tmux session (orchestrator + status windows) |
| `orchestrator.sh` | wave loop: starts features whose dependencies have PRs open, syncs merge state from GitHub |
| `new-worktree.sh <f>` | worktree + branch + bootstrap (`.venv` symlink, `.env` copy, identity, `.port-env`) |
| `launch-agent.sh <f>` | interactive Claude in the worktree (no MCP, no connectors, guard active); resumes on relaunch |
| `ensure-deps.sh <prefix> [tag] [audio_tag]` | locked, idempotent libopenshot build into a prefix |
| `run-app.sh` / `test-all.sh` | isolated-HOME app launch and the agent test gate (run from a worktree) |
| `set-status.sh` / `status.sh` | per-feature state files under `$PORT_ROOT/.state`, dashboard |
| `retarget.sh <f>` | after a dependency merges: rebase onto develop, force-with-lease, `gh pr edit --base develop` |
| `shutdown.sh [--remove-merged|--remove-all]` | stop everything; optionally clean worktrees |

Layout on disk: worktrees at `~/Projects/zenvi-worktrees/<feature>`, state/logs/isolated HOMEs under
`~/Projects/zenvi-worktrees/.state|.logs|.home`. libopenshot prefixes: `~/zenvi-deps` (0.5.0, develop),
`~/zenvi-deps-1.0` (1.0.0, built by the libopenshot-1.0 feature), `~/zenvi-deps-qt6` (later).

## Run
```bash
bash scripts/upstream-port/launch-all.sh                         # everything, dependency-ordered
bash scripts/upstream-port/launch-all.sh --only qt-api,pre-qt6-fixes
bash scripts/upstream-port/launch-all.sh --only remove-webview-timeline --force   # start a held feature
tmux attach -t zenvi-port        # Ctrl-b w: pick a feature window and talk to its agent; Ctrl-b d: detach
bash scripts/upstream-port/status.sh
tail -f ~/Projects/zenvi-worktrees/.logs/<feature>.tmux.log
```
When a feature shows `needs_human`, open its window and answer; the agent continues. A dead window:
`tmux new-window -t zenvi-port -n <f> "bash scripts/upstream-port/launch-agent.sh <f>"` (resumes the session).
After a dependency PR merges: `bash scripts/upstream-port/retarget.sh <dependent-feature>`.
Stop: `bash scripts/upstream-port/shutdown.sh`; clean merged worktrees: `--remove-merged`.

## Waves
0. `libopenshot-1.0`, `pre-qt6-fixes`, `timeline-qwidget-default`, `qt-api` (parallel, from develop)
1. `ai-masking`, `effects-basic-1.0`, `color-grade`, `post-qt6-fixes`, `optimize-preview`, `comfy-ui`
2. `film-grain` (on color-grade), `screen-recording` (on ai-masking)
3. `audio-viz-beat-sync` (on film-grain)
4. held until a release ships with the native timeline default: `remove-webview-timeline`, then `qt6-default`

Skipped upstream work and why: see `skipped` in `manifest.json`.
