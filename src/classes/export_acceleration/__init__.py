"""Export acceleration helpers.

Phases:
  - hw_decode: auto-detect / fall back hardware decoding
  - hw_encode: probe and select hardware encoders
  - export_tuning: queue / cache sizing for pipelined export
  - export_pipeline: overlapped composite + encode
  - smart_render: stream-copy untouched spans
  - background_render: idle-time cache warming + disk render files
"""

from classes.export_acceleration.hw_decode import (
    apply_hardware_decode_settings,
    maybe_auto_detect_hardware_decoder,
    probe_hardware_decoder,
    with_software_decode_fallback,
)
from classes.export_acceleration.hw_encode import (
    get_preferred_video_encoder,
    hardware_bitrate_multiplier,
    probe_video_encoder,
)
from classes.export_acceleration.export_tuning import (
    export_cache_bytes,
    get_export_pipeline_profile,
    is_pipelined_export_safe,
    uses_mp4_faststart_preset,
)
from classes.export_acceleration.export_pipeline import (
    PipelineCancelled,
    run_pipelined_export,
)
from classes.export_acceleration.smart_render import (
    analyze_smart_render_spans,
    try_smart_render_export,
)
from classes.export_acceleration.background_render import (
    BackgroundRenderManager,
    content_hash_for_segment,
)

__all__ = [
    "apply_hardware_decode_settings",
    "maybe_auto_detect_hardware_decoder",
    "probe_hardware_decoder",
    "with_software_decode_fallback",
    "get_preferred_video_encoder",
    "hardware_bitrate_multiplier",
    "probe_video_encoder",
    "export_cache_bytes",
    "get_export_pipeline_profile",
    "is_pipelined_export_safe",
    "uses_mp4_faststart_preset",
    "PipelineCancelled",
    "run_pipelined_export",
    "analyze_smart_render_spans",
    "try_smart_render_export",
    "BackgroundRenderManager",
    "content_hash_for_segment",
]
