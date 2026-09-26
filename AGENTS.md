# Zenvi Core — Agent Doctrine

Rules for humans and coding agents working in this repo. Keep this file true;
cut anything that becomes aspirational.

## Stack

- Python / PyQt5 / libopenshot video editor (Windows, Linux, macOS).
- **Python 3.10 minimum.** CI and `.python-version` pin **3.12**.
- App entry: `src/launch.py`. Domain code lives under `src/classes/`.
- Packaging: `setup.py` + `freeze.py`. Do not add a `[project]` table in
  `pyproject.toml` without checking both; use standalone config files
  (`pytest.ini`, `ruff.toml`, `pyrightconfig.json`) for tooling.

## Concurrency and the Qt main thread

- The Qt GUI thread is scarce. Do not do file I/O, network waits, media
  decode, JSON of large projects, or long loops on it.
- `QThread.currentThread()` checks in tool handlers exist for a reason:
  prefer `_run_on_main_thread` only for the minimum Qt/libopenshot touch,
  and do everything else off-thread.
- A bare `threading.Thread` or `QTimer.singleShot` is not proof work left
  the GUI thread. Be explicit about which executor owns the work.

## File I/O and project data

- Treat every volume as potentially slow. Existence checks, reads, writes,
  and directory walks belong off the GUI thread.
- Project mutations go through `UpdateManager` / `UpdateAction`. Do not
  mutate `project._data` behind the update system's back.
- Stage complete outputs, then install. Do not leave half-written project
  or media files as the live destination.

## Undo

- One user intent = one undoable transaction. Failed, cancelled, refused,
  and no-op operations must not create empty undo steps.
- In-memory undo (`actionHistory`) is unbounded for the live session.
- `history-limit` (default 50) only caps the slice serialized into the
  project file on save. Changing it changes reopen-undo depth and file size,
  not live-session undo.

## Agent tools

- Tools live in `src/classes/tool_handlers.py` (`AGENT_TOOL_HANDLERS`) and
  are also exposed over the in-app MCP server (`agent_mcp_server.py`).
- Design tools from user intent, not from internal method boundaries.
- Prefer explicit typed arguments over `**kwargs`. Return structured,
  machine-readable results when mutating state (receipts), not prose-only
  strings, once that contract exists.
- Validate before opening an undo group. A failed tool call must leave
  history untouched.
- `agent_gap_log.py` records capabilities users ask for that we lack.
  Prefer implementing a logged gap over inventing a speculative tool.

## Tests

- Headless unit suite: `tests/`. `tests/conftest.py` stubs PyQt5 and
  `openshot` by default so the suite runs without Qt or libopenshot.
  ```bash
  pip install -r requirements-dev.txt
  pytest
  ```
- Real-Qt tests: set `ZENVI_REAL_QT=1`. Files that call
  `pytest.importorskip("PyQt5...")` are auto-ignored under the stub.
- Legacy / integration scripts: `src/tests/` (real Qt + openshot). The
  existing `build` CI job still runs these.
- CI job `unit-tests` runs `pytest tests/`, `ruff check src/classes/`, and
  `pyright` (allowlisted files only).
- Every bug fix should add or extend a regression test when practical.
- Tests run in parallel by default. No shared mutable temp paths, ports,
  or fixed filenames across tests.

## Lint and types

- `ruff.toml` — conservative rules on `src/classes/`. Per-file ignores are
  a Phase 0 baseline; burn them down, do not grow them casually.
- `pyrightconfig.json` — `basic` mode on an explicit allowlist. Expand the
  allowlist file by file when a module is clean.

## What not to do

- Do not block the Qt main thread for I/O, network, or heavy work.
- Do not silently swallow exceptions at interaction boundaries
  (`except Exception: return None`) when failure changes the outcome.
- Do not add speculative abstractions, compatibility shims for dropped
  platforms, or second implementations of the same domain rule.
- Do not claim UI or end-to-end verification without a manual check list
  for a human.

## Pull requests

Write PRs so another person can test without reading the diff first.
Follow [docs/PR_FORMAT.md](docs/PR_FORMAT.md). GitHub fills
`.github/PULL_REQUEST_TEMPLATE.md` when you open a PR.
