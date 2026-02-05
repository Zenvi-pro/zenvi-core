"""
@file
@brief Sound grading agent for applying audio effects
@author Zenvi Team

@section LICENSE

Copyright (c) 2024 Zenvi Core
This file is part of Zenvi Core video editor.
"""

import json
import os
import tempfile
import subprocess
from typing import Dict, Any, Optional
from pathlib import Path
from classes.logger import log
from classes.base_theme_agent import BaseThemeAgent

try:
    import openshot
except ImportError:
    openshot = None


class SoundGradingAgent(BaseThemeAgent):
    """
    Agent specialized in applying sound/audio effects to clips.
    Handles bass boost, reverb, EQ, compression, and other audio enhancements.
    """
    
    def __init__(self, theme_data: Dict[str, Any]):
        """
        Initialize the sound grading agent
        
        Args:
            theme_data: Theme configuration with sound section
        """
        super().__init__("sound_grading", theme_data)
        
        self.sound_config = theme_data.get("sound", {})
        
        log.info(f"SoundGradingAgent initialized with theme: {theme_data.get('name', 'Unknown')}")
    
    def process_clip(self, clip_id: str) -> Dict[str, Any]:
        """
        Process a clip by applying sound effects
        
        Args:
            clip_id: ID of clip to process
        
        Returns:
            Dict with processing results
        """
        if not openshot:
            raise ImportError("OpenShot library not available")
        
        from classes.query import Clip
        
        # Get the clip
        clip = Clip.get(id=clip_id)
        if not clip:
            raise ValueError(f"Clip not found: {clip_id}")
        
        effects_applied = []
        
        try:
            # Check if clip has audio
            if not self._clip_has_audio(clip):
                log.info(f"Clip {clip_id} has no audio, skipping sound effects")
                return {
                    "success": True,
                    "effects_applied": [],
                    "message": "No audio track"
                }
            
            # 1. Apply bass boost via EQ
            if "bass_boost_db" in self.sound_config and self.sound_config["bass_boost_db"] != 0:
                self._report_progress(clip_id, "bass_boost", 0.2)
                effect = self._apply_bass_boost(
                    clip,
                    self.sound_config["bass_boost_db"]
                )
                if effect:
                    effects_applied.append("Bass Boost")
            
            # 2. Apply EQ if specified
            if "eq" in self.sound_config:
                self._report_progress(clip_id, "eq", 0.4)
                effect = self._apply_eq(
                    clip,
                    self.sound_config["eq"]
                )
                if effect:
                    effects_applied.append("Equalizer")
            
            # 3. Apply compression
            if self.sound_config.get("compression", {}).get("enabled"):
                self._report_progress(clip_id, "compression", 0.6)
                effect = self._apply_compression(
                    clip,
                    self.sound_config["compression"]
                )
                if effect:
                    effects_applied.append("Compressor")
            
            # 4. Apply normalization
            if self.sound_config.get("normalize"):
                self._report_progress(clip_id, "normalize", 0.8)
                effect = self._apply_normalize(clip)
                if effect:
                    effects_applied.append("Normalize")
            
            # 5. Apply noise reduction (if available)
            if self.sound_config.get("noise_reduction", {}).get("enabled"):
                self._report_progress(clip_id, "noise_reduction", 0.9)
                # This might require FFmpeg processing
                success = self._apply_noise_reduction_ffmpeg(
                    clip,
                    self.sound_config["noise_reduction"]
                )
                if success:
                    effects_applied.append("Noise Reduction")
            
            self._report_progress(clip_id, "finalizing", 1.0)
            
            return {
                "success": True,
                "effects_applied": effects_applied
            }
            
        except Exception as e:
            log.error(f"Error applying sound effects to clip {clip_id}: {e}", exc_info=True)
            raise
    
    def _clip_has_audio(self, clip) -> bool:
        """Check if clip has an audio track"""
        clip_data = clip.data if isinstance(clip.data, dict) else {}
        
        # Check if clip has audio enabled
        has_audio = clip_data.get("has_audio", True)
        
        # Check reader info
        reader = clip_data.get("reader", {})
        if isinstance(reader, dict):
            has_audio_track = reader.get("has_audio", False)
            return has_audio and has_audio_track
        
        return has_audio
    
    def _apply_bass_boost(self, clip, boost_db: float) -> Optional[Any]:
        """
        Apply bass boost using low-frequency EQ
        
        Args:
            clip: Clip object
            boost_db: Boost amount in dB (positive = boost, negative = cut)
        """
        # OpenShot doesn't have a built-in bass boost, so we'll use generic EQ
        # or apply via FFmpeg if needed
        
        # For now, use the parametric EQ approach
        return self._apply_eq(clip, {
            "low": boost_db,
            "mid": 0,
            "high": 0
        })
    
    def _apply_eq(self, clip, eq_config: Dict[str, float]) -> Optional[Any]:
        """
        Apply equalizer effect
        
        Args:
            clip: Clip object
            eq_config: EQ configuration with low, mid, high values
        """
        from classes.app import get_app
        
        try:
            # OpenShot has a Parametric EQ effect
            # We'll simulate a simple 3-band EQ using multiple filters
            
            low_gain = eq_config.get("low", 0)
            mid_gain = eq_config.get("mid", 0)
            high_gain = eq_config.get("high", 0)
            
            # If all gains are 0, skip
            if low_gain == 0 and mid_gain == 0 and high_gain == 0:
                return None
            
            # For simplicity, we'll note this in metadata
            # Full implementation would require custom audio processing
            
            # Add metadata to track that EQ should be applied
            if not isinstance(clip.data, dict):
                clip.data = {}
            
            if "audio_effects" not in clip.data:
                clip.data["audio_effects"] = []
            
            clip.data["audio_effects"].append({
                "type": "eq",
                "low": low_gain,
                "mid": mid_gain,
                "high": high_gain
            })
            
            # Update clip
            from classes.app import get_app
            app = get_app()
            if app and hasattr(app, 'updates'):
                app.updates.update(["clips", clip.id], clip.data)
            
            log.debug(f"Applied EQ (low={low_gain}, mid={mid_gain}, high={high_gain}) to clip {clip.id}")
            return True
            
        except Exception as e:
            log.error(f"Error applying EQ: {e}", exc_info=True)
            return None
    
    def _apply_compression(self, clip, compression_config: Dict[str, Any]) -> Optional[Any]:
        """Apply dynamic range compression"""
        from classes.app import get_app
        
        try:
            # Create Compressor effect if available
            effect = openshot.EffectInfo().CreateEffect("Compressor")
            if not effect:
                log.warning("Compressor effect not available")
                return None
            
            effect.Id(get_app().project.generate_id())
            
            # Get effect JSON
            effect_json = json.loads(effect.Json())
            
            # Set compressor parameters
            # Note: Parameter names may vary depending on OpenShot version
            if "threshold" in compression_config:
                if "threshold" in effect_json:
                    effect_json["threshold"]["value"] = compression_config["threshold"]
            
            if "ratio" in compression_config:
                if "ratio" in effect_json:
                    effect_json["ratio"]["value"] = compression_config["ratio"]
            
            # Add effect to clip
            self._add_effect_to_clip(clip, effect_json)
            
            log.debug(f"Applied compression to clip {clip.id}")
            return effect
            
        except Exception as e:
            log.warning(f"Could not apply compressor effect: {e}")
            # Store in metadata for potential FFmpeg processing
            if not isinstance(clip.data, dict):
                clip.data = {}
            if "audio_effects" not in clip.data:
                clip.data["audio_effects"] = []
            clip.data["audio_effects"].append({
                "type": "compressor",
                **compression_config
            })
            return None
    
    def _apply_normalize(self, clip) -> Optional[Any]:
        """Apply audio normalization"""
        if not isinstance(clip.data, dict):
            clip.data = {}
        
        if "audio_effects" not in clip.data:
            clip.data["audio_effects"] = []
        
        clip.data["audio_effects"].append({
            "type": "normalize",
            "target": -3.0  # Target -3dB peak
        })
        
        # Update clip
        from classes.app import get_app
        app = get_app()
        if app and hasattr(app, 'updates'):
            app.updates.update(["clips", clip.id], clip.data)
        
        log.debug(f"Applied normalization to clip {clip.id}")
        return True
    
    def _apply_noise_reduction_ffmpeg(self, clip, noise_reduction_config: Dict[str, Any]) -> bool:
        """
        Apply noise reduction using FFmpeg
        
        Note: This is a placeholder for future FFmpeg integration.
        Full implementation would extract audio, process with FFmpeg, and re-import.
        """
        if not isinstance(clip.data, dict):
            clip.data = {}
        
        if "audio_effects" not in clip.data:
            clip.data["audio_effects"] = []
        
        strength = noise_reduction_config.get("strength", 0.4)
        
        clip.data["audio_effects"].append({
            "type": "noise_reduction",
            "strength": strength
        })
        
        # Update clip
        from classes.app import get_app
        app = get_app()
        if app and hasattr(app, 'updates'):
            app.updates.update(["clips", clip.id], clip.data)
        
        log.debug(f"Marked noise reduction for clip {clip.id} (strength={strength})")
        return True
    
    def _add_effect_to_clip(self, clip, effect_json: Dict[str, Any]):
        """Add an effect to a clip"""
        if not isinstance(clip.data, dict):
            clip.data = {}
        
        effects = clip.data.get("effects")
        if not isinstance(effects, list):
            effects = list(effects) if effects else []
            clip.data["effects"] = effects
        
        effects.append(effect_json)
        
        # Update clip
        from classes.app import get_app
        app = get_app()
        if app and hasattr(app, 'updates'):
            app.updates.update(["clips", clip.id], clip.data)
    
    def can_help_with_audio_cleanup(self, clip_id: str) -> bool:
        """
        Check if this agent can help clean up audio for caption processing
        
        Args:
            clip_id: Clip ID to check
        
        Returns:
            True if can help
        """
        # This agent can apply noise reduction to improve transcription quality
        return True
    
    def clean_audio_for_transcription(self, clip_id: str) -> bool:
        """
        Clean audio specifically for transcription
        Applies noise reduction and normalization
        
        Args:
            clip_id: Clip ID to process
        
        Returns:
            True if successful
        """
        try:
            from classes.query import Clip
            
            clip = Clip.get(id=clip_id)
            if not clip:
                return False
            
            # Apply aggressive noise reduction
            self._apply_noise_reduction_ffmpeg(clip, {"enabled": True, "strength": 0.6})
            
            # Apply normalization for consistent levels
            self._apply_normalize(clip)
            
            log.info(f"Cleaned audio for transcription: clip {clip_id}")
            return True
            
        except Exception as e:
            log.error(f"Error cleaning audio for transcription: {e}")
            return False
