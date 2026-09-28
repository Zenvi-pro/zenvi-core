# Upstream-port agent procedure (Zenvi <- OpenShot 4.0)

You are one of several Claude Code agents, each porting ONE feature from upstream OpenShot into Zenvi in its own
git worktree. A human (Jashan) may attach to your tmux window at any time and talk to you; otherwise work
autonomously and carefully. Everything you ship must be testable by a human who has not read the diff.

## Hard rules (a PreToolUse hook enforces most of these; do not try to work around it)
- Work ONLY inside your worktree (`$PWD`). Never touch `~/Projects/zenvi` (the main checkout) or another
  feature's worktree under `$PORT_ROOT`. Never `cd` out of your worktree.
- Stay on your branch `$PORT_BRANCH`. Never check out, reset, rebase or push `develop`, `releases` or `main`.
- Push only `$PORT_BRANCH`. No force pushes (only `--force-with-lease` of your own branch during a retarget).
- Every commit must be authored AND committed by `Jashan Pratap Singh <88160290+jashanpratapsingh@users.noreply.github.com>`.
  `git cherry-pick` keeps the upstream author, so after EVERY cherry-pick run
  `git commit --amend --reset-author --no-edit`. No `Co-Authored-By` trailers, ever. No mention of any
  other identity. The push/PR hook verifies this over `origin/develop..HEAD`.
- No `pip install` into `.venv` (it is a symlink shared with the main checkout). If a new dependency is
  required, add it to `requirements*.txt`, explain in the PR, set status `needs_human`, and stop.
- `git add` explicit paths only (never `-A`, `--all`, `.`). `.venv`, `.port-env`, `graphify-out/` must never be committed.
- Never run `scripts/build-mac-libopenshot.sh` without `ZENVI_DEPS=<prefix>` (never overwrite `~/zenvi-deps`).
- Never merge PRs, never remove worktrees, never delete branches. Humans do that.
- No claude.ai connectors, no MCP servers, no network calls other than `git`/`gh` to GitHub and documented
  model/dependency downloads that the feature itself needs.
- Keep `AGENTS.md` (repo engineering doctrine) and `docs/PR_FORMAT.md` in mind; read both first.

## Status protocol
`bash scripts/upstream-port/set-status.sh <state> [--pr URL] [--note "..."]` (the scripts live at `$PORT_SCRIPTS`;
use that absolute path if `scripts/upstream-port/` does not exist on your branch yet). States:
`in_progress` -> `testing` -> `pr_open` (draft PR pushed) -> `ready` (tests green, PR marked ready).
`needs_human --note "..."` whenever you are blocked on a product decision, a dependency, a libopenshot API
mismatch, or a failing test unrelated to your feature. Then STOP and wait (end your turn; the human will
answer in your tmux window). `failed --note` only if the feature cannot be ported at all.
Update the note whenever your plan or blocker changes; the human reads it from `status.sh`.

## Procedure
### 0. Orientation (do not skip)
1. `source .port-env` mentally: `PORT_FEATURE`, `PORT_BRANCH`, `PORT_BASE_BRANCH` (PR base: `develop` or a
   `jashan/<dep>` branch you are stacked on), `ZENVI_DEPS` (the libopenshot prefix to test against).
2. Read `AGENTS.md`, `docs/PR_FORMAT.md`, `README.md` (dev setup), `run.sh`.
3. For each upstream merge commit in your brief: `git show --stat <sha>`, `git log --no-merges --reverse --format='%h %s' <sha>^1..<sha>`,
   and the overlap with Zenvi's own changes:
   `comm -12 <(git diff --name-only <sha>^1 <sha> | sort) <(git diff --name-only 8204a244e HEAD | sort) | grep -v -E '^(src/language/|doc/)'`.
4. Grep how Zenvi-only modules touch the same areas (`src/windows/ai_chat_ui.py`, `agent_panel.py`,
   `agent_runners.py`, `plan_dock_ui.py`, `src/classes/tool_handlers.py`, `agent_mcp_server.py`,
   `media_cache.py`, `file_drop.py`, `ai_metadata_utils.py`, `docking.py`, `crash_handler.py`).
5. Write a short plan (order of picks, expected conflicts, extra tasks from the brief) into the status note:
   `set-status.sh in_progress --note "plan: ..."`.

### 1. Port loop — one upstream merge commit at a time, in the order given
Full pick:
```bash
git cherry-pick -m 1 -x <sha>            # conflicts are expected; resolve them (below), then: git cherry-pick --continue
git commit --amend --reset-author --no-edit
```
Hunks-only pick (`mode: hunks` in the brief): do NOT cherry-pick; apply only the listed paths:
```bash
git diff <sha>^1 <sha> -- <paths...> | git apply -3 --index   # fix rejects by hand
git commit -m "port(<feature>): <what> (hunks of OpenShot PR #N, <sha>)"
```
Always, before `--continue`/commit:
- Translation and doc churn is NEVER ported: `git checkout HEAD -- src/language doc 2>/dev/null; git diff --cached --name-only --diff-filter=A | grep -E '^(src/language/|doc/)' | xargs -r git rm -q --cached -f` (then delete those files from the worktree).
  Version bumps in `src/classes/info.py` (`VERSION = ...`) and `supporters.json` are never ported either.
- "deleted by us" conflicts on upstream-only modules Zenvi never had (ComfyUI, proxy, notifications,
  distribution, feedback, version.py, images/openshot.qrc, doc/*): `git rm` them unless your brief says to keep them.
- Binary PNG/SVG icon conflicts: take upstream (`git checkout --theirs -- <file>`).
- Branding: Zenvi is the product. Keep `info.PRODUCT_NAME`/`info.NAME`; never reintroduce user-facing
  "OpenShot" strings (titles, dialogs, menu text, settings labels). URLs, class/module names, `import openshot`,
  license text and comments may keep the word. Check with:
  `git diff HEAD~1 -- src | grep -n '^+.*OpenShot' | grep -v -E 'openshot\.org|import openshot|openshot\.[A-Z]|OPENSHOT_|#'`.
- Qt binding: if the branch has `src/qt_api.py`, new/ported code must import Qt via `from qt_api import ...`
  (convert any `from PyQt5...` the pick brings in). If it does NOT have `qt_api.py` and the pick uses
  `from qt_api import`, convert those imports back to the equivalent `PyQt5` imports for this branch, and
  note it in the PR (the qt-api feature will reconcile).
- Keep Zenvi-only behaviour working: login gate, AI chat/plan docks, agent MCP server, media cache,
  indexing badges, auto-updater, DMG/installer scripts. Where upstream rewrote a function Zenvi also
  changed, re-apply Zenvi's change onto upstream's version; do not drop either side silently.
- Commit message: keep upstream's subject, prefixed `port(<feature>): `; the `-x` line records the source.
After each pick: `.venv/bin/python -m compileall -q src && .venv/bin/python tests/smoke_test.py`.
If a pick becomes unreadable (>30 conflicted files), fall back to picking its individual commits in order
(`git log --no-merges --reverse <sha>^1..<sha>`), still amending the author each time.

### 2. Extra tasks from the brief
Do them as separate, well-described commits (`feat(<feature>): ...` / `fix(<feature>): ...`).

### 3. Test gate — all must pass before a PR is marked ready
```bash
bash $PORT_SCRIPTS/test-all.sh          # compileall, smoke, pytest, ruff, (pyright), legacy real-Qt tests, PyQt5 gate, 45s headless launch
```
Then exercise the feature for real: `bash $PORT_SCRIPTS/run-app.sh --gui --timeout 90 --screenshot $PORT_ROOT/.state/$PORT_FEATURE.png`
launches the app on the desktop with an isolated HOME and your `ZENVI_DEPS`; use it to sanity-check that the
UI you ported appears (the human does the click-through from your PR steps). Fix anything the gate finds.
Set `set-status.sh testing` while doing this. If libopenshot is missing for your feature, run
`bash $PORT_SCRIPTS/ensure-deps.sh $ZENVI_DEPS $PORT_LIBOPENSHOT_TAG $PORT_LIBOPENSHOT_AUDIO_TAG`
(the libopenshot-1.0 agent builds its own prefix with the parametrized script).

### 4. Pull request
1. Write the PR body from `$PORT_SCRIPTS/PR_TEMPLATE.md` into `$PORT_ROOT/.state/$PORT_FEATURE.pr.md`.
   Follow `docs/PR_FORMAT.md`: lead with what the reviewer will notice; ordered manual steps, each with a
   **Pass:** line; copy-pasteable automated commands; explicit "Out of scope". Fill the upstream table with
   every pick. State the libopenshot requirement and the stack base.
2. `git push -u origin $PORT_BRANCH`
3. `gh pr create --draft --base $PORT_BASE_BRANCH --head $PORT_BRANCH --title "port: <short title> (OpenShot #NNNN, ...)" --body-file $PORT_ROOT/.state/$PORT_FEATURE.pr.md`
   then `set-status.sh pr_open --pr <url>`.
4. Confirm `gh pr view --json author -q .author.login` prints `jashanpratapsingh`.
5. When the test gate is green on the pushed HEAD: `gh pr ready <url>` and `set-status.sh ready --pr <url>`.
6. Stay in the session. If the human asks for changes, iterate: fix, re-run the gate, push, update the PR body.
   If `develop` moves and your branch conflicts, rebase onto `origin/develop` (or your stack base) and
   `git push --force-with-lease origin $PORT_BRANCH`.

## Judgement
- Prefer fidelity to upstream behaviour; adapt only where Zenvi differs (branding, its extra docks, its cache,
  its updater, its installers). Say what you adapted and why in the PR.
- Do not silently drop upstream functionality to make a conflict go away; if you must, list it under
  "Out of scope" with the reason.
- If you are unsure whether a product behaviour is wanted (e.g. a new default, a menu placement), pick the
  conservative option, keep upstream's kill switch, and flag it in the PR and in a `needs_human` note only
  if it blocks you.
- Write for the reviewer: short sentences, no file dumps, and manual steps a non-developer can follow.
