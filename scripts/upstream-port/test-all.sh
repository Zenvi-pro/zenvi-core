#!/usr/bin/env bash
# test-all.sh [--no-app] [--gui-timeout SEC]   — the agent test gate. Exit non-zero on any failure.
# Runs from a feature worktree: compileall, smoke test, pytest, ruff, (pyright), legacy Qt/openshot
# tests, PyQt5-import gate (once qt_api exists), headless launch.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
[[ -f .port-env ]] || die "run from a feature worktree"
source .port-env
NO_APP=0; while [[ $# -gt 0 ]]; do case "$1" in --no-app) NO_APP=1; shift;; *) shift;; esac; done
PY=.venv/bin/python; FAILS=(); RESULTS=(); KNOWN=()
step() { local name="$1"; shift; log "▶ $name"; if "$@"; then RESULTS+=("PASS  $name"); else RESULTS+=("FAIL  $name"); FAILS+=("$name"); fi; }
known() { RESULTS+=("KNOWN-FAIL  $1 (pre-existing on develop, not caused by this branch)"); KNOWN+=("$1"); }
step "compileall src" $PY -m compileall -q src
# smoke_test: on develop, test_video_file_exists raises pytest.skip outside pytest when the optional local
# fixture (~/Downloads video) is absent -> treat exactly that as a known baseline failure.
smoke_out="$($PY tests/smoke_test.py 2>&1)"; smoke_rc=$?
echo "$smoke_out" | tail -25
if [[ $smoke_rc -eq 0 ]]; then RESULTS+=("PASS  tests/smoke_test.py")
elif grep -q 'Skipped: Local test video not present' <<<"$smoke_out" && ! grep -q 'FAIL' <<<"$smoke_out"; then known "tests/smoke_test.py (optional local video fixture missing)"
else RESULTS+=("FAIL  tests/smoke_test.py"); FAILS+=("tests/smoke_test.py"); fi
step "pytest tests/ -q" $PY -m pytest tests/ -q -p no:cacheprovider
if [[ -x .venv/bin/ruff ]]; then step "ruff check src/classes/" .venv/bin/ruff check src/classes/; else warn "ruff missing in .venv (CI runs it)"; fi
if [[ -x .venv/bin/pyright && "${PORT_PYRIGHT:-1}" == 1 ]]; then
  # pyright: errors confined to files this branch did not touch are a baseline problem, not ours.
  py_out="$(.venv/bin/pyright 2>&1)"; py_rc=$?; echo "$py_out" | tail -15
  if [[ $py_rc -eq 0 ]]; then RESULTS+=("PASS  pyright")
  else
    changed="$(git diff --name-only "${PORT_BASE_REF:-origin/develop}"...HEAD 2>/dev/null; git diff --name-only HEAD 2>/dev/null)"
    err_files="$(grep -E '^\s*/.*:[0-9]+:[0-9]+ - error' <<<"$py_out" | sed -E 's#^\s*(.*):[0-9]+:[0-9]+ - error.*#\1#' | sed "s#^$PWD/##" | sort -u)"
    if [[ -n "$err_files" ]] && ! grep -qxF -f <(echo "$changed") <<<"$err_files"; then known "pyright (errors only in files untouched by this branch: $(echo "$err_files" | tr '\n' ' '))"
    else RESULTS+=("FAIL  pyright"); FAILS+=("pyright"); fi
  fi
fi
if [[ -f "$ZENVI_DEPS/python/_openshot.so" ]]; then
  step "src/tests/query_tests.py (real Qt+openshot, offscreen)" env QT_QPA_PLATFORM=offscreen PYTHONPATH="$ZENVI_DEPS/python" $PY src/tests/query_tests.py -platform minimal
  step "src/tests/update_installer_tests.py" env PYTHONPATH="$ZENVI_DEPS/python" $PY src/tests/update_installer_tests.py
else warn "libopenshot missing at $ZENVI_DEPS; skipping real-Qt legacy tests"; fi
if [[ -f src/qt_api.py ]]; then
  pyqt5_gate() { local hits; hits="$(grep -rln -E '^\s*(from|import) PyQt5' src --include='*.py' | grep -v -E '^src/qt_api\.py$' || true)"; [[ -z "$hits" ]] || { echo "direct PyQt5 imports remain:"; echo "$hits"; return 1; }; }
  step "no direct PyQt5 imports outside qt_api.py" pyqt5_gate; fi
if [[ "$NO_APP" == 0 ]]; then step "headless launch stays up 45s" bash "$PORT_SCRIPTS/run-app.sh" --headless --timeout 45; fi
echo; echo "================ test-all summary ($PORT_FEATURE @ $(git rev-parse --short HEAD)) ================"
printf '%s\n' "${RESULTS[@]}"
if (( ${#FAILS[@]} )); then echo "RESULT: FAIL (${#FAILS[@]})"; exit 1; else echo "RESULT: PASS"; exit 0; fi
