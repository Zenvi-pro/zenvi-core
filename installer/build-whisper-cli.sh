#!/usr/bin/env bash
# installer/build-whisper-cli.sh [DEST]
#
# Bundles local transcription for a release: builds whisper.cpp's whisper-cli
# as a self-contained binary (static, portable CPU flags, no OpenMP) and puts
# it with the quantized base model in DEST (default: src/whisper, which
# freeze.py ships as <app>/whisper -- classes/speech/whisper_cpp.py looks there).
#
# Runs in the Linux, macOS and Windows (MSYS2 UCRT64) release jobs.
set -euo pipefail

WHISPER_TAG="${WHISPER_TAG:-v1.9.4}"
MODEL_FILE="ggml-base-q5_1.bin"
MODEL_SHA256="422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/${MODEL_FILE}"

root="$(cd "$(dirname "$0")/.." && pwd)"
dest="${1:-${root}/src/whisper}"
work="${WHISPER_WORK:-${root}/build/whisper.cpp}"
mkdir -p "$dest" "$(dirname "$work")"

if [[ ! -d "$work/.git" ]]; then
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
model="$dest/$MODEL_FILE"
if [[ ! -f "$model" || "$(sha256 "$model")" != "$MODEL_SHA256" ]]; then
  curl -fsSL --retry 3 -o "$model.part" "$MODEL_URL"
  mv "$model.part" "$model"
fi
# Never ship a model other than the one this release was tested with.
actual="$(sha256 "$model")"
[[ "$actual" == "$MODEL_SHA256" ]] || { echo "ERROR: $MODEL_FILE checksum $actual != $MODEL_SHA256" >&2; exit 1; }

"$dest/$exe" --help >/dev/null 2>&1 || { echo "ERROR: $dest/$exe does not run" >&2; exit 1; }
echo "whisper-cli ${WHISPER_TAG} and ${MODEL_FILE} in ${dest}"
