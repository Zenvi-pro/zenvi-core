#!/usr/bin/env bash
# Run OpenShot Video Editor from the repo root.
# - Linux: requires .venv + system libopenshot (see SETUP.md).
# - macOS: auto-bootstraps Homebrew deps, .venv, and a native libopenshot
#   build via scripts/build-mac-libopenshot.sh, then launches from source.

set -e
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

if [[ "$(uname)" == "Darwin" ]]; then
  # macOS native dev flow.
  # libopenshot has no Homebrew bottle, and its compile-time Qt collides with
  # PyQt5's bundled Qt at runtime (two QApplication singletons → segfault).
  # scripts/build-mac-libopenshot.sh builds it from source and rewrites its
  # Qt rpaths to share PyQt5's wheel Qt; the block below just bootstraps that
  # script's prerequisites and skips rebuilds when artifacts already exist.

  ZENVI_DEPS="${ZENVI_DEPS:-$HOME/zenvi-deps}"

  if ! command -v brew >/dev/null 2>&1; then
    cat <<'MSG'
Homebrew was not found, but zenvi-core's macOS native dev flow depends on it
for python@3.11, qt@5, ffmpeg, and the rest of libopenshot's build deps.

To install Homebrew manually:

  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

See https://brew.sh for details.
MSG

    if [[ -t 0 && -t 1 ]]; then
      read -r -p "Install Homebrew now? You will be prompted for your sudo password. [y/N] " _brew_ans
      if [[ "$_brew_ans" =~ ^[Yy] ]]; then
        if ! /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; then
          echo "Homebrew installer exited non-zero. Install it manually, then re-run ./run.sh."
          exit 1
        fi
        # Installer prints "Next steps" telling the user to add brew to PATH;
        # do that for this shell so the rest of the script can find it.
        if [[ -x /opt/homebrew/bin/brew ]]; then
          eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [[ -x /usr/local/bin/brew ]]; then
          eval "$(/usr/local/bin/brew shellenv)"
        fi
      fi
    fi

    if ! command -v brew >/dev/null 2>&1; then
      echo "Homebrew is still not on PATH. Install it, then re-run ./run.sh."
      exit 1
    fi
  fi

  # python@3.11 is the version the build script and PyQt5 wheel paths assume.
  # `brew --prefix` returns the install path even when uninstalled, so test the
  # interpreter binary directly to decide whether to invoke `brew install`.
  PY311="$(brew --prefix python@3.11 2>/dev/null || true)/bin/python3.11"
  if [[ ! -x "$PY311" ]]; then
    echo "Installing python@3.11 via Homebrew..."
    brew install python@3.11
    PY311="$(brew --prefix python@3.11)/bin/python3.11"
  fi
  if [[ ! -x "$PY311" ]]; then
    echo "ERROR: brew install python@3.11 did not produce a usable interpreter at $PY311"
    exit 1
  fi

  if [[ ! -x .venv/bin/python3 ]]; then
    echo "Creating .venv with $PY311 ..."
    # No --system-site-packages on Mac: PyQt5 must come from the wheel so that
    # build-mac-libopenshot.sh can rewrite libopenshot's Qt deps against it.
    "$PY311" -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
  fi

  if [[ ! -f "$ZENVI_DEPS/python/_openshot.so" ]]; then
    echo "libopenshot not found at $ZENVI_DEPS; building from source (~10 min, brews extra deps)..."
    ZENVI_DEPS="$ZENVI_DEPS" bash scripts/build-mac-libopenshot.sh
  fi

  # Runtime env for from-source Mac runs (mirrors build-mac-libopenshot.sh footer).
  export ZENVI_OPENSHOT_INSTALL="$ZENVI_DEPS"
  export PYTHONPATH="$ZENVI_DEPS/python${PYTHONPATH:+:$PYTHONPATH}"
  export QT_MAC_WANTS_LAYER=1
  export QTWEBENGINE_DISABLE_SANDBOX=1

  if [[ -n "${OPENSHOT_HEADLESS:-}" ]]; then
    export QT_QPA_PLATFORM=offscreen
  fi

  exec .venv/bin/python3 src/launch.py "$@"
fi

# Linux flow.
if [[ ! -d .venv ]]; then
  echo "No .venv found. Run: python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

# Fail fast with a clear message if libopenshot is not available
if ! .venv/bin/python3 -c "import openshot" 2>/dev/null; then
  echo "The 'openshot' module (libopenshot) is not installed."
  echo "Install it, then run again. Examples:"
  echo "  Ubuntu: sudo add-apt-repository ppa:openshot.developers/ppa && sudo apt update && sudo apt install python3-openshot"
  echo "  Check:  .venv/bin/python3 scripts/check_setup.py"
  exit 1
fi

# Optional: for headless / CI use offscreen. Omit for normal GUI (X11/Wayland).
if [[ -n "${OPENSHOT_HEADLESS:-}" ]]; then
  export QT_QPA_PLATFORM=offscreen
fi

exec .venv/bin/python3 src/launch.py "$@"
