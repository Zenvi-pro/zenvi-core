# run-win.ps1
# Single-file Windows setup for Zenvi Core.
#
# 1. Installs MSYS2 (via winget) if it isn't already present.
# 2. Generates the MSYS2/build logic as a temp bash script and runs it inside
#    the MSYS2 MinGW64 shell (pacman + compiling libopenshot/-audio needs a
#    real MSYS2 environment - PowerShell alone cannot do this part).
# 3. Sets up the Python venv, installs requirements-noqt.txt, and launches.
#
# Usage (from a normal PowerShell prompt, in the zenvi-core repo root):
#   .\run-win.ps1

$ErrorActionPreference = "Stop"

# Was hardcoded to C:\msys64 in 3 places -- now detected, since winget/manual
# installs don't always land there. Checks (in order): $env:MSYS2_ROOT, the
# registry uninstall entry MSYS2's installer writes, then falls back to the
# documented default.
function Find-Msys2Root {
    if ($env:MSYS2_ROOT -and (Test-Path (Join-Path $env:MSYS2_ROOT "usr\bin\bash.exe"))) {
        return $env:MSYS2_ROOT
    }

    $uninstallGlobs = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )
    foreach ($glob in $uninstallGlobs) {
        $entry = Get-ItemProperty -Path $glob -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -like "MSYS2*" -and $_.InstallLocation } |
            Select-Object -First 1
        if ($entry -and (Test-Path (Join-Path $entry.InstallLocation "usr\bin\bash.exe"))) {
            return $entry.InstallLocation
        }
    }

    return "C:\msys64"
}

$msys2Root = Find-Msys2Root
$msys2Bash = Join-Path $msys2Root "usr\bin\bash.exe"

function Test-Msys2Installed {
    return Test-Path $msys2Bash
}

if (-not (Test-Msys2Installed)) {
    Write-Host "MSYS2 not found at $msys2Root - installing via winget..."

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "winget is not available on this machine."
        Write-Host "Please install MSYS2 manually from https://www.msys2.org/ and re-run this script."
        exit 1
    }

    winget install -e --id MSYS2.MSYS2 --accept-source-agreements --accept-package-agreements

    if (-not (Test-Msys2Installed)) {
        Write-Host "MSYS2 install did not land at the expected path ($msys2Root)."
        Write-Host "If you installed it elsewhere, set an MSYS2_ROOT environment variable to that path and re-run."
        exit 1
    }

    Write-Host "MSYS2 installed."
} else {
    Write-Host "MSYS2 detected at $msys2Root."
}

$repoRoot = $PSScriptRoot

# --- The bash side of the setup, written to a temp file and run inside MSYS2 MinGW64 ---
# (First run: expect 1hr+, mostly compiling libopenshot/libopenshot-audio from source.
#  Every step below is skipped on reruns if already done.)
$bashScript = @'
set -e
cd "$REPO_ROOT_UNIX"

DEPS_DIR="${ZENVI_WIN_DEPS:-$HOME/zenvi-win-deps}"
mkdir -p "$DEPS_DIR"

echo "=== Zenvi Core Windows (MSYS2) Setup ==="
echo "Dependency build directory: $DEPS_DIR"

echo ""
echo "--- [1/6] Syncing MSYS2 package databases and installing packages ---"
# Deliberately NOT running a full `pacman -Syu` here. That was causing two
# separate reported failures:
#  - Fresh MSYS2 install: -Syu upgrades msys2-runtime itself, which force-closes
#    this very shell mid-script (documented MSYS2 behavior) -- script exits
#    before step 1 finishes.
#  - Existing MSYS2 install: -Syu does a full system upgrade (e.g. FFmpeg 8->9,
#    gcc 15->16 -- 351 packages in one report), which can break an
#    already-built libopenshot, and has hit leftover-file conflicts in
#    unrelated environments (e.g. ucrt64) this repo never touches.
# `pacman -Sy` only refreshes the package database (no runtime upgrade, no
# shell close); `-S --needed` then installs/updates only the packages below
# to their currently-synced versions and leaves everything else alone.
pacman -Sy --noconfirm
pacman -S --needed --noconfirm --disable-download-timeout \
    mingw-w64-x86_64-python-cryptography \
    mingw-w64-x86_64-python-rpds-py \
    base-devel git \
    mingw-w64-x86_64-toolchain \
    mingw64/mingw-w64-x86_64-ffmpeg \
    mingw64/mingw-w64-x86_64-swig \
    mingw64/mingw-w64-x86_64-cmake \
    mingw64/mingw-w64-x86_64-doxygen \
    mingw64/mingw-w64-x86_64-zeromq \
    mingw64/mingw-w64-x86_64-python-pyqt5 \
    mingw64/mingw-w64-x86_64-python-pip \
    mingw64/mingw-w64-x86_64-python-pyzmq \
    mingw64/mingw-w64-x86_64-rust \
    mingw64/mingw-w64-x86_64-qt5-svg \
    mingw64/mingw-w64-x86_64-cppzmq \
    mingw-w64-x86_64-python-cffi \
    mingw-w64-x86_64-python-zstandard \
    mingw-w64-x86_64-qtwebkit \
    mingw-w64-x86_64-libffi \
    mingw-w64-x86_64-gcc

pip3 install --break-system-packages httplib2 tinys3 github3.py==0.9.6 requests

echo ""
echo "--- [2/6] unittest-cpp ---"
if [ ! -f /usr/lib/libUnitTest++.a ]; then
    cd "$DEPS_DIR"
    [ -d unittest-cpp ] || git clone https://github.com/unittest-cpp/unittest-cpp.git
    cd unittest-cpp
    mkdir -p builds && cd builds
    cmake -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make \
        -DCMAKE_INSTALL_PREFIX:PATH=/usr -DCMAKE_POLICY_VERSION_MINIMUM=3.5 ../
    make
    make install
    echo "unittest-cpp built and installed."
else
    echo "unittest-cpp already installed -- skipping."
fi
export UNITTEST_DIR="${MSYS2_ROOT_WIN}\usr"

echo ""
echo "--- [3/6] libopenshot-audio ---"
# Was checking for a file literally named 'libopenshot-audio*' directly under
# /usr (maxdepth 1) -- that never exists, since the real installed artifacts
# are the headers under /usr/include/libopenshot-audio/ and the lib under
# /usr/lib/. That mismatch meant this check never matched, so the build (and
# `make install`, silently overwriting any existing install) ran every time.
if ! find /usr/include /usr/lib -maxdepth 2 -iname '*openshot-audio*' 2>/dev/null | grep -q .; then
    cd "$DEPS_DIR"
    [ -d libopenshot-audio ] || git clone https://github.com/OpenShot/libopenshot-audio.git
    cd libopenshot-audio

    rm -rf build
    mkdir -p build && cd build
    # JUCE_ASIO=0: skip requiring the Windows SDK / Steinberg ASIO SDK, same as CI.
    # Drop this flag (and install those SDKs) if you need real ASIO hardware audio.
    cmake -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make \
        -DCMAKE_INSTALL_PREFIX:PATH=/usr \
        -DJUCE_ASIO=0 ../
    cd ..

    # JUCE hardcodes "#define JUCE_ASIO 1" in AppConfig.h regardless of the
    # -DJUCE_ASIO=0 cmake flag, so patch it directly (same fix CI applies).
    # Patched AFTER configure since some JUCE cmake setups (re)generate this
    # file during the configure step, which would silently undo an earlier patch.
    APPCONFIGS="$(find . -iname 'AppConfig.h')"
    if [ -n "$APPCONFIGS" ]; then
        for APPCONFIG in $APPCONFIGS; do
            sed -i -E 's/#define[[:space:]]+JUCE_ASIO[[:space:]]+1/#define JUCE_ASIO 0/' "$APPCONFIG"
            echo "Patched $APPCONFIG:"
            grep -n "JUCE_ASIO" "$APPCONFIG" || true
        done
    else
        echo "WARNING: AppConfig.h not found anywhere under libopenshot-audio -- ASIO build errors may occur."
    fi

    cd build
    make
    if find /usr/include /usr/lib -maxdepth 2 -iname '*openshot-audio*' 2>/dev/null | grep -q .; then
        echo "NOTE: an existing libopenshot-audio install was found under /usr -- 'make install' will overwrite it now."
    fi
    make install
    echo "libopenshot-audio built and installed."
else
    echo "libopenshot-audio already installed -- skipping."
fi
export LIBOPENSHOT_AUDIO_DIR="${MSYS2_ROOT_WIN}\usr"

echo ""
echo "--- [4/6] Extra libopenshot deps ---"
pacman -S --needed --noconfirm mingw64/mingw-w64-x86_64-qt5-svg mingw64/mingw-w64-x86_64-cppzmq

echo ""
echo "--- [5/6] libopenshot ---"
LIBOPENSHOT_SRC="$DEPS_DIR/libopenshot"
BINDINGS_DIR="$LIBOPENSHOT_SRC/build/bindings/python"
# Check for the actual compiled module, not just the directory -- the
# directory can exist (with only CMake's own build files in it) even when
# the bindings subdirectory was never actually built.
if ! find "$BINDINGS_DIR" -iname 'openshot.py' 2>/dev/null | grep -q .; then
    cd "$DEPS_DIR"
    [ -d libopenshot ] || git clone https://github.com/OpenShot/libopenshot.git
    cd libopenshot

    # FFmpeg 7+ compat patch (OpenShot/libopenshot PR #1088): MSYS2 ships a
    # newer FFmpeg (8.x/9.x) that removed pix_fmts/sample_fmts/ch_layouts/
    # supported_samplerates from the public AVCodec struct, which breaks the
    # build otherwise. Fetched and applied automatically, and skipped cleanly
    # if it's already applied (e.g. on a rerun, or once this lands upstream).
    PATCH_URL="https://patch-diff.githubusercontent.com/raw/OpenShot/libopenshot/pull/1088.patch"
    PATCH_FILE="$DEPS_DIR/ffmpeg7-compat.patch"
    if curl -fsSL "$PATCH_URL" -o "$PATCH_FILE"; then
        if git apply --check "$PATCH_FILE" 2>/dev/null; then
            git apply "$PATCH_FILE"
            echo "Applied FFmpeg 7+ compatibility patch (libopenshot PR #1088)."
        else
            echo "FFmpeg 7+ compatibility patch already applied or not applicable -- skipping."
        fi
    else
        echo "WARNING: could not download the FFmpeg 7+ compatibility patch -- build may fail on newer FFmpeg."
    fi

    rm -rf build
    mkdir -p build && cd build
    cmake -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make \
        -DCMAKE_INSTALL_PREFIX:PATH=/mingw64 \
        -DDISABLE_TESTS=1 \
        -DCMAKE_CXX_FLAGS="-include cstdint" ../
    make
    make install

    # The Python bindings subdirectory isn't always part of the top-level
    # 'all' target, so build it explicitly to make sure it actually happens.
    if [ -d bindings/python ]; then
        echo "--- Building Python bindings (bindings/python) explicitly ---"
        (cd bindings/python && make)
    else
        echo "WARNING: bindings/python subdirectory not found under build/ -- Python bindings were not configured."
    fi

    echo "libopenshot built and installed."
else
    echo "libopenshot (including Python bindings) already built -- skipping."
fi

echo ""
echo "--- [6/6] Zenvi Core Python environment ---"
cd "$REPO_ROOT_UNIX"
pacman -S --needed --noconfirm \
    mingw-w64-x86_64-python-pyqt5 \
    mingw-w64-x86_64-python-cffi \
    mingw-w64-x86_64-python-zstandard \
    mingw-w64-x86_64-qtwebkit \
    mingw-w64-x86_64-libffi \
    mingw-w64-x86_64-gcc

if [ ! -d .venv ]; then
    /mingw64/bin/python.exe -m venv --system-site-packages .venv
    echo "Created .venv"
else
    echo ".venv already exists -- skipping creation."
fi

source .venv/bin/activate

# cryptography and rpds-py have Rust extension modules that fail to compile
# from source under MSYS2's Python via pip. Both are already installed above
# via pacman (mingw-w64-x86_64-python-cryptography / -rpds-py), and this venv
# was created with --system-site-packages so it can see them -- strip the two
# lines from the requirements file before installing the rest via pip so pip
# doesn't try to build (or reinstall) them itself.
REQS_FILTERED="$DEPS_DIR/requirements-noqt.filtered.txt"
grep -viE '^(cryptography|rpds-py)([=<>~[:space:]]|$)' requirements-noqt.txt > "$REQS_FILTERED"
pip install -r "$REQS_FILTERED"
# pip install -r requirements-manim.txt   # uncomment if your build needs it

echo ""
echo "=== Setup complete. Launching Zenvi Core... ==="

BINDINGS_DIR="$LIBOPENSHOT_SRC/build/bindings/python"
echo "libopenshot Python bindings expected at: $BINDINGS_DIR"
echo "Contents:"
ls -la "$BINDINGS_DIR" 2>&1 || echo "  (directory not found)"

# Set both: PYTHONPATH_LIBOPENSHOT in case run-zenvi-core.sh reads it itself,
# and the real PYTHONPATH directly so the launched Python process finds the
# 'openshot' module regardless of what run-zenvi-core.sh does internally.
export PYTHONPATH_LIBOPENSHOT="$BINDINGS_DIR"
export PYTHONPATH="$BINDINGS_DIR${PYTHONPATH:+:$PYTHONPATH}"

if [ -f run-zenvi-core.sh ]; then
    bash run-zenvi-core.sh
else
    echo "run-zenvi-core.sh not found -- launching src/launch.py directly."
    python3 src/launch.py
fi
'@

$tempScriptWin  = Join-Path $env:TEMP "zenvi-run-win-setup.sh"
$bashScript | Set-Content -Path $tempScriptWin -NoNewline -Encoding ASCII

# Convert Windows paths to MSYS2-style Unix paths for use inside bash
$repoRootUnix = "/" + $repoRoot.Replace(":", "").Replace("\", "/")
$tempScriptUnix = "/" + $tempScriptWin.Replace(":", "").Replace("\", "/")

Write-Host "=== Handing off to MSYS2 MinGW64 shell ==="

$env:MSYSTEM        = "MINGW64"
$env:CHERE_INVOKING = "1"

& $msys2Bash -lc "export REPO_ROOT_UNIX='$repoRootUnix'; export MSYS2_ROOT_WIN='$msys2Root'; bash '$tempScriptUnix'"
$exitCode = $LASTEXITCODE

Remove-Item -Path $tempScriptWin -ErrorAction SilentlyContinue

if ($exitCode -ne 0) {
    Write-Host "Setup/launch exited with an error (code $exitCode)."
    exit $exitCode
}
