#!/usr/bin/env python3
"""
Simple test script to verify theme system components load correctly.
Run this from the project root: python test_theme_system.py
"""

import sys
import os

# Fix Windows encoding issues
if sys.platform == "win32":
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

def test_theme_loader():
    """Test ThemeLoader can load themes"""
    print("Testing ThemeLoader...")
    try:
        from classes.theme_loader import ThemeLoader
        loader = ThemeLoader()
        
        # List themes
        themes = loader.list_available_themes()
        print(f"  ✓ Found {len(themes)} themes:")
        for theme in themes:
            print(f"    - {theme['theme_id']}: {theme['name']}")
        
        # Load horror theme
        horror = loader.load_theme('horror')
        print(f"  ✓ Loaded horror theme: {horror['name']}")
        
        # Validate
        warnings = loader.validate_theme(horror)
        if warnings:
            print(f"  ! Validation warnings: {warnings}")
        else:
            print(f"  ✓ Theme validation passed")
        
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_communication_bus():
    """Test AgentCommunicationBus"""
    print("\nTesting AgentCommunicationBus...")
    try:
        from classes.agent_communication_bus import AgentCommunicationBus
        bus = AgentCommunicationBus()
        
        # Start bus
        bus.start()
        print("  ✓ Communication bus started")
        
        # Register agent
        bus.register_agent("test_agent_1", "test", {"foo": "bar"})
        print("  ✓ Agent registered")
        
        # Publish message
        bus.publish("test_topic", {"data": "test"}, "test_agent_1", "test")
        print("  ✓ Message published")
        
        # Stop bus
        bus.stop()
        print("  ✓ Communication bus stopped")
        
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_theme_engine():
    """Test ThemeEngine"""
    print("\nTesting ThemeEngine...")
    try:
        from classes.theme_engine import ThemeEngine
        engine = ThemeEngine()
        
        # List themes
        themes = engine.list_themes()
        print(f"  ✓ Engine can list {len(themes)} themes")
        
        # Get theme info
        info = engine.get_theme_info('documentary')
        if info:
            print(f"  ✓ Got theme info for 'documentary': {info['description'][:50]}...")
        
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_ai_tools_import():
    """Test AI tools can be imported"""
    print("\nTesting AI Tools Integration...")
    try:
        from classes.ai_openshot_tools import list_themes, describe_theme
        
        # Test list_themes
        result = list_themes()
        print(f"  ✓ list_themes() returns: {result[:100]}...")
        
        # Test describe_theme
        result = describe_theme('horror')
        print(f"  ✓ describe_theme('horror') returns: {result[:80]}...")
        
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    print("=" * 60)
    print("THEME SYSTEM COMPONENT TESTS")
    print("=" * 60)
    
    results = []
    
    # Run tests
    results.append(("ThemeLoader", test_theme_loader()))
    results.append(("CommunicationBus", test_communication_bus()))
    results.append(("ThemeEngine", test_theme_engine()))
    results.append(("AI Tools", test_ai_tools_import()))
    
    # Summary
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
        print("\n🎉 All tests passed! Theme system is ready.")
        return 0
    else:
        print("\n⚠️  Some tests failed. Check errors above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
