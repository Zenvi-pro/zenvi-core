"""
 @file
 @brief The media index's units, layer versions and analysis constants, in one place.

 Units used in every layer file: seconds (float) for time, 0..1 for luma / channel means /
 fractions, dBFS or LUFS for loudness (negative numbers), fractions of the frame (not
 pixels) for positions and speeds. A field is ``measured`` (computed from the file) or
 ``inferred`` (a model's or heuristic's reading of it); inferred ones carry a confidence.
"""

from __future__ import annotations

MEASURED = "measured"
INFERRED = "inferred"

# Layer name -> version. Bump a version when the layer's algorithm or output shape changes:
# the shelf then treats the saved layer as out of date and re-runs only that layer.
LAYER_STRUCTURE = "structure"
LAYER_AUDIO = "audio"
LAYER_SPEECH = "speech"
LAYER_LOOK = "look"
LAYER_WATCH = "watch"        # cloud: what happens in each shot, described on demand by Gemini
LAYER_VECTORS = "vectors_local"    # search vectors of every kind, from the model chosen in Preferences (see vectors.py)
LAYER_VERSIONS = {
    LAYER_STRUCTURE: 3,         # 3: black judged against the file's own shots; motion carries typical_jitter_px
    LAYER_AUDIO: 3,             # 3: sections only with a steady tempo, no downbeats, phrase points are section starts
    LAYER_SPEECH: 1,
    LAYER_LOOK: 1,
    LAYER_WATCH: 2,             # 2: interest, usable, people, hook (older saved watch layers are kept, never re-bought)
    LAYER_VECTORS: 1,
}

# One decode pass feeds cuts, motion and colour: small frames, ten per second. Measured:
# at 320 px the colour statistics are within 0.001 (means), 0.015 (percentiles) and 0.005
# (clipped fractions) of a 640 px analysis, so a larger frame buys nothing.
ANALYSIS_LONG_EDGE = 320
ANALYSIS_FPS = 10.0

# Shots shorter than this are folded into a neighbour; longer ones are split so no record
# covers more than SHOT_MAX_SECONDS (finer search and cheaper watching).
SHOT_MIN_SECONDS = 0.3
SHOT_MAX_SECONDS = 8.0

# Search vectors and descriptions.
EMBED_DIMS = 768                  # vector size stored on the shelf (float16, L2-normalised)
KEYFRAME_EVERY_SECONDS = 2.0      # at least one picture vector this often inside a shot
MAX_KEYFRAMES = 3000              # a safety cap per file
PROXY_HEIGHT = 480

# Lazy layers: made the first time they are asked for, then kept. They are not part of "fully indexed".
LAYER_MUSIC_DESC = "music_desc"     # cloud: genre, mood, energy, vocals per window of a music file
LAZY_LAYERS = {LAYER_MUSIC_DESC: 1}

NOT_APPLICABLE = "not_applicable"     # manifest status of a layer the file cannot have (no audio track, no picture)

# The watch layer (descriptions) costs money, so it is never re-run because of a version bump: an older one stays in
# use and the new fields read as unknown. Local layers are free and do refresh; vectors are re-made for a new version.
# Layer shapes only ever grow: a reader accepts a layer saved at an older version (it lacks the newer
# fields) and the next index run refreshes it, so a version bump never makes existing data unreadable.
