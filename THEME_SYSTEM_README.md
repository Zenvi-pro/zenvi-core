# AI Theme Implementer - User Guide

## Overview

The AI Theme Implementer is a revolutionary feature in Zenvi Core that allows you to transform your entire video project with cinematic themes in just one click. Apply iconic looks from movies and shows like "Stranger Things", "Blade Runner", or create your own custom themes.

## What It Does

The Theme System automatically applies:
- **Color Grading**: Professional color adjustments (brightness, contrast, saturation, hue)
- **LUT Support**: Industry-standard 3D LUT files for cinematic color grading
- **Visual Effects**: Film grain, blur, sharpen, lens flare, and more
- **Smart AI Adjustments**: Analyzes each clip and adjusts effects intelligently
- **Scene-Aware Application**: Different settings for indoor/outdoor, day/night scenes

## Getting Started

### Accessing the Theme Browser

1. Open Zenvi Core
2. Go to `View > Docks > Theme Browser` (or it may be visible by default in the right panel)
3. The Theme Browser will show all available themes

### Applying a Theme

**Method 1: Using the Theme Browser**

1. Open the Theme Browser dock
2. Browse available themes (cards with thumbnails and descriptions)
3. Click on a theme card to see details
4. Click the **"Apply"** button
5. Confirm the application when prompted
6. Wait for the theme to be applied to all clips

**Method 2: Using the AI Chat Assistant**

1. Open the AI Chat Assistant (`View > Docks > AI Assistant`)
2. Type commands like:
   - `list themes` - See all available themes
   - `apply stranger things theme` - Apply the Stranger Things theme
   - `suggest a theme` - Get theme recommendations
   - `apply horror theme` - Apply the Horror theme

### Theme Application Options

In the Theme Browser, you can customize how themes are applied:

**Application Components:**
- ✅ **Color Grading** - Brightness, contrast, saturation, hue adjustments
- ✅ **Visual Effects** - Grain, blur, sharpen, lens flare, etc.
- ⏳ **Music & Audio** - Coming soon
- ⏳ **Typography** - Coming soon
- ⏳ **Editing Pacing** - Coming soon

**Intensity Slider:**
- Adjust from 0% to 100%
- Controls how strongly the theme is applied
- Lower values = subtle effect
- Higher values = pronounced effect

**Smart Mode:**
- ✅ Enabled: AI adjusts intensity per clip based on scene analysis
- ❌ Disabled: Same intensity applied to all clips

## Available Preset Themes

### 1. Stranger Things
**Description**: 80s sci-fi horror aesthetic with retro vibes, teal shadows, and vintage film grain

**Best For**: Mystery, nostalgia, 80s content, sci-fi horror

**Characteristics:**
- Teal-orange color palette
- Increased saturation
- Film grain effect
- High contrast

**Settings:**
- Brightness: +5%
- Contrast: +15%
- Saturation: +25%
- Film Grain: Medium

---

### 2. Blade Runner
**Description**: Cyberpunk noir with cyan/magenta tones, heavy contrast, and atmospheric haze

**Best For**: Cyberpunk, futuristic, noir, night scenes

**Characteristics:**
- Cyan/magenta color split
- Crushed blacks
- Neon glow effects
- Heavy contrast

**Settings:**
- Brightness: -5%
- Contrast: +35%
- Saturation: +15%
- Glow Effect: Cyan
- Hue Shift: +10°

---

### 3. Wes Anderson
**Description**: Pastel color palette with symmetrical framing and vintage warmth

**Best For**: Whimsical, artistic, indie films, colorful content

**Characteristics:**
- Pastel colors
- Warm tones
- Soft contrast
- Slight film grain

**Settings:**
- Brightness: +8%
- Contrast: -5%
- Saturation: +10%
- Hue Shift: +5°
- Film Grain: Light

---

### 4. Documentary
**Description**: Natural, professional look with subtle grading and clarity

**Best For**: Interviews, documentaries, professional content, natural look

**Characteristics:**
- Natural colors
- Enhanced sharpness
- Minimal saturation boost
- Clean, professional look

**Settings:**
- Brightness: +2%
- Contrast: +8%
- Saturation: -2%
- Sharpen: Light

---

### 5. Horror
**Description**: Dark, desaturated aesthetic with high contrast and ominous atmosphere

**Best For**: Horror, thriller, tense scenes, dark content

**Characteristics:**
- Desaturated colors
- Darkened shadows
- Heavy contrast
- Increased grain

**Settings:**
- Brightness: -15%
- Contrast: +40%
- Saturation: -30%
- Film Grain: Heavy
- Hue Shift: -5°

## Smart AI Features

### Scene-Aware Application

The Theme System uses AI to analyze your clips and applies effects differently based on:

- **Scene Type**: Indoor vs outdoor
- **Lighting**: Bright, normal, or dark
- **Mood**: Happy, tense, calm, etc.
- **Quality**: High vs low resolution
- **Content**: Objects, activities, people

**Example**: The "Stranger Things" theme will:
- Apply **more grain** to indoor scenes (for retro feel)
- Apply **more saturation** to outdoor scenes (for vibrant look)
- Apply **even more grain** to night scenes (for VHS aesthetic)

### Intensity Modifiers

When **Smart Mode** is enabled, the AI automatically adjusts effect intensity:

- **Outdoor bright scenes**: Reduced intensity (80%)
- **Dark scenes**: Increased intensity (120%) for better visibility
- **High-quality footage**: Subtle effects (90%) to preserve quality
- **Vintage/retro content**: Increased grain (130%)
- **Modern/professional content**: Reduced grain (70%)

## Tips and Best Practices

### 1. Preview Before Full Application
- Apply theme to a single clip first to preview
- Adjust intensity slider to taste
- Then apply to entire project

### 2. Use Smart Mode
- Smart Mode provides better results for mixed footage
- Disable for uniform look across all clips

### 3. Stack Themes
- You can apply multiple themes sequentially
- Effects will combine (use caution)

### 4. Undo If Needed
- Use `Edit > Undo` to remove theme effects
- Theme effects are added as regular effects, so they can be edited manually

### 5. Custom Adjustments
- After applying a theme, manually tweak individual clip effects
- Right-click on clips and go to Properties to adjust

### 6. Performance
- Applying themes to large projects (100+ clips) may take time
- Be patient and wait for completion message

## Troubleshooting

### "No clips found in project"
- Make sure you have clips on the timeline
- Import media files first before applying themes

### "Failed to apply theme"
- Check that theme files exist in the `themes/` directory
- Look at application logs for detailed error messages

### Theme looks too strong/weak
- Adjust the intensity slider (50-75% often works well)
- Try enabling/disabling Smart Mode

### Theme not visible in browser
- Refresh the theme list (⟳ button)
- Verify theme files are in correct directory structure
- Check that `theme.json` file is valid JSON

## Advanced: Creating Custom Themes

See `THEME_API_DOCS.md` for information on creating your own themes.

## Keyboard Shortcuts

- No specific shortcuts yet (coming in future updates)

## Support

For issues or questions:
- Check application logs: `Help > View Log`
- Report bugs on GitHub issues
- Community support on Discord

---

**Enjoy transforming your videos with cinematic themes!** 🎬✨
