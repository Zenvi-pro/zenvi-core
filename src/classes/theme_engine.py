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

try:
    from PyQt5.QtCore import QThread, QEventLoop
except ImportError:
    QThread = None
    QEventLoop = None


class ThemeEngine:
    """
    Main orchestrator for theme application.
    Simple synchronous execution - no threading.
    """
    
    def __init__(self):
        """Initialize the theme engine"""
        self.theme_loader = ThemeLoader()
        self.current_theme: Optional[Dict[str, Any]] = None
        self.worker = None
        self.thread = None
        self.last_results = None
        self.last_error = None
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
        Apply a theme to clips - uses worker threads for computation,
        blocks until complete, then applies results on main thread
        
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
            
            log.info("Applying theme '{}' to {} clips with threading".format(theme_id, len(clip_ids)))
            
            # Use worker thread for computation
            if QThread is not None and QEventLoop is not None:
                result = self._apply_theme_with_worker(
                    theme_data, clip_ids, apply_color, apply_sound, apply_captions
                )
            else:
                # Fallback to synchronous if Qt not available
                result = self._apply_theme_sync(
                    theme_data, clip_ids, apply_color, apply_sound, apply_captions
                )
            
            elapsed = time.time() - start_time
            result["elapsed_time"] = elapsed
            
            if result.get("success"):
                log.info("Theme '{}' applied successfully in {:.1f}s".format(theme_id, elapsed))
            
            return result
            
        except Exception as e:
            log.error("Error applying theme: {}".format(e), exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }
    
    def _apply_theme_with_worker(
        self,
        theme_data: Dict[str, Any],
        clip_ids: List[str],
        apply_color: bool,
        apply_sound: bool,
        apply_captions: bool
    ) -> Dict[str, Any]:
        """Apply theme using worker thread (blocking until complete)"""
        from classes.theme_worker import ThemeWorker
        
        # Reset state
        self.last_results = None
        self.last_error = None
        
        # Create worker and thread
        self.thread = QThread()
        self.worker = ThemeWorker(
            theme_data=theme_data,
            clip_ids=clip_ids,
            options={
                "apply_color": apply_color,
                "apply_sound": apply_sound,
                "apply_captions": apply_captions
            }
        )
        self.worker.moveToThread(self.thread)
        
        # Connect signals
        self.thread.started.connect(self.worker.process)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.error.connect(self._on_worker_error)
        self.worker.progress.connect(self._on_worker_progress)
        
        # Create event loop to wait for completion
        loop = QEventLoop()
        self.worker.finished.connect(loop.quit)
        self.worker.error.connect(loop.quit)
        
        # Start processing
        self.thread.start()
        
        # Wait for completion (blocks here but allows Qt events)
        loop.exec_()
        
        # Clean up thread
        self.thread.quit()
        self.thread.wait(5000)  # Wait up to 5 seconds
        
        # Check results
        if self.last_error:
            return {
                "success": False,
                "error": self.last_error
            }
        
        if not self.last_results:
            return {
                "success": False,
                "error": "No results from worker"
            }
        
        # Apply results on main thread
        try:
            self._apply_results_on_main_thread(self.last_results)
            return {
                "success": True,
                "clips_processed": len(clip_ids),
                "effects_applied": len(self.last_results.get("effects", [])),
                "captions_added": len(self.last_results.get("captions", []))
            }
        except Exception as e:
            log.error("Failed to apply results: {}".format(e), exc_info=True)
            return {
                "success": False,
                "error": "Failed to apply results: {}".format(e)
            }
    
    def _apply_theme_sync(
        self,
        theme_data: Dict[str, Any],
        clip_ids: List[str],
        apply_color: bool,
        apply_sound: bool,
        apply_captions: bool
    ) -> Dict[str, Any]:
        """Fallback synchronous theme application (no threading)"""
        from classes.theme_applicator import (
            apply_color_grading,
            apply_film_grain,
            apply_sound_effects,
            add_captions_to_clips
        )
        
        results = []
        
        # Apply color grading
        if apply_color and "color_grading" in theme_data:
            result = apply_color_grading(clip_ids, theme_data["color_grading"])
            results.append(result)
            
            if "effects" in theme_data and "grain" in theme_data["effects"]:
                grain_intensity = theme_data["effects"]["grain"].get("intensity", 0.3)
                result = apply_film_grain(clip_ids, grain_intensity)
                results.append(result)
        
        # Apply sound effects
        if apply_sound and "sound" in theme_data:
            result = apply_sound_effects(clip_ids, theme_data["sound"])
            results.append(result)
        
        # Add captions
        if apply_captions:
            caption_config = theme_data.get("captions", {})
            result = add_captions_to_clips(clip_ids, caption_config)
            results.append(result)
        
        return {
            "success": True,
            "clips_processed": len(clip_ids),
            "results": results
        }
    
    def _on_worker_finished(self, results: Dict[str, Any]):
        """Handle worker finished signal (runs on main thread)"""
        self.last_results = results
        log.info("Worker finished, received results")
    
    def _on_worker_error(self, error_msg: str):
        """Handle worker error signal (runs on main thread)"""
        self.last_error = error_msg
        log.error("Worker error: {}".format(error_msg))
    
    def _on_worker_progress(self, progress_pct: int, message: str):
        """Handle worker progress signal (runs on main thread)"""
        log.info("Theme progress: {}% - {}".format(progress_pct, message))
    
    def _apply_results_on_main_thread(self, results: Dict[str, Any]):
        """
        Apply worker results on main thread (safe for Qt operations)
        
        Args:
            results: Dict with effects, captions, audio_effects lists
        """
        from classes.app import get_app
        from classes.query import Clip
        
        app = get_app()
        
        # Apply effects
        for clip_id, effect_json in results.get("effects", []):
            try:
                clip = Clip.get(id=clip_id)
                if not clip:
                    continue
                
                clip_data = clip.data if isinstance(clip.data, dict) else {}
                
                # Generate real effect ID
                effect_json["id"] = app.project.generate_id()
                
                # Add effect to clip
                if "effects" not in clip_data:
                    clip_data["effects"] = []
                clip_data["effects"].append(effect_json)
                
                # Update clip (Qt call - must be on main thread)
                app.updates.update(["clips", clip_id], clip_data)
                
            except Exception as e:
                log.error("Failed to apply effect to clip {}: {}".format(clip_id, e))
        
        # Apply audio effects (metadata only)
        for clip_id, audio_config in results.get("audio_effects", []):
            try:
                clip = Clip.get(id=clip_id)
                if not clip:
                    continue
                
                clip_data = clip.data if isinstance(clip.data, dict) else {}
                
                if "audio_effects" not in clip_data:
                    clip_data["audio_effects"] = []
                clip_data["audio_effects"].append(audio_config)
                
                app.updates.update(["clips", clip_id], clip_data)
                
            except Exception as e:
                log.error("Failed to apply audio to clip {}: {}".format(clip_id, e))
        
        # Apply captions
        for clip_id, caption_list in results.get("captions", []):
            try:
                clip = Clip.get(id=clip_id)
                if not clip:
                    continue
                
                clip_data = clip.data if isinstance(clip.data, dict) else {}
                
                if "captions" not in clip_data:
                    clip_data["captions"] = []
                clip_data["captions"].extend(caption_list)
                
                app.updates.update(["clips", clip_id], clip_data)
                
            except Exception as e:
                log.error("Failed to apply captions to clip {}: {}".format(clip_id, e))
    
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
