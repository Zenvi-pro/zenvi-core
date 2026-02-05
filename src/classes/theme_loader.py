"""
@file
@brief Theme loader for loading and validating theme definitions
@author Zenvi Team

@section LICENSE

Copyright (c) 2024 Zenvi Core
This file is part of Zenvi Core video editor.
"""

import json
import os
from typing import Dict, Any, List, Optional
from pathlib import Path
from classes.logger import log


class ThemeValidationError(Exception):
    """Raised when theme validation fails"""
    pass


class ThemeLoader:
    """Loads and validates theme definition files"""
    
    def __init__(self, themes_dir: str = "themes"):
        """
        Initialize the theme loader
        
        Args:
            themes_dir: Directory containing theme folders
        """
        self.themes_dir = Path(themes_dir)
        self._theme_cache: Dict[str, Dict[str, Any]] = {}
        log.info(f"ThemeLoader initialized with directory: {self.themes_dir}")
    
    def list_available_themes(self) -> List[Dict[str, str]]:
        """
        List all available themes
        
        Returns:
            List of dicts with theme_id, name, and description
        """
        themes = []
        
        if not self.themes_dir.exists():
            log.warning(f"Themes directory not found: {self.themes_dir}")
            return themes
        
        for theme_dir in self.themes_dir.iterdir():
            if not theme_dir.is_dir():
                continue
            
            theme_file = theme_dir / "theme.json"
            if not theme_file.exists():
                continue
            
            try:
                with open(theme_file, 'r', encoding='utf-8') as f:
                    theme_data = json.load(f)
                
                themes.append({
                    "theme_id": theme_data.get("theme_id", theme_dir.name),
                    "name": theme_data.get("name", theme_dir.name),
                    "description": theme_data.get("description", "No description")
                })
            except Exception as e:
                log.warning(f"Error loading theme {theme_dir.name}: {e}")
                continue
        
        log.info(f"Found {len(themes)} available themes")
        return themes
    
    def load_theme(self, theme_id: str, use_cache: bool = True) -> Dict[str, Any]:
        """
        Load a theme by ID
        
        Args:
            theme_id: Theme identifier (e.g., 'horror', 'documentary')
            use_cache: Whether to use cached theme if available
        
        Returns:
            Theme data dictionary
        
        Raises:
            FileNotFoundError: If theme not found
            ThemeValidationError: If theme validation fails
        """
        # Check cache first
        if use_cache and theme_id in self._theme_cache:
            log.debug(f"Returning cached theme: {theme_id}")
            return self._theme_cache[theme_id]
        
        # Find theme directory
        theme_dir = self.themes_dir / theme_id
        if not theme_dir.exists():
            raise FileNotFoundError(f"Theme directory not found: {theme_dir}")
        
        theme_file = theme_dir / "theme.json"
        if not theme_file.exists():
            raise FileNotFoundError(f"Theme file not found: {theme_file}")
        
        # Load theme JSON
        try:
            with open(theme_file, 'r', encoding='utf-8') as f:
                theme_data = json.load(f)
        except json.JSONDecodeError as e:
            raise ThemeValidationError(f"Invalid JSON in theme file: {e}")
        
        # Validate theme
        warnings = self.validate_theme(theme_data)
        if warnings:
            log.warning(f"Theme {theme_id} validation warnings: {warnings}")
        
        # Cache theme
        self._theme_cache[theme_id] = theme_data
        log.info(f"Loaded theme: {theme_id}")
        
        return theme_data
    
    def load_theme_from_file(self, file_path: str) -> Dict[str, Any]:
        """
        Load a theme from a specific file path
        
        Args:
            file_path: Path to theme.json file
        
        Returns:
            Theme data dictionary
        
        Raises:
            FileNotFoundError: If file not found
            ThemeValidationError: If validation fails
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"Theme file not found: {file_path}")
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                theme_data = json.load(f)
        except json.JSONDecodeError as e:
            raise ThemeValidationError(f"Invalid JSON in theme file: {e}")
        
        # Validate theme
        warnings = self.validate_theme(theme_data)
        if warnings:
            log.warning(f"Theme validation warnings: {warnings}")
        
        return theme_data
    
    def validate_theme(self, theme_data: Dict[str, Any]) -> List[str]:
        """
        Validate theme data structure
        
        Args:
            theme_data: Theme dictionary to validate
        
        Returns:
            List of warning messages (empty if valid)
        """
        warnings = []
        
        # Check required fields
        required_fields = ["theme_id", "name", "description", "version"]
        for field in required_fields:
            if field not in theme_data:
                warnings.append(f"Missing required field: {field}")
        
        # Validate color grading section
        if "color_grading" in theme_data:
            color = theme_data["color_grading"]
            
            # Check brightness (typically 0.5 to 1.5)
            if "brightness" in color:
                if not (0.3 <= color["brightness"] <= 2.0):
                    warnings.append(f"Brightness {color['brightness']} is outside recommended range (0.3-2.0)")
            
            # Check contrast (typically 0.8 to 1.5)
            if "contrast" in color:
                if not (0.5 <= color["contrast"] <= 2.0):
                    warnings.append(f"Contrast {color['contrast']} is outside recommended range (0.5-2.0)")
            
            # Check saturation (typically 0.5 to 1.5)
            if "saturation" in color:
                if not (0.0 <= color["saturation"] <= 2.0):
                    warnings.append(f"Saturation {color['saturation']} is outside recommended range (0.0-2.0)")
            
            # Check hue shift (-180 to 180)
            if "hue_shift" in color:
                if not (-180 <= color["hue_shift"] <= 180):
                    warnings.append(f"Hue shift {color['hue_shift']} is outside range (-180 to 180)")
        
        # Validate effects section
        if "effects" in theme_data:
            effects = theme_data["effects"]
            
            # Check grain intensity (0.0 to 1.0)
            if "grain" in effects and "intensity" in effects["grain"]:
                if not (0.0 <= effects["grain"]["intensity"] <= 1.0):
                    warnings.append(f"Grain intensity should be between 0.0 and 1.0")
        
        # Validate sound section
        if "sound" in theme_data:
            sound = theme_data["sound"]
            
            # Check bass boost (-12 to +12 dB)
            if "bass_boost_db" in sound:
                if not (-12 <= sound["bass_boost_db"] <= 12):
                    warnings.append(f"Bass boost {sound['bass_boost_db']} dB is extreme (recommended: -6 to +6)")
        
        # Validate captions section
        if "captions" in theme_data:
            captions = theme_data["captions"]
            
            # Check required caption fields
            caption_required = ["font", "font_size", "color", "position"]
            for field in caption_required:
                if field not in captions:
                    warnings.append(f"Captions missing recommended field: {field}")
        
        return warnings
    
    def get_theme_metadata(self, theme_id: str) -> Optional[Dict[str, Any]]:
        """
        Get theme metadata without loading full theme
        
        Args:
            theme_id: Theme identifier
        
        Returns:
            Dict with metadata or None if not found
        """
        theme_dir = self.themes_dir / theme_id
        if not theme_dir.exists():
            return None
        
        theme_file = theme_dir / "theme.json"
        if not theme_file.exists():
            return None
        
        try:
            with open(theme_file, 'r', encoding='utf-8') as f:
                theme_data = json.load(f)
            
            return {
                "theme_id": theme_data.get("theme_id", theme_id),
                "name": theme_data.get("name", "Unknown"),
                "description": theme_data.get("description", ""),
                "version": theme_data.get("version", "1.0.0"),
                "creator": theme_data.get("creator", "Unknown"),
                "tags": theme_data.get("tags", []),
                "best_for": theme_data.get("best_for", [])
            }
        except Exception as e:
            log.error(f"Error loading theme metadata for {theme_id}: {e}")
            return None
    
    def clear_cache(self):
        """Clear the theme cache"""
        self._theme_cache.clear()
        log.debug("Theme cache cleared")
    
    def reload_theme(self, theme_id: str) -> Dict[str, Any]:
        """
        Reload a theme, bypassing cache
        
        Args:
            theme_id: Theme identifier
        
        Returns:
            Reloaded theme data
        """
        if theme_id in self._theme_cache:
            del self._theme_cache[theme_id]
        
        return self.load_theme(theme_id, use_cache=False)
