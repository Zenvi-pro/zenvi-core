#!/usr/bin/env bash
# installer/build-whisper-cli.sh [DEST]
#
# Bundles local transcription for a release: builds whisper.cpp's whisper-cli
# as a self-contained binary (static, portable CPU flags, no OpenMP) and puts
# it with the quantized base model and the Silero VAD model in DEST (default:
# src/whisper, which freeze.py ships as <app>/whisper --
# classes/speech/whisper_cpp.py looks there).
#
# Runs in the Linux, macOS and Windows (MSYS2 UCRT64) release jobs.
set -euo pipefail

WHISPER_TAG="${WHISPER_TAG:-v1.9.4}"
# name | sha256 | pinned URL (a revision, not "main": a changed file upstream
# must not break, or silently change, a release).
MODELS=(
  "ggml-base-q5_1.bin|422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-base-q5_1.bin"
  "ggml-silero-v5.1.2.bin|29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf|https://huggingface.co/ggml-org/whisper-vad/resolve/9ffd54a1e1ee413ddf265af9913beaf518d1639b/ggml-silero-v5.1.2.bin"
)

for tool in git cmake curl; do
  command -v "$tool" >/dev/null || { echo "ERROR: $tool is needed to bundle whisper.cpp" >&2; exit 1; }
done
command -v sha256sum >/dev/null || command -v shasum >/dev/null \
  || { echo "ERROR: sha256sum or shasum is needed to verify the models" >&2; exit 1; }

root="$(cd "$(dirname "$0")/.." && pwd)"
dest="${1:-${root}/src/whisper}"
work="${WHISPER_WORK:-${root}/build/whisper.cpp}"
mkdir -p "$dest" "$(dirname "$work")"

# Always the pinned tag, also on a reused (cached / left-over) checkout.
if [[ -d "$work/.git" ]]; then
  git -C "$work" fetch --depth 1 origin "refs/tags/${WHISPER_TAG}:refs/tags/${WHISPER_TAG}"
  git -C "$work" checkout --quiet --detach "$WHISPER_TAG"
else
  rm -rf "$work"
  git clone --depth 1 --branch "$WHISPER_TAG" https://github.com/ggml-org/whisper.cpp.git "$work"
fi

flags=(-DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF
       -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF -DWHISPER_SDL2=OFF -DWHISPER_CURL=OFF)
exe=whisper-cli
case "$(uname -s)" in
  MINGW*|MSYS*)
    exe=whisper-cli.exe
    # No libstdc++ / libgcc / winpthread DLLs next to it.
    flags+=(-DCMAKE_EXE_LINKER_FLAGS="-static -static-libgcc -static-libstdc++") ;;
  Darwin)
    flags+=(-DCMAKE_OSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-12.0}") ;;
esac
if command -v ninja >/dev/null; then
  generator=(-G Ninja)
elif [[ "$exe" == *.exe ]]; then
  generator=(-G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make)  # as ci-win-msys-libopenshot.sh
else
  generator=(-G "Unix Makefiles")
fi
cmake -S "$work" -B "$work/build" "${generator[@]}" "${flags[@]}"
cmake --build "$work/build" --target whisper-cli -j
cp "$work/build/bin/$exe" "$dest/$exe"

sha256() {  # sha256sum on Linux / MSYS2, shasum on macOS
  if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1
}
for entry in "${MODELS[@]}"; do
  IFS='|' read -r name sum url <<<"$entry"
  file="$dest/$name"
  if [[ ! -f "$file" || "$(sha256 "$file")" != "$sum" ]]; then
    curl -fsSL --retry 3 -o "$file.part" "$url"
    mv "$file.part" "$file"
  fi
  # Never ship a model other than the one this release was tested with.
  actual="$(sha256 "$file")"
  [[ "$actual" == "$sum" ]] || { echo "ERROR: $name checksum $actual != $sum" >&2; exit 1; }
done

"$dest/$exe" --help >/dev/null 2>&1 || { echo "ERROR: $dest/$exe does not run" >&2; exit 1; }
echo "whisper-cli ${WHISPER_TAG}, ggml-base-q5_1.bin and ggml-silero-v5.1.2.bin in ${dest}"
