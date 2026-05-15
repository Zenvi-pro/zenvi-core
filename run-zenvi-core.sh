#!/usr/bin/env bash
# Launch zenvi-core with a minimal environment (avoids Linux Snap/Qt conflicts).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

resolve_venv_python() {
  local c
  for c in "$VENV_DIR/bin/python3" "$VENV_DIR/bin/python" "$VENV_DIR/Scripts/python.exe"; do
    if [[ -x "$c" ]]; then
      printf '%s' "$c"
      return 0
    fi
  done
  return 1
}

PYTHON_BIN="$(resolve_venv_python)" || {
  echo "Missing venv interpreter under $VENV_DIR (tried bin/python3, bin/python, Scripts/python.exe)"
  echo "Create it (example): python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
}

# PyQt wheels in the venv often clash with libopenshot linked to system Qt.
if compgen -G "$VENV_DIR/lib/python*/site-packages/PyQt5" >/dev/null 2>&1 ||
   compgen -G "$VENV_DIR/lib/python*/site-packages/PyQtWebEngine*" >/dev/null 2>&1; then
  if [[ -n "${PYTHONPATH_LIBOPENSHOT:-}" && -z "${ZENVI_ALLOW_VENV_QT:-}" ]]; then
    echo "PyQt5 / PyQtWebEngine inside .venv conflicts with typical libopenshot + system Qt setups."
    echo "Linux: use distro PyQt5 packages, then pip uninstall those wheels and use requirements-noqt.txt"
    echo "Windows/MSYS: use MSYS Python + mingw-w64-python-pyqt5 packages where possible."
    echo "Override: ZENVI_ALLOW_VENV_QT=1"
    exit 1
  fi
fi

HOST_PYTHONPATH="${PYTHONPATH:-}"
HOST_LIB="${PYTHONPATH_LIBOPENSHOT:-}"
COMBINED_PYTHONPATH="$HOST_PYTHONPATH"
if [[ -n "$HOST_LIB" ]]; then
  COMBINED_PYTHONPATH="${HOST_LIB}${COMBINED_PYTHONPATH:+:}${COMBINED_PYTHONPATH}"
fi

OS="$(uname -s 2>/dev/null || true)"
if [[ "$OS" == Linux* ]]; then
  # Strip host env; fixed FHS PATH avoids Snap-injected broken Qt paths.
  RUN_PATH="$VENV_DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
else
  # MSYS2 / Git Bash: keep existing PATH so MinGW DLLs and tools resolve.
  RUN_PATH="$VENV_DIR/bin:${PATH:-/usr/bin:/bin}"
fi

ENV_ARGS=(
  "HOME=$HOME"
  "USER=${USER:-}"
  "LOGNAME=${LOGNAME:-${USER:-}}"
  "LANG=${LANG:-C.UTF-8}"
  "LC_ALL=${LC_ALL:-}"
  "PATH=$RUN_PATH"
  "VIRTUAL_ENV=$VENV_DIR"
  "PYTHONNOUSERSITE=1"
  "SHELL=${SHELL:-/bin/bash}"
  "TERM=${TERM:-xterm-256color}"
  "DISPLAY=${DISPLAY:-}"
  "DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-}"
  "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-}"
  "PYTHONPATH=$COMBINED_PYTHONPATH"
  "PYTHONPATH_LIBOPENSHOT=$HOST_LIB"
)

# Linux only: help the dynamic linker find libopenshot built next to the bindings tree.
if [[ "$OS" == Linux* && -n "$HOST_LIB" && -d "$HOST_LIB" ]]; then
  lib_build="$(cd "$(dirname "$HOST_LIB")/.." && pwd)"
  if [[ -d "$lib_build/src" ]]; then
    ENV_ARGS+=("LD_LIBRARY_PATH=$lib_build/src")
  fi
fi

exec env -i "${ENV_ARGS[@]}" "$PYTHON_BIN" "$SCRIPT_DIR/src/launch.py" "$@"
