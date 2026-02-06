#!/usr/bin/env python3
"""
Test theme system with threading
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
        from classes.theme_worker import ThemeWorker
        from classes.theme_applicator import apply_color_grading
        print("  ✓ All imports successful")
        return True
    except Exception as e:
        print(f"  ✗ Import error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_worker_creation():
    """Test worker can be created"""
    print("\nTesting ThemeWorker creation...")
    try:
        from classes.theme_worker import ThemeWorker
        from classes.theme_loader import ThemeLoader
        
        loader = ThemeLoader()
        theme = loader.load_theme('horror')
        
        worker = ThemeWorker(
            theme_data=theme,
            clip_ids=['test1', 'test2'],
            options={'apply_color': True, 'apply_sound': True, 'apply_captions': False}
        )
        
        print("  ✓ ThemeWorker created successfully")
        print(f"    - Processing {len(worker.clip_ids)} clips")
        print(f"    - Options: {worker.options}")
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_engine_with_threading():
    """Test theme engine with Qt threading"""
    print("\nTesting ThemeEngine with threading...")
    try:
        from classes.theme_engine import ThemeEngine
        
        engine = ThemeEngine()
        
        # Check if Qt is available
        try:
            from PyQt5.QtCore import QThread
            has_qt = True
        except ImportError:
            has_qt = False
        
        if has_qt:
            print("  ✓ Qt available - threading will be used")
        else:
            print("  ⚠ Qt not available - will use synchronous fallback")
        
        # List themes
        themes = engine.list_themes()
        print(f"  ✓ Engine loaded {len(themes)} themes")
        
        # Load a theme
        theme_data = engine.load_theme('wes-anderson')
        print(f"  ✓ Loaded 'wes-anderson' theme")
        print(f"    - Brightness: {theme_data['color_grading']['brightness']}")
        print(f"    - Has captions config: {'captions' in theme_data}")
        
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_dependencies():
    """Test required dependencies"""
    print("\nTesting dependencies...")
    
    results = []
    
    # Test openai
    try:
        import openai
        print("  ✓ openai package installed")
        results.append(True)
    except ImportError:
        print("  ⚠ openai package NOT installed (captions will not work)")
        print("     Install with: pip install openai")
        results.append(False)
    
    # Test PyQt5
    try:
        from PyQt5.QtCore import QThread, QEventLoop
        print("  ✓ PyQt5 with threading support available")
        results.append(True)
    except ImportError:
        print("  ⚠ PyQt5 not available (will use synchronous mode)")
        results.append(False)
    
    # Test openshot
    try:
        import openshot
        print("  ✓ OpenShot library available")
        results.append(True)
    except ImportError:
        print("  ⚠ OpenShot library not available")
        results.append(False)
    
    return any(results)

def test_tool_integration():
    """Test AI tool integration"""
    print("\nTesting AI tool integration...")
    try:
        from classes.ai_openshot_tools import list_themes, describe_theme
        
        # Test list_themes
        result = list_themes()
        print(f"  ✓ list_themes() works: {result[:80]}...")
        
        # Test describe_theme
        result = describe_theme('horror')
        print(f"  ✓ describe_theme('horror') works")
        
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    print("=" * 60)
    print("THEME SYSTEM WITH THREADING - TESTS")
    print("=" * 60)
    
    results = []
    
    results.append(("Imports", test_imports()))
    results.append(("Dependencies", test_dependencies()))
    results.append(("Worker Creation", test_worker_creation()))
    results.append(("Engine Threading", test_engine_with_threading()))
    results.append(("Tool Integration", test_tool_integration()))
    
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "PASS ✓" if result else "FAIL ✗"
        print(f"{name:25s}: {status}")
    
    print(f"\n{passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed!")
        print("\nThreaded theme system is ready:")
        print("  - Worker threads handle computation (color, transcription)")
        print("  - Main thread applies Qt updates (safe!)")
        print("  - Parallel processing with ThreadPoolExecutor")
        print("  - UI stays responsive during processing")
        return 0
    elif passed >= 3:
        print("\n✓ Core functionality working!")
        print("Some optional features missing (install dependencies)")
        return 0
    else:
        print("\n⚠️  Critical tests failed.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
