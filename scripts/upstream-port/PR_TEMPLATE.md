What this is

<!-- 2-4 short paragraphs in plain language: which OpenShot 4.0 capability this brings to Zenvi, what a user
     will notice, and what this PR is NOT (name the sibling port PRs). -->

Ported from upstream OpenShot

| Upstream PR | Merge commit | Title | Notes |
|---|---|---|---|
| OpenShot/openshot-qt#NNNN | `sha` | title | full pick / hunks only: paths |

Requires libopenshot: <0.5.0 is fine | 1.0.0 (build with `ZENVI_DEPS=~/zenvi-deps-1.0 LIBOPENSHOT_TAG=v1.0.0 LIBOPENSHOT_AUDIO_TAG=v1.0.0 bash scripts/build-mac-libopenshot.sh`)>
Stacked on: <none — targets develop | `jashan/<dep>` (PR #N); retarget to develop after it merges with `scripts/upstream-port/retarget.sh <feature>`>

Platforms

<!-- Mac / Windows / Linux. Where new prefs or menus live on Mac vs Win/Linux. -->

What we did, step by step

<!-- Grouped bullets: what the upstream code does, what was adapted for Zenvi (branding via info.PRODUCT_NAME,
     Zenvi-only modules touched, conflicts and how they were resolved, translation churn dropped), defaults and
     kill switches. -->

How to test

Automated (from the repo root, inside this branch's checkout):

```bash
.venv/bin/python tests/smoke_test.py
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check src/classes/
OPENSHOT_HEADLESS=1 ./run.sh        # must stay up; Ctrl-C to stop
```

Manual (do these in order) — run the app with `./run.sh` (use `ZENVI_DEPS=~/zenvi-deps-1.0 ./run.sh` if this PR needs libopenshot 1.0):

1. <step>
   - Pass: <what you must see>
2. <step>
   - Pass: <what you must see>

Regression checks

1. Launch, open an existing `.zvn` project, play, save.
   - Pass: no console tracebacks; project reopens.
2. Zenvi assistant: ask it to add a clip and split it at the playhead.
   - Pass: both actions land on the timeline and are undoable.

Out of scope (not in this PR)

<!-- sibling port PRs, follow-ups (translations, docs), upstream features deliberately skipped -->
