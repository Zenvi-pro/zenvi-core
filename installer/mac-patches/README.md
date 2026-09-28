# Source patches for OpenShot upstream libraries

These patches apply to OpenShot's upstream `libopenshot` and `libopenshot-audio`
repositories at a specific tag. They are applied at build time by

- `scripts/build-mac-libopenshot.sh` (local macOS builds, default tag `v0.5.0`),
- `.github/workflows/release.yml` (macOS arm64 release job, tag `v1.0.0`),
- `installer/ci-win-msys-libopenshot.sh` (Windows MSYS2 release job, tag `v1.0.0`).

All three use the same rule: every `libopenshot-<tag>-*.patch` /
`libopenshot-audio-<tag>-*.patch` file is applied **only if
`git apply --check` passes**; a patch that no longer applies (because upstream
merged the fix) is skipped with a notice. Patch file names are tag-locked, so
bumping the tag means adding (or copying) patches under the new name and
deleting the ones that are no longer needed.

After the patches, `installer/patch-libopenshot-ffmpeg.py` rewrites the
FFmpeg 8/9 `AVCodec` field accesses (`supported_samplerates`, `ch_layouts`,
`sample_fmts`, `pix_fmts` were removed in FFmpeg 8). That script is still
required at `v1.0.0`.

Zenvi requires libopenshot **1.0.0** (`src/classes/info.py`,
`MINIMUM_LIBOPENSHOT_VERSION`). The `v0.5.0` patches are kept only so the old
default of the local build script keeps working.

## libopenshot-audio

### `libopenshot-audio-v0.5.0-mac.patch`, `libopenshot-audio-v1.0.0-mac.patch` (identical)

**File:** `CMakeLists.txt`

Removes the `-framework AGL` linker flag. AGL (Apple Graphics Library) was
deprecated in macOS 10.9 and removed in macOS 10.14; it is absent from modern
SDKs, and JUCE's old build configuration still references it. Linking succeeds
without it on macOS 11+.

Status at `v1.0.0`: **still needed** (`-framework AGL` is still in upstream's
`CMakeLists.txt`). The Windows script also tries this patch and skips it.

## libopenshot

### `libopenshot-v1.0.0-noncrop-location.patch`

**File:** `src/Clip.cpp`. Source: libopenshot `develop` commit
`98ee060d94856e7d5765f8d939ab8f03cab6b35b` ("Preserve non-crop clip location
behavior", 2026-09-03), `src/Clip.cpp` hunk only.

libopenshot 1.0 changed `location_x` / `location_y` so that `+/-1` moves a
clip fully offscreen (relative to the scaled clip geometry) instead of moving
it by one canvas width/height. The `v1.0.0` **tag** applies that to every scale
mode. Upstream corrected this right after tagging: only `SCALE_CROP` uses the
new math; Fit, Stretch and None keep the historical canvas-relative meaning,
so existing projects do not shift.

Zenvi's preview transform handles (`src/windows/video_widget.py`, ported from
openshot-qt PR #6109) and its project migration (`src/classes/project_data.py`,
ported from openshot-qt PRs #6075/#6109) implement the corrected contract, so
this patch is **required** for the preview handles to match the rendered frame.
Drop it when moving to a libopenshot tag that contains `98ee060`.

### `libopenshot-v0.5.0-mac.patch` (v0.5.0 only)

Five files, five logical fixes. **None of them apply at `v1.0.0`**: every fix
below is upstream by then, which is exactly why the patch fails
`git apply --check` and is skipped.

| Fix | Files | Upstream status at v1.0.0 |
| --- | --- | --- |
| 1. Drop `avresample` from FFmpeg detection | `cmake/Modules/FindFFmpeg.cmake`, `src/CMakeLists.txt` | `avresample` is only requested when `swresample` is missing |
| 2. `FF_PROFILE_*` → `AV_PROFILE_*` | `src/FFmpegWriter.cpp` | no `FF_PROFILE_*` left |
| 3. `av_stream_add_side_data()` removed | `src/FFmpegWriter.cpp` | no call left |
| 4. `AVStream::nb_side_data` → `codecpar->nb_coded_side_data` | `src/FFmpegReader.cpp` | no `nb_side_data` left |
| 5. SWR channel layout via `av_opt_set_chlayout` (FFmpeg 8) | `src/FFmpegReader.cpp`, `src/FrameMapper.cpp` | upstream since v0.7.0, guarded by `HAVE_CH_LAYOUT` |

Fix 5 was the one with the visible symptom (black preview / "timeline doesn't
play" on macOS with FFmpeg 8): the legacy `in_channel_layout` option is
ignored by FFmpeg 8, `swr_init()` fails, and the playback clock never starts.

## Adding a patch

1. Name it `libopenshot-<tag>-<topic>.patch` or `libopenshot-audio-<tag>-<topic>.patch`.
2. Start the file with a short prose header (what, why, upstream commit if any);
   `git apply` ignores text before the first `diff --git`.
3. Check it: `git -C <clone at tag> apply --check --whitespace=nowarn <patch>`.
4. Record it in this file, including when it can be dropped.
