#!/usr/bin/env bash
# MSYS2 UCRT64 packages for the Windows build: what libopenshot and the frozen app
# are built from. Shared by the release job and the Windows libopenshot check
# (.github/workflows/windows-libopenshot.yml), so a package change is tested
# before a release tag runs it.
#
#   harden   pacman download timeouts, skip the stalled OSUOSL mirror
#   install  update and install the packages (retries slow mirrors)
set -euo pipefail

harden() {
  grep -q '^DisableDownloadTimeout' /etc/pacman.conf \
    || sed -i '/^\[options\]/a DisableDownloadTimeout' /etc/pacman.conf
  for f in /etc/pacman.d/mirrorlist.*; do
    [[ -f "$f" ]] || continue
    sed -i 's|^Server = https://ftp2.osuosl.org|# &|' "$f"
  done
}

install() {
  pkgs=(
    base-devel
    git
    curl
    mingw-w64-ucrt-x86_64-toolchain
    mingw-w64-ucrt-x86_64-ffmpeg
    mingw-w64-ucrt-x86_64-swig
    mingw-w64-ucrt-x86_64-cmake
    mingw-w64-ucrt-x86_64-zeromq
    mingw-w64-ucrt-x86_64-cppzmq
    mingw-w64-ucrt-x86_64-python
    mingw-w64-ucrt-x86_64-python-pip
    mingw-w64-ucrt-x86_64-python-pyqt5
    mingw-w64-ucrt-x86_64-python-pyzmq
    # media index v2 vector search; the venv uses --system-site-packages, so pip
    # sees this as already satisfied instead of building numpy from source.
    mingw-w64-ucrt-x86_64-python-numpy
    mingw-w64-ucrt-x86_64-rust
    mingw-w64-ucrt-x86_64-qt5-svg
    mingw-w64-ucrt-x86_64-python-cx-freeze
    mingw-w64-ucrt-x86_64-python-lief
    mingw-w64-ucrt-x86_64-python-cffi
    mingw-w64-ucrt-x86_64-python-zstandard
    # mcp's dependency tree pulls native extensions that have no MinGW
    # wheel on PyPI, so pip falls back to compiling them and pyo3 fails
    # to link against MinGW Python 3.14 ("undefined reference to
    # PyExc_OSError", maturin exit 1). Take MSYS2's prebuilt copies
    # instead: the venv is created with --system-site-packages, so pip
    # sees these as already satisfied and never builds them.
    mingw-w64-ucrt-x86_64-python-cryptography
    mingw-w64-ucrt-x86_64-python-rpds-py
    mingw-w64-ucrt-x86_64-python-jsonschema
    mingw-w64-ucrt-x86_64-python-pydantic
    mingw-w64-ucrt-x86_64-python-pydantic-core
    mingw-w64-ucrt-x86_64-python-pyjwt
    # Required at import time, not optional: see the pip step below.
    mingw-w64-ucrt-x86_64-python-pywin32
    mingw-w64-ucrt-x86_64-qtwebkit
    mingw-w64-ucrt-x86_64-libffi
    mingw-w64-ucrt-x86_64-ninja
    mingw-w64-ucrt-x86_64-babl
    mingw-w64-ucrt-x86_64-jsoncpp
    mingw-w64-ucrt-x86_64-libsamplerate
    # libopenshot 1.0 OpenCV effects (Tracker / Object Detector / Stabilizer);
    # ENABLE_OPENCV also needs protobuf for the tracker data files.
    mingw-w64-ucrt-x86_64-opencv
    mingw-w64-ucrt-x86_64-protobuf
  )
  attempt=1
  max=6
  while true; do
    if pacman -Syuu --noconfirm --disable-download-timeout \
      && pacman -S --needed --noconfirm --overwrite '*' --disable-download-timeout "${pkgs[@]}"; then
      break
    fi
    if (( attempt >= max )); then
      echo "::error::pacman install failed after ${max} attempts (slow/stalled MSYS2 mirrors)"
      exit 1
    fi
    echo "pacman failed (attempt ${attempt}/${max}), retrying..."
    sleep $(( attempt * 20 ))
    attempt=$((attempt + 1))
  done
}

case "${1:-all}" in
  harden) harden ;;
  install) install ;;
  all) harden; install ;;
  *) echo "usage: $0 [harden|install|all]" >&2; exit 2 ;;
esac
