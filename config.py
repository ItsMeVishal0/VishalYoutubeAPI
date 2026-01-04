import os
from typing import Optional

class Config:
    # Server settings
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False
    
    # Rate limiting
    RATE_LIMIT_WINDOW: int = 5  # Increased from 2
    MAX_REQUESTS_PER_MINUTE: int = 15  # Reduced from 30
    
    # YouTube settings
    YTDLP_TIMEOUT: int = 60
    COOKIES_FILE: Optional[str] = "cookies.txt" if os.path.exists("cookies.txt") else None
    
    # Cache settings
    CACHE_TTL: int = 7200  # 2 hours
    MAX_CACHE_SIZE: int = 1000
    
    # Proxy settings
    PROXY: Optional[str] = None  # Example: "http://proxy:port"
    
    # Download settings
    DOWNLOAD_DIR: str = "downloads"
    MAX_DOWNLOAD_SIZE: int = 500 * 1024 * 1024
    
    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "streaming_api.log"
    
    # YouTube API settings
    YOUTUBE_API_KEY: Optional[str] = None  # Optional: For additional metadata
    
    # Enhanced yt-dlp options
    YTDLP_DEFAULT_OPTS: dict = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'socket_timeout': 60,
        'retries': 10,
        'fragment_retries': 10,
        'skip_unavailable_fragments': True,
        'ignoreerrors': True,
        'nooverwrites': True,
        'concurrent_fragment_downloads': 1,  # Reduced to 1 for stability
        'throttledratelimit': 256000,  # 250KB/s
        'sleep_interval_requests': 3,
        'sleep_interval': 5,
        'max_sleep_interval': 30,
        'verbose': False,
        'force_generic_extractor': False,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],  # Try different clients
                'player_skip': ['configs', 'webpage'],
            }
        },
        'format_sort': ['+acodec', '+vcodec', 'res', 'fps', 'size', 'br', 'asr'],
        'check_formats': 'selected',
        'compat_opts': ['no-youtube-unavailable-videos'],
    }

config = Config()
os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)