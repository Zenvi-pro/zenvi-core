"""
@file
@brief Simple theme application functions - no threading, synchronous execution
@author Zenvi Team

@section LICENSE

Copyright (c) 2024 Zenvi Core
This file is part of Zenvi Core video editor.
"""

import json
from typing import Dict, Any, List, Optional
from classes.logger import log


def _get_app():
    """Get app; must be called from main thread."""
    from classes.app import get_app
    return get_app()


def apply_color_grading(clip_ids: List[str], color_config: Dict[str, Any]) -> str:
    """Apply color grading effects to clips"""
    try:
        import openshot
        from classes.query import Clip
        
        effects_applied = 0
        
        for clip_id in clip_ids:
            clip = Clip.get(id=clip_id)
            if not clip:
                continue
            
            # Apply brightness/contrast
            if "brightness" in color_config or "contrast" in color_config:
                brightness = color_config.get("brightness", 1.0)
                contrast = color_config.get("contrast", 1.0)
                
                effect = openshot.EffectInfo().CreateEffect("Brightness")
                effect.Id(_get_app().project.generate_id())
                effect_json = json.loads(effect.Json())
                
                if "brightness" in effect_json:
                    effect_json["brightness"]["value"] = brightness
                if "contrast" in effect_json:
                    openshot_contrast = (contrast - 1.0) * 3 + 3
                    effect_json["contrast"]["value"] = max(0, min(20, openshot_contrast))
                
                _add_effect_to_clip(clip, effect_json)
                effects_applied += 1
            
            # Apply saturation
            if "saturation" in color_config:
                saturation = color_config.get("saturation", 1.0)
                
                effect = openshot.EffectInfo().CreateEffect("Saturation")
                effect.Id(_get_app().project.generate_id())
                effect_json = json.loads(effect.Json())
                
                if "saturation" in effect_json:
                    effect_json["saturation"]["value"] = saturation
                
                _add_effect_to_clip(clip, effect_json)
                effects_applied += 1
            
            # Apply hue shift
            if "hue_shift" in color_config and color_config["hue_shift"] != 0:
                hue_shift = color_config.get("hue_shift", 0)
                
                effect = openshot.EffectInfo().CreateEffect("Hue")
                effect.Id(_get_app().project.generate_id())
                effect_json = json.loads(effect.Json())
                
                if "hue" in effect_json:
                    effect_json["hue"]["value"] = hue_shift
                
                _add_effect_to_clip(clip, effect_json)
                effects_applied += 1
        
        return "Applied {} color grading effects to {} clips".format(effects_applied, len(clip_ids))
    except Exception as e:
        log.error("apply_color_grading: %s", e, exc_info=True)
        return "Error: {}".format(e)


def apply_film_grain(clip_ids: List[str], intensity: float = 0.3) -> str:
    """Apply film grain effect to clips"""
    try:
        import openshot
        from classes.query import Clip
        
        for clip_id in clip_ids:
            clip = Clip.get(id=clip_id)
            if not clip:
                continue
            
            effect = openshot.EffectInfo().CreateEffect("Noise")
            effect.Id(_get_app().project.generate_id())
            effect_json = json.loads(effect.Json())
            
            if "level" in effect_json:
                effect_json["level"]["value"] = intensity
            
            _add_effect_to_clip(clip, effect_json)
        
        return "Applied film grain to {} clips".format(len(clip_ids))
    except Exception as e:
        log.error("apply_film_grain: %s", e, exc_info=True)
        return "Error: {}".format(e)


def apply_sound_effects(clip_ids: List[str], sound_config: Dict[str, Any]) -> str:
    """Apply sound effects to clips"""
    try:
        from classes.query import Clip
        
        effects_applied = 0
        
        for clip_id in clip_ids:
            clip = Clip.get(id=clip_id)
            if not clip:
                continue
            
            clip_data = clip.data if isinstance(clip.data, dict) else {}
            
            # Check if clip has audio
            has_audio = clip_data.get("has_audio", True)
            if not has_audio:
                continue
            
            # Store audio effects in metadata for now
            if "audio_effects" not in clip_data:
                clip_data["audio_effects"] = []
            
            # Add EQ settings
            if "eq" in sound_config:
                clip_data["audio_effects"].append({
                    "type": "eq",
                    **sound_config["eq"]
                })
                effects_applied += 1
            
            # Add bass boost
            if "bass_boost_db" in sound_config and sound_config["bass_boost_db"] != 0:
                clip_data["audio_effects"].append({
                    "type": "bass_boost",
                    "amount": sound_config["bass_boost_db"]
                })
                effects_applied += 1
            
            # Update clip
            _get_app().updates.update(["clips", clip.id], clip_data)
        
        if effects_applied > 0:
            return "Applied {} sound effects to {} clips".format(effects_applied, len(clip_ids))
        else:
            return "No audio tracks found in selected clips"
    except Exception as e:
        log.error("apply_sound_effects: %s", e, exc_info=True)
        return "Error: {}".format(e)


def add_captions_to_clips(clip_ids: List[str], caption_config: Optional[Dict[str, Any]] = None, api_key: Optional[str] = None) -> str:
    """Add auto-generated captions to clips using Whisper API"""
    try:
        if not api_key:
            # Try to get from settings
            try:
                app = _get_app()
                api_key = app._data.get("openai_whisper_api_key") or app._data.get("openai-api-key")
            except:
                pass
            
            if not api_key:
                import os
                api_key = os.getenv("OPENAI_API_KEY")
        
        if not api_key:
            return "Error: OpenAI API key not configured. Set it in Preferences > AI or environment variable OPENAI_API_KEY"
        
        try:
            from openai import OpenAI
        except ImportError:
            return "Error: openai package not installed. Run: pip install openai"
        
        client = OpenAI(api_key=api_key)
        
        from classes.query import Clip, File
        import tempfile
        import os
        from pathlib import Path
        
        captions_added = 0
        
        for clip_id in clip_ids:
            clip = Clip.get(id=clip_id)
            if not clip:
                continue
            
            # Get clip file path
            clip_data = clip.data if isinstance(clip.data, dict) else {}
            reader = clip_data.get("reader", {})
            
            if isinstance(reader, dict):
                file_path = reader.get("path")
            else:
                clip_file = File.get(id=clip_data.get("file_id"))
                file_path = clip_file.data.get("path") if clip_file else None
            
            if not file_path or not os.path.exists(file_path):
                log.warning("No file path for clip {}".format(clip_id))
                continue
            
            # Check if file is audio/video
            file_ext = Path(file_path).suffix.lower()
            if file_ext not in ['.mp3', '.wav', '.mp4', '.avi', '.mov', '.mkv', '.m4a', '.ogg']:
                continue
            
            # Transcribe with Whisper
            try:
                with open(file_path, "rb") as audio_file:
                    response = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file,
                        response_format="verbose_json",
                        timestamp_granularities=["segment"]
                    )
                
                if not response.segments:
                    continue
                
                # Add caption metadata to clip
                if "captions" not in clip_data:
                    clip_data["captions"] = []
                
                for seg in response.segments:
                    caption_data = {
                        "start": seg.start,
                        "end": seg.end,
                        "text": seg.text.strip()
                    }
                    if caption_config:
                        caption_data.update(caption_config)
                    
                    clip_data["captions"].append(caption_data)
                    captions_added += 1
                
                # Update clip
                _get_app().updates.update(["clips", clip.id], clip_data)
                
            except Exception as e:
                log.error("Error transcribing clip {}: {}".format(clip_id, e))
                continue
        
        return "Added {} captions to {} clips".format(captions_added, len(clip_ids))
    except Exception as e:
        log.error("add_captions_to_clips: %s", e, exc_info=True)
        return "Error: {}".format(e)


def _add_effect_to_clip(clip, effect_json: Dict[str, Any]):
    """Add an effect to a clip"""
    if not isinstance(clip.data, dict):
        clip.data = {}
    
    effects = clip.data.get("effects")
    if not isinstance(effects, list):
        effects = list(effects) if effects else []
        clip.data["effects"] = effects
    
    effects.append(effect_json)
    
    # Update clip on main thread
    _get_app().updates.update(["clips", clip.id], clip.data)
