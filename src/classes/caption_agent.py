"""
@file
@brief Caption agent for automatic transcription and caption styling
@author Zenvi Team

@section LICENSE

Copyright (c) 2024 Zenvi Core
This file is part of Zenvi Core video editor.
"""

import json
import os
import tempfile
import time
from typing import Dict, Any, List, Optional
from pathlib import Path
from classes.logger import log
from classes.base_theme_agent import BaseThemeAgent

try:
    import openshot
except ImportError:
    openshot = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


class CaptionAgent(BaseThemeAgent):
    """
    Agent specialized in transcribing audio and creating styled captions.
    Uses OpenAI Whisper API for transcription and applies theme-specific styling.
    """
    
    def __init__(self, theme_data: Dict[str, Any], api_key: Optional[str] = None):
        """
        Initialize the caption agent
        
        Args:
            theme_data: Theme configuration with captions section
            api_key: OpenAI API key (optional, will try to get from settings)
        """
        super().__init__("caption", theme_data)
        
        self.captions_config = theme_data.get("captions", {})
        self.api_key = api_key or self._get_api_key()
        self.client = None
        
        if self.api_key and OpenAI:
            try:
                self.client = OpenAI(api_key=self.api_key)
                log.info("OpenAI client initialized for caption transcription")
            except Exception as e:
                log.error(f"Failed to initialize OpenAI client: {e}")
        else:
            log.warning("OpenAI API key not available or openai package not installed")
        
        log.info(f"CaptionAgent initialized with theme: {theme_data.get('name', 'Unknown')}")
    
    def _get_api_key(self) -> Optional[str]:
        """Get OpenAI API key from settings or environment"""
        try:
            from classes.app import get_app
            app = get_app()
            if app:
                # Try to get from settings
                api_key = app._data.get("openai_whisper_api_key")
                if api_key:
                    return api_key
        except:
            pass
        
        # Try environment variable
        return os.getenv("OPENAI_API_KEY")
    
    def process_clip(self, clip_id: str) -> Dict[str, Any]:
        """
        Process a clip by transcribing audio and adding styled captions
        
        Args:
            clip_id: ID of clip to process
        
        Returns:
            Dict with processing results
        """
        if not self.client:
            raise RuntimeError("OpenAI client not initialized. Check API key.")
        
        if not openshot:
            raise ImportError("OpenShot library not available")
        
        from classes.query import Clip
        
        # Get the clip
        clip = Clip.get(id=clip_id)
        if not clip:
            raise ValueError(f"Clip not found: {clip_id}")
        
        try:
            # Check if clip has audio
            if not self._clip_has_audio(clip):
                log.info(f"Clip {clip_id} has no audio, skipping captions")
                return {
                    "success": True,
                    "captions_added": 0,
                    "message": "No audio track"
                }
            
            # 1. Extract audio from clip
            self._report_progress(clip_id, "extracting_audio", 0.1)
            audio_file = self._extract_audio(clip)
            
            if not audio_file or not os.path.exists(audio_file):
                # Request help from sound agent to clean audio
                self.request_help(
                    "Audio extraction failed or audio quality too low",
                    clip_id,
                    {"needs_audio_cleanup": True}
                )
                raise ValueError("Failed to extract audio")
            
            # 2. Transcribe using Whisper API
            self._report_progress(clip_id, "transcribing", 0.3)
            transcription = self._transcribe_audio(audio_file, clip_id)
            
            # Clean up temporary audio file
            try:
                os.unlink(audio_file)
            except:
                pass
            
            if not transcription or not transcription.get("segments"):
                log.warning(f"No transcription segments for clip {clip_id}")
                return {
                    "success": True,
                    "captions_added": 0,
                    "message": "No speech detected"
                }
            
            # 3. Create caption effects with styling
            self._report_progress(clip_id, "creating_captions", 0.7)
            captions_added = self._create_caption_effects(clip, transcription)
            
            self._report_progress(clip_id, "finalizing", 1.0)
            
            return {
                "success": True,
                "captions_added": captions_added,
                "segments_count": len(transcription.get("segments", []))
            }
            
        except Exception as e:
            log.error(f"Error adding captions to clip {clip_id}: {e}", exc_info=True)
            
            # Check if we should request more workers
            with self._lock:
                queue_size = len(self.assigned_clips)
                if queue_size > 3:
                    self.request_worker(
                        f"Caption queue backing up: {queue_size} clips waiting",
                        self.assigned_clips[:2]  # Suggest first 2 clips for new worker
                    )
            
            raise
    
    def _clip_has_audio(self, clip) -> bool:
        """Check if clip has an audio track"""
        clip_data = clip.data if isinstance(clip.data, dict) else {}
        has_audio = clip_data.get("has_audio", True)
        reader = clip_data.get("reader", {})
        if isinstance(reader, dict):
            has_audio_track = reader.get("has_audio", False)
            return has_audio and has_audio_track
        return has_audio
    
    def _extract_audio(self, clip) -> Optional[str]:
        """
        Extract audio from clip to a temporary file
        
        Args:
            clip: Clip object
        
        Returns:
            Path to temporary audio file
        """
        try:
            # Get clip file path
            clip_data = clip.data if isinstance(clip.data, dict) else {}
            reader = clip_data.get("reader", {})
            
            if isinstance(reader, dict):
                file_path = reader.get("path")
            else:
                # Try to get from file reference
                from classes.query import File
                clip_file = File.get(id=clip_data.get("file_id"))
                if clip_file:
                    file_path = clip_file.data.get("path")
                else:
                    file_path = None
            
            if not file_path or not os.path.exists(file_path):
                log.error(f"Could not find source file for clip {clip.id}")
                return None
            
            # Create temporary file for audio
            temp_dir = tempfile.gettempdir()
            temp_audio = os.path.join(temp_dir, f"clip_{clip.id}_audio.wav")
            
            # For simplicity, if the source is already audio, just use it
            # In production, would use FFmpeg to extract audio properly
            file_ext = Path(file_path).suffix.lower()
            if file_ext in ['.mp3', '.wav', '.aac', '.m4a', '.ogg', '.flac']:
                # Audio file, can use directly
                return file_path
            
            # For video files, we'd need to extract audio with FFmpeg
            # This is a simplified version - full implementation would use FFmpeg
            log.warning(f"Audio extraction for video files not fully implemented. Using source: {file_path}")
            return file_path
            
        except Exception as e:
            log.error(f"Error extracting audio: {e}", exc_info=True)
            return None
    
    def _transcribe_audio(self, audio_file: str, clip_id: str) -> Optional[Dict[str, Any]]:
        """
        Transcribe audio using OpenAI Whisper API
        
        Args:
            audio_file: Path to audio file
            clip_id: Clip ID (for error reporting)
        
        Returns:
            Transcription result with segments and timing
        """
        if not self.client:
            raise RuntimeError("OpenAI client not available")
        
        try:
            # Open audio file
            with open(audio_file, "rb") as audio:
                # Call Whisper API with word-level timestamps
                response = self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"]
                )
            
            # Check confidence (if available)
            # Note: Whisper API doesn't always return confidence scores
            # We'll check the quality of transcription indirectly
            
            if not response.segments:
                log.warning(f"Whisper returned no segments for clip {clip_id}")
                return None
            
            # Check if transcription quality seems low
            avg_segment_length = sum(len(s.text) for s in response.segments) / len(response.segments)
            if avg_segment_length < 5:
                # Very short segments might indicate poor audio quality
                self.request_help(
                    "Transcription quality may be low (very short segments)",
                    clip_id,
                    {
                        "needs_audio_cleanup": True,
                        "avg_segment_length": avg_segment_length
                    }
                )
            
            # Convert to our format
            result = {
                "text": response.text,
                "language": getattr(response, "language", "en"),
                "duration": getattr(response, "duration", 0),
                "segments": [
                    {
                        "id": i,
                        "start": seg.start,
                        "end": seg.end,
                        "text": seg.text.strip()
                    }
                    for i, seg in enumerate(response.segments)
                ]
            }
            
            log.info(f"Transcribed {len(result['segments'])} segments for clip {clip_id}")
            return result
            
        except Exception as e:
            log.error(f"Error transcribing audio: {e}", exc_info=True)
            
            # Check for API rate limiting
            if "rate_limit" in str(e).lower() or "429" in str(e):
                log.warning("Whisper API rate limit reached, waiting...")
                time.sleep(20)  # Wait before retrying
                # Request additional workers might not help here
                
            raise
    
    def _create_caption_effects(self, clip, transcription: Dict[str, Any]) -> int:
        """
        Create Caption effects for each transcription segment
        
        Args:
            clip: Clip object
            transcription: Transcription result with segments
        
        Returns:
            Number of captions added
        """
        from classes.app import get_app
        
        segments = transcription.get("segments", [])
        if not segments:
            return 0
        
        # Get clip timing info
        clip_data = clip.data if isinstance(clip.data, dict) else {}
        clip_start = clip_data.get("start", 0)
        
        captions_added = 0
        
        for segment in segments:
            try:
                # Create Caption effect
                caption_text = segment["text"]
                start_time = segment["start"]
                end_time = segment["end"]
                
                # Create caption with theme styling
                effect = self._create_styled_caption(
                    caption_text,
                    clip_start + start_time,
                    clip_start + end_time
                )
                
                if effect:
                    # Add to clip
                    effect_json = json.loads(effect.Json())
                    self._add_effect_to_clip(clip, effect_json)
                    captions_added += 1
                    
            except Exception as e:
                log.error(f"Error creating caption effect: {e}", exc_info=True)
                continue
        
        log.info(f"Added {captions_added} captions to clip {clip.id}")
        return captions_added
    
    def _create_styled_caption(self, text: str, start_time: float, end_time: float) -> Optional[Any]:
        """
        Create a styled caption effect
        
        Args:
            text: Caption text
            start_time: Start time in seconds
            end_time: End time in seconds
        
        Returns:
            Caption effect object
        """
        from classes.app import get_app
        
        try:
            # Create Caption effect
            effect = openshot.EffectInfo().CreateEffect("Caption")
            if not effect:
                log.warning("Caption effect not available in this OpenShot version")
                return None
            
            effect.Id(get_app().project.generate_id())
            
            # Get effect JSON
            effect_json = json.loads(effect.Json())
            
            # Apply theme styling
            if "caption_text" in effect_json:
                effect_json["caption_text"]["value"] = text
            
            if "caption_font" in effect_json:
                effect_json["caption_font"]["value"] = self.captions_config.get("font", "Arial")
            
            if "caption_size" in effect_json:
                effect_json["caption_size"]["value"] = self.captions_config.get("font_size", 42)
            
            if "caption_color" in effect_json:
                color = self.captions_config.get("color", "#ffffff")
                effect_json["caption_color"]["value"] = color
            
            # Set timing
            if "start" in effect_json:
                effect_json["start"]["value"] = start_time
            if "end" in effect_json:
                effect_json["end"]["value"] = end_time
            
            return effect
            
        except Exception as e:
            log.error(f"Error creating styled caption: {e}", exc_info=True)
            return None
    
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
