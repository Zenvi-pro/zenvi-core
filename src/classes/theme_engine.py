"""
@file
@brief Theme engine orchestrator for applying themes to clips
@author Zenvi Team

@section LICENSE

Copyright (c) 2024 Zenvi Core
This file is part of Zenvi Core video editor.
"""

from typing import Dict, Any, List, Optional
from classes.logger import log
from classes.theme_loader import ThemeLoader
from classes.coordinator_agent import CoordinatorAgent


class ThemeEngine:
    """
    Main orchestrator for theme application.
    Loads themes and coordinates multi-agent theme application.
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
        Apply a theme to clips
        
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
            
            log.info(f"Applying theme '{theme_id}' to {len(clip_ids)} clips")
            
            # Create coordinator
            components = {
                "color": apply_color,
                "sound": apply_sound,
                "captions": apply_captions
            }
            
            coordinator = CoordinatorAgent(theme_data, clip_ids, components)
            
            # Execute
            result = coordinator.execute()
            
            if result.get("success"):
                log.info(f"Theme '{theme_id}' applied successfully")
            else:
                log.error(f"Theme application failed: {result.get('error')}")
            
            return result
            
        except Exception as e:
            log.error(f"Error applying theme: {e}", exc_info=True)
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
            log.error(f"Error getting clip IDs: {e}")
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
            log.error(f"Error getting selected clips: {e}")
            return []
