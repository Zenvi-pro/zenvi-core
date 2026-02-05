"""
@file
@brief Theme engine orchestrator for applying themes to clips
@author Zenvi Team

@section LICENSE

Copyright (c) 2024 Zenvi Core
This file is part of Zenvi Core video editor.
"""

import time
from typing import Dict, Any, List, Optional
from classes.logger import log
from classes.theme_loader import ThemeLoader
from classes.theme_applicator import (
    apply_color_grading,
    apply_film_grain,
    apply_sound_effects,
    add_captions_to_clips
)


class ThemeEngine:
    """
    Main orchestrator for theme application.
    Simple synchronous execution - no threading.
    """
    
    def __init__(self):
        """Initialize the theme engine"""
        self.theme_loader = ThemeLoader()
        self.current_theme: Optional[Dict[str, Any]] = None
        log.info("ThemeEngine initialized")
    
    def list_themes(self) -> List[Dict[str, str]]:
        """
        List all available themes
        
        Returns:
            List of theme info dicts
        """
        return self.theme_loader.list_available_themes()
    
    def load_theme(self, theme_id: str) -> Dict[str, Any]:
        """
        Load a theme by ID
        
        Args:
            theme_id: Theme identifier
        
        Returns:
            Theme data
        """
        self.current_theme = self.theme_loader.load_theme(theme_id)
        return self.current_theme
    
    def get_theme_info(self, theme_id: str) -> Optional[Dict[str, Any]]:
        """
        Get theme metadata
        
        Args:
            theme_id: Theme identifier
        
        Returns:
            Theme metadata or None
        """
        return self.theme_loader.get_theme_metadata(theme_id)
    
    def apply_theme(
        self,
        theme_id: str,
        clip_ids: Optional[List[str]] = None,
        apply_color: bool = True,
        apply_sound: bool = True,
        apply_captions: bool = False
    ) -> Dict[str, Any]:
        """
        Apply a theme to clips - runs synchronously on main thread
        
        Args:
            theme_id: Theme to apply
            clip_ids: Clip IDs to apply to (None = all clips)
            apply_color: Whether to apply color grading
            apply_sound: Whether to apply sound effects
            apply_captions: Whether to add captions
        
        Returns:
            Results dict with success status
        """
        try:
            start_time = time.time()
            
            # Load theme
            theme_data = self.load_theme(theme_id)
            
            # Get clips if not specified
            if clip_ids is None:
                clip_ids = self._get_all_clip_ids()
            
            if not clip_ids:
                return {
                    "success": False,
                    "error": "No clips found to apply theme to"
                }
            
            log.info("Applying theme '{}' to {} clips".format(theme_id, len(clip_ids)))
            
            results = []
            
            # Apply color grading
            if apply_color and "color_grading" in theme_data:
                log.info("Applying color grading...")
                result = apply_color_grading(clip_ids, theme_data["color_grading"])
                results.append(result)
                
                # Apply film grain if specified
                if "effects" in theme_data and "grain" in theme_data["effects"]:
                    grain_intensity = theme_data["effects"]["grain"].get("intensity", 0.3)
                    result = apply_film_grain(clip_ids, grain_intensity)
                    results.append(result)
            
            # Apply sound effects
            if apply_sound and "sound" in theme_data:
                log.info("Applying sound effects...")
                result = apply_sound_effects(clip_ids, theme_data["sound"])
                results.append(result)
            
            # Add captions
            if apply_captions:
                log.info("Adding captions...")
                caption_config = theme_data.get("captions", {})
                result = add_captions_to_clips(clip_ids, caption_config)
                results.append(result)
            
            elapsed = time.time() - start_time
            
            log.info("Theme '{}' applied successfully in {:.1f}s".format(theme_id, elapsed))
            
            return {
                "success": True,
                "theme_id": theme_id,
                "clips_processed": len(clip_ids),
                "elapsed_time": elapsed,
                "results": results
            }
            
        except Exception as e:
            log.error("Error applying theme: {}".format(e), exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_all_clip_ids(self) -> List[str]:
        """Get all clip IDs in the project"""
        try:
            from classes.query import Clip
            clips = Clip.filter()
            return [c.id for c in clips]
        except Exception as e:
            log.error("Error getting clip IDs: {}".format(e))
            return []
    
    def get_selected_clip_ids(self) -> List[str]:
        """Get currently selected clip IDs"""
        try:
            from classes.app import get_app
            app = get_app()
            
            # Get selected items from timeline
            if hasattr(app, 'window') and hasattr(app.window, 'selected_clips'):
                return app.window.selected_clips
            
            # Fallback: return empty list
            return []
            
        except Exception as e:
            log.error("Error getting selected clips: {}".format(e))
            return []
