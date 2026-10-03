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
LAYER_VERSIONS = {
    LAYER_STRUCTURE: 1,
    LAYER_AUDIO: 1,
    LAYER_SPEECH: 1,
    LAYER_LOOK: 1,
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
