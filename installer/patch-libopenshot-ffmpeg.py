#!/usr/bin/env python3
"""Patch cloned libopenshot sources for FFmpeg 7/8 (MSYS2/Homebrew currently ship 8.x).

FFmpeg 8 removed AVCodec.supported_samplerates / ch_layouts / sample_fmts / pix_fmts.
Query them via avcodec_get_supported_config() instead.
"""
from __future__ import annotations

import glob
import os
import re
import sys

HELPERS = r"""
#ifndef OPENSHOT_FFMPEG8_CODEC_CONFIG
#define OPENSHOT_FFMPEG8_CODEC_CONFIG
#if LIBAVCODEC_VERSION_INT >= AV_VERSION_INT(61, 13, 100)
inline static const int *openshot_codec_supported_samplerates(const AVCodec *codec) {
	const int *rates = NULL;
	avcodec_get_supported_config(NULL, codec, AV_CODEC_CONFIG_SAMPLE_RATE, 0,
	                             (const void **)&rates, NULL);
	return rates;
}
#if HAVE_CH_LAYOUT
inline static const AVChannelLayout *openshot_codec_ch_layouts(const AVCodec *codec) {
	const AVChannelLayout *layouts = NULL;
	avcodec_get_supported_config(NULL, codec, AV_CODEC_CONFIG_CHANNEL_LAYOUT, 0,
	                             (const void **)&layouts, NULL);
	return layouts;
}
#endif
inline static const AVSampleFormat *openshot_codec_sample_fmts(const AVCodec *codec) {
	const AVSampleFormat *fmts = NULL;
	avcodec_get_supported_config(NULL, codec, AV_CODEC_CONFIG_SAMPLE_FORMAT, 0,
	                             (const void **)&fmts, NULL);
	return fmts;
}
inline static const AVPixelFormat *openshot_codec_pix_fmts(const AVCodec *codec) {
	const AVPixelFormat *fmts = NULL;
	avcodec_get_supported_config(NULL, codec, AV_CODEC_CONFIG_PIX_FORMAT, 0,
	                             (const void **)&fmts, NULL);
	return fmts;
}
#else
inline static const int *openshot_codec_supported_samplerates(const AVCodec *codec) {
	return codec->supported_samplerates;
}
#if HAVE_CH_LAYOUT
inline static const AVChannelLayout *openshot_codec_ch_layouts(const AVCodec *codec) {
	return codec->ch_layouts;
}
#endif
inline static const AVSampleFormat *openshot_codec_sample_fmts(const AVCodec *codec) {
	return codec->sample_fmts;
}
inline static const AVPixelFormat *openshot_codec_pix_fmts(const AVCodec *codec) {
	return codec->pix_fmts;
}
#endif
#endif
"""

FIELD_REPLACEMENTS = (
    ("codec->supported_samplerates", "openshot_codec_supported_samplerates(codec)"),
    ("codec->ch_layouts", "openshot_codec_ch_layouts(codec)"),
    ("codec->sample_fmts", "openshot_codec_sample_fmts(codec)"),
    ("codec->pix_fmts", "openshot_codec_pix_fmts(codec)"),
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        return fh.read()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write(text)


def patch_utilities(root: str) -> None:
    path = os.path.join(root, "src", "FFmpegUtilities.h")
    if not os.path.isfile(path):
        raise SystemExit(f"missing {path}")
    text = _read(path)
    if "OPENSHOT_FFMPEG8_CODEC_CONFIG" in text:
        return
    patched, n = re.subn(
        r"#endif\s+//\s*OPENSHOT_FFMPEG_UTILITIES_H",
        HELPERS.lstrip("\n") + "\n#endif  // OPENSHOT_FFMPEG_UTILITIES_H",
        text,
        count=1,
    )
    if n != 1:
        raise SystemExit(f"could not insert FFmpeg 8 helpers into {path}")
    _write(path, patched)


def patch_sources(root: str) -> None:
    for path in glob.glob(os.path.join(root, "**", "*.cpp"), recursive=True):
        try:
            text = _read(path)
        except OSError:
            continue
        original = text
        if "FF_PROFILE_" in text:
            text = (
                text.replace("FF_PROFILE_H264_BASELINE", "AV_PROFILE_H264_BASELINE")
                .replace("FF_PROFILE_H264_CONSTRAINED", "AV_PROFILE_H264_CONSTRAINED")
                .replace("FF_PROFILE_H264_MAIN", "AV_PROFILE_H264_MAIN")
                .replace("FF_PROFILE_H264_HIGH", "AV_PROFILE_H264_HIGH")
            )
        if "av_stream_add_side_data" in text:
            text = re.sub(
                r"av_stream_add_side_data\([^;]*\);",
                "(void)0; /* removed FFmpeg7+ */",
                text,
            )
        if "->nb_side_data" in text:
            text = text.replace("->nb_side_data", "->codecpar->nb_coded_side_data").replace(
                "->side_data[", "->codecpar->coded_side_data["
            )
        if "openshot_codec_supported_samplerates" not in text:
            for old, new in FIELD_REPLACEMENTS:
                text = text.replace(old, new)
        if text != original:
            _write(path, text)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} <libopenshot-src>")
    root = sys.argv[1]
    if not os.path.isdir(root):
        raise SystemExit(f"not a directory: {root}")
    patch_utilities(root)
    patch_sources(root)


if __name__ == "__main__":
    main()
