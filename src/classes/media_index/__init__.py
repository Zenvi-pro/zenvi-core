"""
 @file
 @brief Media index v2: a durable, fingerprint-keyed record of what a media file contains.

 One folder per file *content* (not per project, not per path) under
 ``USER_PATH/media_index``. Indexing looks the fingerprint up before doing any cloud
 work, so the same video in another project, or at another path, is never analysed
 (or charged) twice. See ``store.Shelf``.
"""

from classes.media_index.schema import LAYER_VECTORS as S_VECTORS, LAYER_WATCH as S_WATCH  # noqa: F401
from classes.media_index.store import (  # noqa: F401
    LAYER_V1,
    SCHEMA_VERSION,
    Shelf,
    default_shelf,
    sha_of,
)
