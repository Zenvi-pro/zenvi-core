# AI Theme Implementer - Technical Documentation

## Architecture Overview

The AI Theme Implementer system consists of several key components:

```
ThemeEngine (Orchestrator)
├── ThemeLoader (Load & parse themes)
├── ColorGradingApplicator (Color adjustments)
├── EffectsApplicator (Visual effects)
├── ThemeAnalyzer (AI scene analysis)
└── LUTLoader (3D LUT support)
```

## Core Classes

### ThemeEngine

**Location**: `src/classes/theme_engine.py`

Main orchestrator for theme application.

**Key Methods:**

```python
engine = ThemeEngine()

# Load a theme
engine.load_theme("stranger-things")

# Apply theme with options
engine.apply_theme(
    theme_id="stranger-things",
    apply_color_grading=True,
    apply_effects=True,
    intensity=1.0,
    smart_mode=True
)

# Get available themes
themes = engine.get_available_themes()
```

**Signals:**
- `progress_updated(int percent, str task)` - Progress updates
- `application_complete(bool success, str message)` - Completion status

### ThemeLoader

**Location**: `src/classes/theme_loader.py`

Loads and validates theme definition files.

**Key Methods:**

```python
loader = ThemeLoader()

# Load theme by ID
theme = loader.load_theme("stranger-things")

# Load from file path
theme = loader.load_theme_from_file("/path/to/theme.json")

# List available themes
themes = loader.list_available_themes()

# Validate theme
warnings = loader.validate_theme(theme)
```

### ColorGradingApplicator

**Location**: `src/classes/color_grading_applicator.py`

Applies color grading settings to clips.

**Effects Applied:**
- Brightness (OpenShot Brightness effect)
- Contrast (OpenShot Brightness effect)
- Saturation (OpenShot Saturation effect)
- Hue (OpenShot Hue effect)
- LUT/ColorMap (OpenShot ColorMap effect)

**Key Methods:**

```python
applicator = ColorGradingApplicator()

# Apply color grading from theme
applicator.apply(theme, clips, intensity=1.0, smart_mode=True)
```

### EffectsApplicator

**Location**: `src/classes/effects_applicator.py`

Applies visual effects to clips.

**Effects Supported:**
- Grain/Noise
- Blur
- Sharpen
- Lens Flare/Glow
- Pixelate
- Color Shift

**Key Methods:**

```python
applicator = EffectsApplicator()

# Apply effects from theme
applicator.apply(theme, clips, intensity=1.0, smart_mode=True)
```

### ThemeAnalyzer

**Location**: `src/classes/theme_analyzer.py`

Analyzes clips using AI metadata for intelligent theme application.

**Key Methods:**

```python
analyzer = ThemeAnalyzer()

# Analyze a clip
analysis = analyzer.analyze_clip(clip)
# Returns: {
#   "intensity_modifier": 1.0,
#   "scene_type": "outdoor",
#   "lighting": "bright",
#   "mood": "happy",
#   "recommendations": {...}
# }

# Get compatibility score
score = analyzer.get_theme_compatibility_score(clip, "horror")

# Suggest themes
suggestions = analyzer.suggest_themes_for_clip(clip, available_themes)
```

### LUTLoader

**Location**: `src/classes/lut_loader.py`

Loads and validates 3D LUT files (.cube format).

**Key Methods:**

```python
lut_loader = LUTLoader()

# Load and validate LUT
lut_path = lut_loader.load_lut("/path/to/lut.cube")
```

## Theme Definition Format

### File Structure

```
themes/
└── your-theme-name/
    ├── theme.json          # Theme definition (required)
    ├── preview.jpg         # Preview thumbnail (optional)
    └── lut.cube           # LUT file (optional)
```

### theme.json Schema

```json
{
  "theme_id": "your-theme-id",
  "name": "Your Theme Name",
  "description": "Description of your theme",
  "version": "1.0",
  "creator": "Your Name",
  "thumbnail": "themes/your-theme-name/preview.jpg",
  
  "color_grading": {
    "lut_file": "themes/your-theme-name/lut.cube",
    "brightness": 1.05,
    "contrast": 1.15,
    "saturation": 1.25,
    "hue_shift": 5
  },
  
  "effects": {
    "grain": {
      "intensity": 0.3,
      "type": "16mm"
    },
    "blur": {
      "amount": 2.0
    },
    "sharpen": {
      "amount": 0.5
    },
    "glow": {
      "intensity": 0.4,
      "color": "#ff6b35"
    },
    "pixelate": {
      "amount": 0.2
    },
    "color_shift": {
      "amount": 10.0
    }
  },
  
  "music": {
    "genres": ["synthwave", "80s"],
    "mood": ["mysterious", "nostalgic"],
    "tempo_range": [80, 120]
  },
  
  "typography": {
    "fonts": ["ITC Benguiat", "Helvetica"],
    "title_color": "#ff0000",
    "animation_style": "flicker",
    "glow_effect": true
  },
  
  "editing_style": {
    "pacing": "moderate",
    "avg_clip_duration": 4.5,
    "transition_type": "cut",
    "transition_frequency": "high"
  },
  
  "scene_adaptations": {
    "outdoor": {
      "saturation": 1.4,
      "warmth": 1.1
    },
    "indoor": {
      "grain": 0.4
    },
    "night": {
      "grain": 0.5
    }
  }
}
```

### Field Descriptions

#### Required Fields

- `theme_id` (string): Unique identifier (use kebab-case)
- `name` (string): Display name for the theme
- `description` (string): Brief description
- `version` (string): Theme version (semantic versioning)
- `creator` (string): Theme creator name

#### Optional Fields

**color_grading** (object):
- `lut_file` (string): Path to .cube LUT file (relative to theme directory)
- `brightness` (float): 1.0 = normal, >1.0 = brighter, <1.0 = darker
- `contrast` (float): 1.0 = normal, >1.0 = more contrast
- `saturation` (float): 1.0 = normal, 0.0 = grayscale, >1.0 = more saturated
- `hue_shift` (float): Degrees to shift hue (-180 to 180)

**effects** (object):
- `grain` (object): Film grain/noise
  - `intensity` (float): 0.0 to 1.0
  - `type` (string): "16mm", "video", "digital"
- `blur` (object): Blur effect
  - `amount` (float): Blur radius
- `sharpen` (object): Sharpen effect
  - `amount` (float): Sharpen intensity
- `glow` (object): Lens flare/glow
  - `intensity` (float): Glow intensity
  - `color` (string): Hex color code
- `pixelate` (object): Pixelation
  - `amount` (float): Pixelation level
- `color_shift` (object): Color shifting
  - `amount` (float): Shift amount

**scene_adaptations** (object):
Override settings for specific scene types:
- `outdoor`, `indoor`, `night`, `day`, `action`

Each can contain any color_grading or effects properties.

## Creating a Custom Theme

### Step 1: Create Theme Directory

```bash
mkdir themes/my-custom-theme
```

### Step 2: Create theme.json

```json
{
  "theme_id": "my-custom-theme",
  "name": "My Custom Theme",
  "description": "My awesome custom look",
  "version": "1.0",
  "creator": "Your Name",
  
  "color_grading": {
    "brightness": 1.1,
    "contrast": 1.2,
    "saturation": 1.15
  },
  
  "effects": {
    "grain": {
      "intensity": 0.2
    }
  }
}
```

### Step 3: (Optional) Add LUT File

1. Get a .cube LUT file
2. Place it in your theme directory
3. Reference it in theme.json:

```json
"color_grading": {
  "lut_file": "themes/my-custom-theme/my-lut.cube"
}
```

### Step 4: (Optional) Add Preview Image

1. Create a preview image (1920x1080 recommended)
2. Save as `preview.jpg` in theme directory
3. Reference it:

```json
"thumbnail": "themes/my-custom-theme/preview.jpg"
```

### Step 5: Test Your Theme

1. Restart Zenvi Core
2. Open Theme Browser
3. Look for your theme
4. Apply to test footage

## Integration with AI Chat

Add custom theme detection in `ai_chat_functionality.py`:

```python
theme_mapping = {
    "your theme": "your-theme-id",
    # Add your theme here
}
```

## Effect Parameter Reference

### OpenShot Effect Mapping

| Theme Effect | OpenShot Effect | Parameters |
|-------------|----------------|------------|
| brightness | Brightness | brightness.value |
| contrast | Brightness | contrast.value |
| saturation | Saturation | saturation.value |
| hue | Hue | hue.value |
| lut | ColorMap | file.value, alpha.value |
| grain | Noise | level.value |
| blur | Blur | horizontal_radius, vertical_radius |
| sharpen | Sharpen | sigma.value |
| glow | LensFlare | (various) |
| pixelate | Pixelate | pixelization.value |
| color_shift | Color Shift | x.value |

## Best Practices

### 1. Value Ranges

- Keep brightness between 0.8 and 1.2
- Keep contrast between 0.9 and 1.4
- Keep saturation between 0.7 and 1.4
- Grain intensity should be 0.1 to 0.4 max

### 2. Scene Adaptations

- Use scene adaptations for fine-tuning
- Don't override all base values
- Test on diverse footage

### 3. LUT Files

- Use professional LUTs for best results
- Test LUTs on various footage types
- Provide fallback color grading without LUT

### 4. Performance

- Too many effects can slow playback
- Keep grain intensity reasonable
- Test on lower-end systems

## API Usage Examples

### Programmatic Theme Application

```python
from classes.theme_engine import ThemeEngine
from classes.query import Clip

# Initialize engine
engine = ThemeEngine()

# Get all clips
clips = Clip.filter()

# Load and apply theme
if engine.load_theme("stranger-things"):
    success = engine.apply_theme(
        theme_id="stranger-things",
        apply_color_grading=True,
        apply_effects=True,
        intensity=0.8,  # 80% intensity
        smart_mode=True
    )
    
    if success:
        print("Theme applied successfully!")
```

### Custom Intensity Per Clip

```python
from classes.color_grading_applicator import ColorGradingApplicator
from classes.theme_loader import ThemeLoader

# Load theme
loader = ThemeLoader()
theme = loader.load_theme("horror")

# Apply with custom logic
applicator = ColorGradingApplicator()

for clip in clips:
    # Custom intensity based on clip duration
    duration = clip.data.get("end", 0) - clip.data.get("start", 0)
    intensity = 0.5 if duration > 10 else 1.0
    
    applicator.apply(theme, [clip], intensity=intensity, smart_mode=False)
```

## Troubleshooting

### Theme Not Loading

1. Check JSON syntax with validator
2. Verify file paths are relative to theme directory
3. Check console logs for errors

### Effects Not Applying

1. Verify effect names match OpenShot effects
2. Check parameter value ranges
3. Test with simpler theme first

### LUT Not Working

1. Verify .cube file format
2. Check file path is correct
3. Try absolute path for testing

## Future Enhancements

Planned features:
- Music applicator implementation
- Typography applicator implementation
- Pacing applicator implementation
- Theme marketplace integration
- AI theme learning from reference videos
- Multi-theme blending
- Keyframe-based effect intensity

---

**For more information, see `THEME_SYSTEM_README.md`**
