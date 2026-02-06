"""
@file
@brief Qt Worker for theme processing with parallel clip processing
@author Zenvi Team

@section LICENSE

Copyright (c) 2024 Zenvi Core
This file is part of Zenvi Core video editor.
"""

import json
import os
import tempfile
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from classes.logger import log

try:
    from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot
except ImportError:
    # Fallback for non-Qt environments
    QObject = object
    def pyqtSignal(*args, **kwargs):
        return None
    def pyqtSlot(*args, **kwargs):
        def decorator(func):
            return func
        return decorator


class ThemeWorker(QObject if QObject is not object else object):
    """
    Background worker for theme processing.
    Runs computation in worker threads, emits results for main thread to apply.
    """
    
    # Qt signals for cross-thread communication
    progress = pyqtSignal(int, str)  # (progress_percent, status_message)
    finished = pyqtSignal(dict)       # (results_dict)
    error = pyqtSignal(str)           # (error_message)
    
    def __init__(self, theme_data: Dict[str, Any], clip_ids: List[str], options: Dict[str, bool]):
        """
        Initialize theme worker
        
        Args:
            theme_data: Theme configuration (color_grading, effects, sound, captions)
            clip_ids: List of clip IDs to process
            options: Dict with apply_color, apply_sound, apply_captions flags
        """
        if QObject is not object:
            super().__init__()
        
        self.theme_data = theme_data
        self.clip_ids = clip_ids
        self.options = options
        self.cancelled = False
        
        log.info("ThemeWorker initialized for {} clips".format(len(clip_ids)))
    
    @pyqtSlot()
    def process(self):
        """
        Main processing method - runs on worker thread.
        Does NOT make any Qt calls. Only computation and API calls.
        """
        try:
            log.info("ThemeWorker.process started")
            
            results = {
                "effects": [],      # List of (clip_id, effect_json) tuples
                "captions": [],     # List of (clip_id, caption_data) tuples
                "audio_effects": [] # List of (clip_id, audio_config) tuples
            }
            
            total_clips = len(self.clip_ids)
            
            # Use ThreadPoolExecutor for parallel clip processing
            max_workers = min(4, total_clips)  # Cap at 4 workers
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all clips for processing
                futures = {}
                for clip_id in self.clip_ids:
                    if self.cancelled:
                        break
                    future = executor.submit(self._process_clip, clip_id)
                    futures[future] = clip_id
                
                # Collect results as they complete
                completed = 0
                for future in as_completed(futures):
                    if self.cancelled:
                        break
                    
                    clip_id = futures[future]
                    try:
                        clip_result = future.result()
                        
                        # Merge results
                        results["effects"].extend(clip_result.get("effects", []))
                        results["captions"].extend(clip_result.get("captions", []))
                        results["audio_effects"].extend(clip_result.get("audio_effects", []))
                        
                        # Emit progress
                        completed += 1
                        progress_pct = int((completed / total_clips) * 100)
                        self.progress.emit(progress_pct, "Processed {}/{}".format(completed, total_clips))
                        
                    except Exception as e:
                        log.error("Error processing clip {}: {}".format(clip_id, e), exc_info=True)
                        # Continue with other clips
            
            if self.cancelled:
                self.error.emit("Processing cancelled")
            else:
                log.info("ThemeWorker.process finished successfully")
                self.finished.emit(results)
                
        except Exception as e:
            log.error("ThemeWorker.process failed: {}".format(e), exc_info=True)
            self.error.emit(str(e))
    
    def _process_clip(self, clip_id: str) -> Dict[str, Any]:
        """
        Process a single clip - NO Qt calls here!
        
        Args:
            clip_id: Clip ID to process
        
        Returns:
            Dict with effects, captions, audio_effects data
        """
        result = {
            "effects": [],
            "captions": [],
            "audio_effects": []
        }
        
        try:
            # Get clip data (read-only, no Qt calls)
            clip_info = self._get_clip_info(clip_id)
            if not clip_info:
                return result
            
            # Process color grading
            if self.options.get("apply_color", True) and "color_grading" in self.theme_data:
                color_effects = self._compute_color_grading(clip_id, clip_info, self.theme_data["color_grading"])
                result["effects"].extend(color_effects)
            
            # Process visual effects (grain, vignette, etc)
            if self.options.get("apply_color", True) and "effects" in self.theme_data:
                visual_effects = self._compute_visual_effects(clip_id, clip_info, self.theme_data["effects"])
                result["effects"].extend(visual_effects)
            
            # Process sound
            if self.options.get("apply_sound", True) and "sound" in self.theme_data:
                audio_config = self._compute_audio_effects(clip_id, clip_info, self.theme_data["sound"])
                if audio_config:
                    result["audio_effects"].append((clip_id, audio_config))
            
            # Process captions
            if self.options.get("apply_captions", False):
                caption_data = self._transcribe_audio(clip_id, clip_info, self.theme_data.get("captions", {}))
                if caption_data:
                    result["captions"].append((clip_id, caption_data))
            
        except Exception as e:
            log.error("_process_clip {} failed: {}".format(clip_id, e), exc_info=True)
        
        return result
    
    def _get_clip_info(self, clip_id: str) -> Optional[Dict[str, Any]]:
        """Get clip information without Qt calls"""
        try:
            # Import here to avoid Qt calls during init
            from classes.query import Clip
            clip = Clip.get(id=clip_id)
            if not clip:
                return None
            
            # Extract data we need (no Qt modifications)
            clip_data = clip.data if isinstance(clip.data, dict) else {}
            reader = clip_data.get("reader", {})
            
            return {
                "id": clip_id,
                "data": clip_data,
                "file_path": reader.get("path") if isinstance(reader, dict) else None,
                "has_audio": clip_data.get("has_audio", True)
            }
        except Exception as e:
            log.error("_get_clip_info failed: {}".format(e))
            return None
    
    def _compute_color_grading(self, clip_id: str, clip_info: Dict, color_config: Dict) -> List[tuple]:
        """Compute color grading effect parameters (no Qt calls)"""
        effects = []
        
        try:
            import openshot
            
            # Brightness/Contrast
            if "brightness" in color_config or "contrast" in color_config:
                brightness = color_config.get("brightness", 1.0)
                contrast = color_config.get("contrast", 1.0)
                
                effect = openshot.EffectInfo().CreateEffect("Brightness")
                effect.Id("temp_id_brightness_{}".format(clip_id))
                effect_json = json.loads(effect.Json())
                
                if "brightness" in effect_json:
                    effect_json["brightness"]["value"] = brightness
                if "contrast" in effect_json:
                    openshot_contrast = (contrast - 1.0) * 3 + 3
                    effect_json["contrast"]["value"] = max(0, min(20, openshot_contrast))
                
                effects.append((clip_id, effect_json))
            
            # Saturation
            if "saturation" in color_config:
                saturation = color_config.get("saturation", 1.0)
                
                effect = openshot.EffectInfo().CreateEffect("Saturation")
                effect.Id("temp_id_saturation_{}".format(clip_id))
                effect_json = json.loads(effect.Json())
                
                if "saturation" in effect_json:
                    effect_json["saturation"]["value"] = saturation
                
                effects.append((clip_id, effect_json))
            
            # Hue Shift
            if "hue_shift" in color_config and color_config["hue_shift"] != 0:
                hue_shift = color_config.get("hue_shift", 0)
                
                effect = openshot.EffectInfo().CreateEffect("Hue")
                effect.Id("temp_id_hue_{}".format(clip_id))
                effect_json = json.loads(effect.Json())
                
                if "hue" in effect_json:
                    effect_json["hue"]["value"] = hue_shift
                
                effects.append((clip_id, effect_json))
                
        except Exception as e:
            log.error("_compute_color_grading failed: {}".format(e), exc_info=True)
        
        return effects
    
    def _compute_visual_effects(self, clip_id: str, clip_info: Dict, effects_config: Dict) -> List[tuple]:
        """Compute visual effects (grain, vignette) parameters"""
        effects = []
        
        try:
            import openshot
            
            # Film grain
            if "grain" in effects_config:
                grain_config = effects_config["grain"]
                intensity = grain_config.get("intensity", 0.3)
                
                effect = openshot.EffectInfo().CreateEffect("Noise")
                effect.Id("temp_id_grain_{}".format(clip_id))
                effect_json = json.loads(effect.Json())
                
                if "level" in effect_json:
                    effect_json["level"]["value"] = intensity
                
                effects.append((clip_id, effect_json))
                
        except Exception as e:
            log.error("_compute_visual_effects failed: {}".format(e), exc_info=True)
        
        return effects
    
    def _compute_audio_effects(self, clip_id: str, clip_info: Dict, sound_config: Dict) -> Optional[Dict]:
        """Compute audio effect parameters (stored as metadata)"""
        if not clip_info.get("has_audio"):
            return None
        
        audio_data = {
            "eq": sound_config.get("eq", {}),
            "bass_boost_db": sound_config.get("bass_boost_db", 0),
            "reverb": sound_config.get("reverb", {}),
            "pitch_shift": sound_config.get("pitch_shift", 0)
        }
        
        return audio_data
    
    def _transcribe_audio(self, clip_id: str, clip_info: Dict, caption_config: Dict) -> Optional[List[Dict]]:
        """Transcribe audio using Whisper API (can be slow)"""
        file_path = clip_info.get("file_path")
        if not file_path or not os.path.exists(file_path):
            return None
        
        # Check if file has audio
        if not clip_info.get("has_audio"):
            return None
        
        try:
            # Get API key
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                # Try to get from app settings (read-only access)
                try:
                    from classes.app import get_app
                    app = get_app()
                    if app and hasattr(app, '_data'):
                        api_key = app._data.get("openai_whisper_api_key") or app._data.get("openai-api-key")
                except:
                    pass
            
            if not api_key:
                log.warning("OpenAI API key not found, skipping captions")
                return None
            
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            
            # Transcribe
            with open(file_path, "rb") as audio_file:
                response = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"]
                )
            
            if not response.segments:
                return None
            
            # Convert to caption format
            captions = []
            for seg in response.segments:
                caption_data = {
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text.strip(),
                    **caption_config  # Add theme styling
                }
                captions.append(caption_data)
            
            return captions
            
        except ImportError:
            log.warning("openai package not installed, skipping captions")
            return None
        except Exception as e:
            log.error("_transcribe_audio failed: {}".format(e), exc_info=True)
            return None
    
    def cancel(self):
        """Cancel processing"""
        self.cancelled = True
        log.info("ThemeWorker cancellation requested")
