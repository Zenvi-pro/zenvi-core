"""
@file
@brief Color grading agent for applying visual effects
@author Zenvi Team

@section LICENSE

Copyright (c) 2024 Zenvi Core
This file is part of Zenvi Core video editor.
"""

import json
from typing import Dict, Any, Optional
from classes.logger import log
from classes.base_theme_agent import BaseThemeAgent

try:
    import openshot
except ImportError:
    openshot = None


class ColorGradingAgent(BaseThemeAgent):
    """
    Agent specialized in applying color grading effects to clips.
    Handles brightness, contrast, saturation, hue shifts, and visual effects like grain.
    """
    
    def __init__(self, theme_data: Dict[str, Any]):
        """
        Initialize the color grading agent
        
        Args:
            theme_data: Theme configuration with color_grading and effects sections
        """
        super().__init__("color_grading", theme_data)
        
        self.color_grading_config = theme_data.get("color_grading", {})
        self.effects_config = theme_data.get("effects", {})
        self.scene_adaptations = theme_data.get("scene_adaptations", {})
        
        log.info(f"ColorGradingAgent initialized with theme: {theme_data.get('name', 'Unknown')}")
    
    def process_clip(self, clip_id: str) -> Dict[str, Any]:
        """
        Process a clip by applying color grading effects
        
        Args:
            clip_id: ID of clip to process
        
        Returns:
            Dict with processing results
        """
        if not openshot:
            raise ImportError("OpenShot library not available")
        
        from classes.query import Clip
        from classes.app import get_app
        
        # Get the clip
        clip = Clip.get(id=clip_id)
        if not clip:
            raise ValueError(f"Clip not found: {clip_id}")
        
        effects_applied = []
        
        try:
            # Get scene type for adaptations (if available)
            scene_type = self._detect_scene_type(clip)
            adapted_config = self._apply_scene_adaptations(self.color_grading_config, scene_type)
            
            # 1. Apply brightness/contrast
            if "brightness" in adapted_config or "contrast" in adapted_config:
                self._report_progress(clip_id, "brightness_contrast", 0.2)
                effect = self._apply_brightness_contrast(
                    clip,
                    adapted_config.get("brightness", 1.0),
                    adapted_config.get("contrast", 1.0)
                )
                if effect:
                    effects_applied.append("Brightness")
            
            # 2. Apply saturation
            if "saturation" in adapted_config:
                self._report_progress(clip_id, "saturation", 0.4)
                effect = self._apply_saturation(
                    clip,
                    adapted_config.get("saturation", 1.0)
                )
                if effect:
                    effects_applied.append("Saturation")
            
            # 3. Apply hue shift
            if "hue_shift" in adapted_config and adapted_config["hue_shift"] != 0:
                self._report_progress(clip_id, "hue", 0.6)
                effect = self._apply_hue(
                    clip,
                    adapted_config.get("hue_shift", 0)
                )
                if effect:
                    effects_applied.append("Hue")
            
            # 4. Apply grain/noise
            grain_config = self.effects_config.get("grain")
            if grain_config and grain_config.get("intensity", 0) > 0:
                self._report_progress(clip_id, "grain", 0.8)
                # Apply scene adaptations to grain too
                adapted_effects = self._apply_scene_adaptations(self.effects_config, scene_type)
                adapted_grain = adapted_effects.get("grain", grain_config)
                
                effect = self._apply_grain(
                    clip,
                    adapted_grain.get("intensity", 0.3)
                )
                if effect:
                    effects_applied.append("Noise")
            
            # 5. Apply other effects (blur, sharpen, etc.)
            if "sharpen" in self.effects_config:
                self._report_progress(clip_id, "sharpen", 0.9)
                effect = self._apply_sharpen(
                    clip,
                    self.effects_config["sharpen"].get("amount", 0.5)
                )
                if effect:
                    effects_applied.append("Sharpen")
            
            self._report_progress(clip_id, "finalizing", 1.0)
            
            return {
                "success": True,
                "effects_applied": effects_applied,
                "scene_type": scene_type
            }
            
        except Exception as e:
            log.error(f"Error applying color grading to clip {clip_id}: {e}", exc_info=True)
            raise
    
    def _apply_brightness_contrast(self, clip, brightness: float, contrast: float) -> Optional[Any]:
        """Apply brightness and contrast effect"""
        from classes.app import get_app
        
        try:
            # Create Brightness effect
            effect = openshot.EffectInfo().CreateEffect("Brightness")
            effect.Id(get_app().project.generate_id())
            
            # Get effect JSON
            effect_json = json.loads(effect.Json())
            
            # Set brightness value (OpenShot uses 0-2 range, 1.0 = normal)
            if "brightness" in effect_json:
                effect_json["brightness"]["value"] = brightness
            
            # Set contrast value (OpenShot uses 0-20 range, 3 = normal)
            # Convert our 1.0-based value to OpenShot's scale
            if "contrast" in effect_json:
                openshot_contrast = (contrast - 1.0) * 3 + 3
                effect_json["contrast"]["value"] = max(0, min(20, openshot_contrast))
            
            # Add effect to clip
            self._add_effect_to_clip(clip, effect_json)
            
            log.debug(f"Applied brightness={brightness}, contrast={contrast} to clip {clip.id}")
            return effect
            
        except Exception as e:
            log.error(f"Error applying brightness/contrast: {e}", exc_info=True)
            return None
    
    def _apply_saturation(self, clip, saturation: float) -> Optional[Any]:
        """Apply saturation effect"""
        from classes.app import get_app
        
        try:
            # Create Saturation effect
            effect = openshot.EffectInfo().CreateEffect("Saturation")
            effect.Id(get_app().project.generate_id())
            
            # Get effect JSON
            effect_json = json.loads(effect.Json())
            
            # Set saturation value (OpenShot uses 0-4 range, 1.0 = normal)
            if "saturation" in effect_json:
                effect_json["saturation"]["value"] = saturation
            
            # Add effect to clip
            self._add_effect_to_clip(clip, effect_json)
            
            log.debug(f"Applied saturation={saturation} to clip {clip.id}")
            return effect
            
        except Exception as e:
            log.error(f"Error applying saturation: {e}", exc_info=True)
            return None
    
    def _apply_hue(self, clip, hue_shift: float) -> Optional[Any]:
        """Apply hue shift effect"""
        from classes.app import get_app
        
        try:
            # Create Hue effect
            effect = openshot.EffectInfo().CreateEffect("Hue")
            effect.Id(get_app().project.generate_id())
            
            # Get effect JSON
            effect_json = json.loads(effect.Json())
            
            # Set hue value (OpenShot uses degrees)
            if "hue" in effect_json:
                effect_json["hue"]["value"] = hue_shift
            
            # Add effect to clip
            self._add_effect_to_clip(clip, effect_json)
            
            log.debug(f"Applied hue_shift={hue_shift} to clip {clip.id}")
            return effect
            
        except Exception as e:
            log.error(f"Error applying hue: {e}", exc_info=True)
            return None
    
    def _apply_grain(self, clip, intensity: float) -> Optional[Any]:
        """Apply grain/noise effect"""
        from classes.app import get_app
        
        try:
            # Create Noise effect
            effect = openshot.EffectInfo().CreateEffect("Noise")
            effect.Id(get_app().project.generate_id())
            
            # Get effect JSON
            effect_json = json.loads(effect.Json())
            
            # Set noise level (convert 0-1 intensity to OpenShot's scale)
            if "level" in effect_json:
                effect_json["level"]["value"] = intensity
            
            # Add effect to clip
            self._add_effect_to_clip(clip, effect_json)
            
            log.debug(f"Applied grain intensity={intensity} to clip {clip.id}")
            return effect
            
        except Exception as e:
            log.error(f"Error applying grain: {e}", exc_info=True)
            return None
    
    def _apply_sharpen(self, clip, amount: float) -> Optional[Any]:
        """Apply sharpen effect"""
        from classes.app import get_app
        
        try:
            # Create Sharpen effect
            effect = openshot.EffectInfo().CreateEffect("Sharpen")
            effect.Id(get_app().project.generate_id())
            
            # Get effect JSON
            effect_json = json.loads(effect.Json())
            
            # Set sharpen sigma value
            if "sigma" in effect_json:
                effect_json["sigma"]["value"] = amount
            
            # Add effect to clip
            self._add_effect_to_clip(clip, effect_json)
            
            log.debug(f"Applied sharpen amount={amount} to clip {clip.id}")
            return effect
            
        except Exception as e:
            log.error(f"Error applying sharpen: {e}", exc_info=True)
            return None
    
    def _add_effect_to_clip(self, clip, effect_json: Dict[str, Any]):
        """Add an effect to a clip"""
        # Ensure clip.data is a dict
        if not isinstance(clip.data, dict):
            clip.data = {}
        
        # Get existing effects
        effects = clip.data.get("effects")
        if not isinstance(effects, list):
            effects = list(effects) if effects else []
            clip.data["effects"] = effects
        
        # Add new effect
        effects.append(effect_json)
        
        # Update clip data
        # Note: This will be called from main thread via ThemeEngine
        from classes.app import get_app
        app = get_app()
        if app and hasattr(app, 'updates'):
            app.updates.update(["clips", clip.id], clip.data)
    
    def _detect_scene_type(self, clip) -> str:
        """
        Detect scene type from clip metadata (if available)
        
        Args:
            clip: Clip object
        
        Returns:
            Scene type string ('indoor', 'outdoor', 'night', etc.)
        """
        # Check if clip has AI metadata with scene detection
        clip_data = clip.data if isinstance(clip.data, dict) else {}
        
        # Look for AI-generated scene tags
        if "ai_metadata" in clip_data:
            metadata = clip_data["ai_metadata"]
            if "scene_type" in metadata:
                return metadata["scene_type"]
        
        # Default to general
        return "general"
    
    def _apply_scene_adaptations(self, config: Dict[str, Any], scene_type: str) -> Dict[str, Any]:
        """
        Apply scene-specific adaptations to configuration
        
        Args:
            config: Base configuration
            scene_type: Detected scene type
        
        Returns:
            Adapted configuration
        """
        if not scene_type or scene_type == "general":
            return config.copy()
        
        # Get scene adaptation if available
        adaptation = self.scene_adaptations.get(scene_type, {})
        
        # Merge adaptation with base config
        adapted = config.copy()
        for key, value in adaptation.items():
            if isinstance(value, dict) and key in adapted and isinstance(adapted[key], dict):
                # Merge nested dicts
                adapted[key] = {**adapted[key], **value}
            else:
                # Override value
                adapted[key] = value
        
        return adapted
