#!/usr/bin/env bash
# installer/build-ffmpeg-cli.sh [DEST]
#
# Builds a self-contained ffmpeg + ffprobe for the macOS app (static, libx264
# linked in, system frameworks only) and puts them in DEST (default:
# build/ffmpeg-cli). freeze.py copies them next to the frozen executable
# (<app>/lib/ffmpeg, <app>/lib/ffprobe), where classes/ffmpeg_cli.find_ffmpeg
# looks first. Windows already ships ffmpeg.exe from MSYS2; Linux uses the
# system ffmpeg.
#
# Why: the app only bundles the libav* dylibs that libopenshot needs. Indexing,
# transcription audio extraction, thumbnails and the export fixtures all shell
# out to the ffmpeg CLI, so a Mac without Homebrew ffmpeg failed them.
#
# The app is GPLv3 (COPYING), so linking GPL libx264 is licence-compatible.
set -euo pipefail

FFMPEG_TAG="${FFMPEG_TAG:-n8.0.3}"
X264_REF="${X264_REF:-b35605ace3ddf7c1a5d67a2eb553f034aef41d55}"   # videolan/x264 stable
ARCH="${FFMPEG_ARCH:-$(uname -m)}"                                  # arm64 | x86_64
DEPLOY="${MACOSX_DEPLOYMENT_TARGET:-12.0}"

[[ "$(uname -s)" == "Darwin" ]] || { echo "ERROR: this script builds the macOS ffmpeg only" >&2; exit 1; }
for tool in git make clang pkg-config; do
  command -v "$tool" >/dev/null || { echo "ERROR: $tool is needed to build ffmpeg" >&2; exit 1; }
done

root="$(cd "$(dirname "$0")/.." && pwd)"
dest="${1:-${root}/build/ffmpeg-cli}"
work="${FFMPEG_WORK:-${root}/build/ffmpeg-src}"
prefix="${work}/prefix-${ARCH}"
jobs="$(sysctl -n hw.logicalcpu)"
mkdir -p "$dest" "$work"

# Skip a rebuild when a matching binary is already in place (CI cache / re-run).
stamp="${dest}/.built-${FFMPEG_TAG}-${X264_REF:0:12}-${ARCH}"
if [[ -x "${dest}/ffmpeg" && -x "${dest}/ffprobe" && -f "$stamp" ]]; then
  echo "ffmpeg ${FFMPEG_TAG} (${ARCH}) already built in ${dest}"
  exit 0
fi

archflags=(-arch "$ARCH" -mmacosx-version-min="$DEPLOY")
cc_cmd="clang ${archflags[*]}"

# --- x264 (static) ---------------------------------------------------------
if [[ ! -f "${prefix}/lib/libx264.a" ]]; then
  rm -rf "${work}/x264"
  git init -q "${work}/x264"
  git -C "${work}/x264" fetch -q --depth 1 https://code.videolan.org/videolan/x264.git "$X264_REF"
  git -C "${work}/x264" checkout -q --detach FETCH_HEAD
  x264_flags=(--prefix="$prefix" --enable-static --enable-pic --disable-cli --disable-opencl
              --extra-cflags="${archflags[*]}" --extra-ldflags="${archflags[*]}")
  if [[ "$ARCH" == "x86_64" && "$(uname -m)" != "x86_64" ]]; then
    # Cross-building the Intel app on an Apple Silicon runner: no nasm, no asm.
    x264_flags+=(--host=x86_64-apple-darwin --disable-asm)
  fi
  (cd "${work}/x264" && CC="$cc_cmd" ./configure "${x264_flags[@]}" && make -j"$jobs" && make install)
fi

# --- ffmpeg ----------------------------------------------------------------
if [[ -d "${work}/ffmpeg/.git" ]]; then
  git -C "${work}/ffmpeg" fetch -q --depth 1 origin "refs/tags/${FFMPEG_TAG}:refs/tags/${FFMPEG_TAG}"
  git -C "${work}/ffmpeg" checkout -q --detach "$FFMPEG_TAG"
else
  rm -rf "${work}/ffmpeg"
  git clone -q --depth 1 --branch "$FFMPEG_TAG" https://git.ffmpeg.org/ffmpeg.git "${work}/ffmpeg"
fi

ff_flags=(--prefix="${prefix}/ff" --arch="$ARCH" --cc="$cc_cmd"
          --enable-gpl --enable-libx264 --enable-static --disable-shared
          --disable-autodetect --enable-videotoolbox --enable-audiotoolbox --enable-zlib
          --disable-ffplay --disable-doc --disable-debug
          --extra-cflags="-I${prefix}/include" --extra-ldflags="-L${prefix}/lib")
if [[ "$ARCH" == "x86_64" && "$(uname -m)" != "x86_64" ]]; then
  ff_flags+=(--enable-cross-compile --target-os=darwin --disable-x86asm)
fi
(cd "${work}/ffmpeg" && make distclean >/dev/null 2>&1 || true)
(cd "${work}/ffmpeg" && PKG_CONFIG_PATH="${prefix}/lib/pkgconfig" ./configure "${ff_flags[@]}" \
  && make -j"$jobs" ffmpeg ffprobe)

cp "${work}/ffmpeg/ffmpeg" "${work}/ffmpeg/ffprobe" "$dest/"
strip -x "${dest}/ffmpeg" "${dest}/ffprobe"
rm -f "${dest}"/.built-*
: > "$stamp"

# --- verify: runs, links nothing outside the OS, and has what the app calls ---
bad="$(otool -L "${dest}/ffmpeg" | tail -n +2 | awk '{print $1}' | grep -vE '^(/usr/lib/|/System/Library/)' || true)"
[[ -z "$bad" ]] || { echo "ERROR: ffmpeg links non-system libraries:" >&2; echo "$bad" >&2; exit 1; }
if [[ "$ARCH" == "$(uname -m)" ]]; then
  "${dest}/ffmpeg" -hide_banner -encoders 2>/dev/null | grep -qE ' libx264 ' || { echo "ERROR: libx264 missing" >&2; exit 1; }
  "${dest}/ffmpeg" -hide_banner -encoders 2>/dev/null | grep -qE ' aac ' || { echo "ERROR: aac encoder missing" >&2; exit 1; }
  "${dest}/ffmpeg" -hide_banner -filters 2>/dev/null | grep -qE ' (select|showinfo|ebur128|silencedetect|astats|aspectralstats|showspectrumpic) ' || true
fi
echo "ffmpeg ${FFMPEG_TAG} (${ARCH}) built into ${dest}"
