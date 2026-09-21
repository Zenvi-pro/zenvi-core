# Export benchmark media fixtures

Generated (not committed) short clips used by `scripts/benchmark-export.py`
and export acceleration tests.

Generate with:

```bash
python3 scripts/generate_export_fixtures.py
```

Outputs land in this directory:
- `h264_1080p30_2s.mp4`
- `h264_720p30_2s.mp4`
- `hevc_1080p30_2s.mp4` (skipped if encoder unavailable)
- `h264_4k30_1s.mp4`
