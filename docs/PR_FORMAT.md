# How we write pull requests

PRs should be easy for someone else to test without reading the diff first.
Use the shape below. GitHub also loads a blank version from
[`.github/PULL_REQUEST_TEMPLATE.md`](../.github/PULL_REQUEST_TEMPLATE.md)
when you open a PR.

## Required sections

1. **What this is** — short human summary. Say what changed and what this is *not*.
2. **Platforms** — Mac / Windows / Linux, and where any new prefs live.
3. **What we did, step by step** — grouped bullets, defaults, kill switches, caveats.
4. **How to test** — automated commands + ordered manual steps with a **Pass:** line.
5. **Out of scope** — related work that is *not* in this PR.

## Rules

- Lead with what the reviewer will notice, not file names.
- Prefer `./run.sh` as the launch command unless the PR needs something else.
- Manual steps are ordered. Each has a clear Pass line.
- Automated commands must be copy-pasteable from the repo root.
- Do not paste huge logs or full commit histories into the body.

## Example shape (from a real PR)

```
What this is

Makes Zenvi video export faster without changing the normal Export dialog flow.
No new screens — same File → Export Video. New kill switches live under
Zenvi → Preferences → Performance (macOS app menu; not under Edit).

This is not the DMG installer PR, and not the mac export/GUI-thread work on
other branches.

Platforms

Mac, Windows, and Linux. …

What we did, step by step

Safety net (Phase 0)
…

Hardware decode
…

How to test

Automated:
.venv/bin/python -m pytest tests/test_export_acceleration.py -q

Manual (do these in order) — run the app with ./run.sh:
1. Confirm Preferences toggles
   Pass: …
2. Normal export (main)
   Pass: file plays, audio syncs, no crash.

Out of scope (not in this PR)

GPU compositing in libopenshot (C++ work)
DMG installer branding
…
```
