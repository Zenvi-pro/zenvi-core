"""Generated media: ComfyUI create/enhance and jobs, narration (TTS), the Recording dock.

Workstream: ai-generation. The tools live in topic modules registered below:

* ``ai_generation_comfyui`` -- the optional local ComfyUI integration (AI Tools menu):
  template catalogue, Create with AI, Enhance with AI, the generation queue.
* ``ai_generation_tts`` -- narration from text, placed on a real track.
* ``ai_generation_recording`` -- open and configure the Recording dock; the person
  presses Record (capture itself stays a human action).

The cloud AI video tools (generate_video / modify_clip / generate_transition_clip),
semantic search and stock import are pre-existing handlers in ``tool_handlers``
(see ``coverage.LEGACY_COVERAGE``); this workstream fixed their failure semantics.
"""

from classes.editor_tools import (  # noqa: F401
    ai_generation_comfyui,
    ai_generation_recording,
    ai_generation_tts,
)
