#!/usr/bin/env python3
"""
Test simplified theme system (no threading/multi-agent)
"""

import sys
import os

# Fix Windows encoding
if sys.platform == "win32":
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

def test_imports():
    """Test all imports work"""
    print("Testing imports...")
    try:
        from classes.theme_loader import ThemeLoader
        from classes.theme_engine import ThemeEngine
        from classes.theme_applicator import apply_color_grading, apply_film_grain, add_captions_to_clips
        from classes.ai_openshot_tools import (
            list_themes, describe_theme, adjust_color_grading, 
            add_captions, add_film_grain
        )
        print("  ✓ All imports successful")
        return True
    except Exception as e:
        print(f"  ✗ Import error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_theme_loader():
    """Test theme loader"""
    print("\nTesting ThemeLoader...")
    try:
        from classes.theme_loader import ThemeLoader
        loader = ThemeLoader()
        
        themes = loader.list_available_themes()
        print(f"  ✓ Found {len(themes)} themes")
        
        theme = loader.load_theme('wes-anderson')
        print(f"  ✓ Loaded 'wes-anderson' theme: {theme['name']}")
        print(f"    - Brightness: {theme['color_grading']['brightness']}")
        print(f"    - Saturation: {theme['color_grading']['saturation']}")
        print(f"    - Grain: {theme['effects']['grain']['intensity']}")
        
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_ai_tools():
    """Test AI tool functions"""
    print("\nTesting AI Tool Functions...")
    try:
        from classes.ai_openshot_tools import list_themes, describe_theme
        
        # Test list_themes
        result = list_themes()
        print(f"  ✓ list_themes(): {result[:80]}...")
        
        # Test describe_theme
        result = describe_theme('horror')
        print(f"  ✓ describe_theme('horror'): {result[:80]}...")
        
        # Test describe_theme for wes-anderson
        result = describe_theme('wes-anderson')
        print(f"  ✓ describe_theme('wes-anderson'): {result[:80]}...")
        
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_theme_engine():
    """Test simplified theme engine"""
    print("\nTesting Simplified ThemeEngine...")
    try:
        from classes.theme_engine import ThemeEngine
        engine = ThemeEngine()
        
        themes = engine.list_themes()
        print(f"  ✓ Engine lists {len(themes)} themes")
        
        info = engine.get_theme_info('documentary')
        if info:
            print(f"  ✓ Got theme info: {info['name']}")
            print(f"    Tags: {', '.join(info.get('tags', []))}")
        
        print("  ✓ Theme engine works (no threading/agents)")
        
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    print("=" * 60)
    print("SIMPLIFIED THEME SYSTEM TESTS")
    print("=" * 60)
    
    results = []
    
    results.append(("Imports", test_imports()))
    results.append(("ThemeLoader", test_theme_loader()))
    results.append(("AI Tools", test_ai_tools()))
    results.append(("ThemeEngine", test_theme_engine()))
    
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "PASS ✓" if result else "FAIL ✗"
        print(f"{name:20s}: {status}")
    
    print(f"\n{passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed! Simplified theme system ready.")
        print("\nAvailable natural language commands:")
        print("  - 'apply wes anderson theme'")
        print("  - 'make this brighter'")
        print("  - 'add captions'")
        print("  - 'increase saturation'")
        print("  - 'add film grain'")
        return 0
    else:
        print("\n⚠️  Some tests failed.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
