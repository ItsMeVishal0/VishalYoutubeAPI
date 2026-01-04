#!/usr/bin/env python3
import os
import sys
import subprocess
import urllib.request
import json

def setup_environment():
    """Setup complete environment for YouTube audio streaming"""
    
    print("🚀 Setting up YouTube Audio Streaming API...")
    
    # 1. Check Python version
    print("\n1️⃣ Checking Python version...")
    if sys.version_info < (3, 8):
        print("❌ Python 3.8+ required")
        sys.exit(1)
    print(f"✅ Python {sys.version_info.major}.{sys.version_info.minor}")
    
    # 2. Install/Update dependencies
    print("\n2️⃣ Installing dependencies...")
    dependencies = [
        "yt-dlp[default]",
        "fastapi",
        "uvicorn[standard]",
        "aiohttp",
        "python-multipart",
        "websockets",
    ]
    
    for dep in dependencies:
        print(f"   Installing {dep}...")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", dep], 
                         check=True, capture_output=True)
            print(f"   ✅ {dep}")
        except subprocess.CalledProcessError as e:
            print(f"   ❌ Failed to install {dep}: {e.stderr.decode()[:200]}")
    
    # 3. Create directories
    print("\n3️⃣ Creating directories...")
    directories = ["downloads", "static", "logs"]
    for directory in directories:
        os.makedirs(directory, exist_ok=True)
        print(f"   📁 Created {directory}/")
    
    # 4. Check for cookies
    print("\n4️⃣ Checking for cookies file...")
    cookies_file = "cookies.txt"
    if os.path.exists(cookies_file):
        print(f"   ✅ Found {cookies_file}")
        print("   ℹ️  To export cookies from browser:")
        print("      Chrome: Install 'Get cookies.txt' extension")
        print("      Firefox: Use 'cookies.txt' extension")
    else:
        print(f"   ⚠️  No {cookies_file} found")
        print("   ℹ️  Age-restricted videos may not work without cookies")
    
    # 5. Test yt-dlp
    print("\n5️⃣ Testing yt-dlp...")
    test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # Test URL
    try:
        import yt_dlp
        ydl = yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True})
        info = ydl.extract_info(test_url, download=False)
        print(f"   ✅ yt-dlp working. Found: {info.get('title', 'Test video')}")
    except Exception as e:
        print(f"   ❌ yt-dlp test failed: {e}")
    
    # 6. Create cookies template
    print("\n6️⃣ Creating cookies template (if needed)...")
    if not os.path.exists(cookies_file):
        with open(cookies_example, "w") as f:
            f.write("# Export cookies from browser and save as cookies.txt\n")
            f.write("# Format: Netscape HTTP Cookie File\n")
            f.write("# Domain should include .youtube.com\n")
        print(f"   📄 Created {cookies_example}")
    
    print("\n" + "="*50)
    print("✅ Setup complete!")
    print("="*50)
    print("\nTo start the server:")
    print("  python main.py")
    print("\nFor audio streaming issues:")
    print("  1. Add cookies.txt from logged-in YouTube session")
    print("  2. Update yt-dlp: pip install --upgrade yt-dlp")
    print("  3. Use proxy if region-blocked: set PROXY in config.py")
    print("\nAPI Endpoints:")
    print("  http://localhost:8000/docs - API documentation")
    print("  http://localhost:8000/stream/audio?url=YOUTUBE_URL")
    print("  http://localhost:8000/stream/video?url=YOUTUBE_URL&quality=best")

if __name__ == "__main__":
    setup_environment()