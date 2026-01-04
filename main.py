import asyncio
import time
import json
import os
import logging
from typing import Dict, List, Optional, Any
from contextlib import asynccontextmanager

import yt_dlp
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, BackgroundTasks, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    StreamingResponse, 
    RedirectResponse, 
    JSONResponse, 
    FileResponse
)
from fastapi.staticfiles import StaticFiles

from config import config
from utils import youtube_utils, rate_limiter, cache

# Setup logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Global variables
_last_request_time = {}
_request_count = {}

# Lifespan events
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events"""
    # Startup
    logger.info("🚀 YouTube Streaming API Server starting...")
    logger.info(f"📁 Download directory: {config.DOWNLOAD_DIR}")
    logger.info(f"🌐 Server will run on: http://{config.HOST}:{config.PORT}")
    
    if config.COOKIES_FILE and os.path.exists(config.COOKIES_FILE):
        logger.info("🍪 Cookies file detected")
    
    yield
    
    # Shutdown
    logger.info("👋 Shutting down YouTube Streaming API Server...")

# Create FastAPI app
app = FastAPI(
    title="YouTube Streaming API",
    version="2.0.0",
    description="High-performance YouTube streaming API server",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files (if needed)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Rate limiting middleware
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Rate limiting middleware"""
    client_ip = request.client.host if request.client else "unknown"
    
    # Check rate limit
    if not await rate_limiter.check_limit(client_ip):
        return JSONResponse(
            status_code=429,
            content={
                "error": "Rate limit exceeded",
                "message": "Please wait before making another request"
            }
        )
    
    # Process request
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    
    # Add headers
    response.headers["X-Process-Time"] = str(process_time)
    response.headers["X-RateLimit-Remaining"] = "unlimited"
    
    logger.info(f"{request.method} {request.url.path} - {response.status_code} - {process_time:.3f}s")
    
    return response

# Enhanced YouTubeDownloader class
class YouTubeDownloader:
    @staticmethod
    def get_ydl_options(video_type: str = "video", quality: str = "best"):
        """Get yt-dlp options with enhanced audio extraction"""
        ydl_opts = config.YTDLP_DEFAULT_OPTS.copy()
        
        # Add cookies if available (CRITICAL FOR AUDIO)
        if config.COOKIES_FILE and os.path.exists(config.COOKIES_FILE):
            ydl_opts['cookiefile'] = config.COOKIES_FILE
            logger.info(f"🍪 Using cookies from {config.COOKIES_FILE}")
        
        # Add proxy if configured
        if config.PROXY:
            ydl_opts['proxy'] = config.PROXY
            logger.info(f"🌐 Using proxy: {config.PROXY}")
        
        # Region bypass for India
        ydl_opts['geo_bypass'] = True
        ydl_opts['geo_bypass_country'] = 'IN'
        
        # Format selection
        if video_type == "audio":
            # Enhanced audio format selection
            ydl_opts['format'] = 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best'
            ydl_opts['postprocessors'] = []
            
            # Try multiple YouTube extractor settings
            ydl_opts['extractor_args'].update({
                'youtube': {
                    'player_client': ['android', 'ios', 'web', 'tvhtml5'],
                    'player_skip': ['configs', 'webpage'],
                    'skip': ['hls', 'dash'],
                }
            })
            
            # Don't require audio-only format, accept video+audio
            ydl_opts['format'] = 'best[acodec!=none]'
            
        elif video_type == "video":
            if quality == "low":
                ydl_opts['format'] = 'best[height<=360]'
            elif quality == "medium":
                ydl_opts['format'] = 'best[height<=480]'
            elif quality == "high":
                ydl_opts['format'] = 'best[height<=720]'
            else:  # best
                ydl_opts['format'] = 'best[height<=1080]/best'
        
        return ydl_opts
    
    @staticmethod
    async def get_audio_stream(url: str, use_cookies: bool = True) -> Dict[str, Any]:
        """Specialized method for audio streaming with multiple fallbacks"""
        video_id = youtube_utils.extract_video_id(url)
        logger.info(f"🎵 Attempting audio extraction for: {video_id}")
        
        # Method 1: Try with cookies (most likely to work)
        if use_cookies and config.COOKIES_FILE and os.path.exists(config.COOKIES_FILE):
            result = await YouTubeDownloader._try_audio_method(
                url, video_id, "cookies_method", use_cookies=True
            )
            if result['status'] == 'success':
                return result
        
        # Method 2: Try without audio-only restriction
        result = await YouTubeDownloader._try_audio_method(
            url, video_id, "mixed_format_method", accept_video=True
        )
        if result['status'] == 'success':
            return result
        
        # Method 3: Try DASH audio extraction
        result = await YouTubeDownloader._try_audio_method(
            url, video_id, "dash_method", extract_dash=True
        )
        if result['status'] == 'success':
            return result
        
        # Method 4: Last resort - try generic extractor
        result = await YouTubeDownloader._try_audio_method(
            url, video_id, "generic_method", force_generic=True
        )
        
        return result
    
    @staticmethod
    async def _try_audio_method(url: str, video_id: str, method_name: str, **kwargs) -> Dict[str, Any]:
        """Try a specific audio extraction method"""
        try:
            logger.info(f"🔄 Trying {method_name} for {video_id}")
            
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False,
                'socket_timeout': 30,
                'ignoreerrors': True,
            }
            
            # Add cookies if requested and available
            if kwargs.get('use_cookies') and config.COOKIES_FILE and os.path.exists(config.COOKIES_FILE):
                ydl_opts['cookiefile'] = config.COOKIES_FILE
            
            # Format selection based on method
            if method_name == "cookies_method":
                ydl_opts['format'] = 'bestaudio/best'
            elif method_name == "mixed_format_method":
                # Accept video+audio formats and extract audio
                ydl_opts['format'] = 'best[acodec!=none]'
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
            elif method_name == "dash_method":
                # Try DASH audio specifically
                ydl_opts['format'] = 'bestaudio[protocol=dash]/bestaudio'
            elif method_name == "generic_method":
                ydl_opts['force_generic_extractor'] = True
                ydl_opts['format'] = 'best'
            
            # Add proxy if configured
            if config.PROXY:
                ydl_opts['proxy'] = config.PROXY
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                if not info:
                    return {'status': 'error', 'message': 'No video info found'}
                
                formats = info.get('formats', [])
                
                # Find suitable format
                suitable_formats = []
                for fmt in formats:
                    # For mixed format method, accept any format with audio
                    if method_name == "mixed_format_method" and fmt.get('acodec') != 'none':
                        suitable_formats.append(fmt)
                    # For other methods, look for audio-only or best audio
                    elif fmt.get('acodec') != 'none' and (fmt.get('vcodec') == 'none' or method_name != "mixed_format_method"):
                        suitable_formats.append(fmt)
                
                if not suitable_formats:
                    return {'status': 'error', 'message': f'No suitable formats in {method_name}'}
                
                # Sort by bitrate/quality
                suitable_formats.sort(
                    key=lambda x: (
                        x.get('abr', 0) or x.get('tbr', 0) or 0,
                        x.get('asr', 0) or 0,
                        x.get('filesize', 0) or 0
                    ),
                    reverse=True
                )
                
                best_format = suitable_formats[0]
                
                return {
                    'status': 'success',
                    'video_id': video_id,
                    'title': info.get('title', 'Unknown Title'),
                    'duration': info.get('duration', 0),
                    'stream_url': best_format['url'],
                    'type': 'audio',
                    'format': {
                        'ext': best_format.get('ext', 'm4a'),
                        'abr': best_format.get('abr', 128),
                        'asr': best_format.get('asr', 44100),
                        'vcodec': best_format.get('vcodec', 'none'),
                        'acodec': best_format.get('acodec', 'none'),
                        'filesize': best_format.get('filesize'),
                        'protocol': best_format.get('protocol', ''),
                        'format_note': best_format.get('format_note', '')
                    },
                    'method_used': method_name
                }
                
        except Exception as e:
            logger.error(f"{method_name} failed: {e}")
            return {'status': 'error', 'message': f'{method_name}: {str(e)}'}
    
    @staticmethod
    async def get_stream_info(url: str, video_type: str = "video", quality: str = "best") -> Dict[str, Any]:
        """Get streaming information for YouTube URL"""
        try:
            video_id = youtube_utils.extract_video_id(url)
            if not video_id:
                raise ValueError("Invalid YouTube URL")
            
            # Check cache first
            cache_key = f"{video_id}:{video_type}:{quality}"
            cached_data = await cache.get(cache_key)
            if cached_data:
                logger.info(f"🎯 Cache hit for {video_id}")
                return cached_data
            
            logger.info(f"🔍 Processing: {video_id} | Type: {video_type} | Quality: {quality}")
            
            # Use specialized method for audio
            if video_type == "audio":
                result = await YouTubeDownloader.get_audio_stream(url)
            else:
                ydl_opts = YouTubeDownloader.get_ydl_options(video_type, quality)
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    
                    if not info:
                        raise ValueError("Could not extract video info")
                    
                    result = {
                        'status': 'success',
                        'video_id': video_id,
                        'title': info.get('title', 'Unknown Title'),
                        'duration': info.get('duration', 0),
                        'thumbnail': info.get('thumbnail', ''),
                        'channel': info.get('channel', 'Unknown Channel'),
                        'view_count': info.get('view_count', 0),
                        'like_count': info.get('like_count', 0),
                        'upload_date': info.get('upload_date', ''),
                    }
                    
                    # Find best video format
                    video_formats = [f for f in info.get('formats', []) 
                                   if f.get('vcodec') != 'none']
                    
                    if video_formats:
                        video_formats.sort(
                            key=lambda x: (
                                x.get('height', 0) or 0,
                                x.get('width', 0) or 0,
                                x.get('fps', 0) or 0
                            ),
                            reverse=True
                        )
                        best_video = video_formats[0]
                        result.update({
                            'stream_url': best_video['url'],
                            'type': 'video',
                            'format': {
                                'ext': best_video.get('ext', 'mp4'),
                                'height': best_video.get('height'),
                                'width': best_video.get('width'),
                                'fps': best_video.get('fps'),
                                'filesize': best_video.get('filesize'),
                                'format_note': best_video.get('format_note', '')
                            }
                        })
                    else:
                        raise ValueError("No suitable video format found")
            
            # Cache successful results
            if result['status'] == 'success':
                await cache.set(cache_key, result)
            
            return result
                
        except Exception as e:
            logger.error(f"❌ Error getting stream info: {e}")
            return {
                'status': 'error',
                'message': str(e),
                'video_id': youtube_utils.extract_video_id(url) or 'unknown'
            }

# Create downloader instance
downloader = YouTubeDownloader()

# API Endpoints

@app.get("/")
async def root():
    """Root endpoint with API info"""
    return {
        "service": "YouTube Streaming API",
        "version": "2.0.0",
        "status": "active",
        "timestamp": time.time(),
        "endpoints": {
            "/stream/video": "Stream video (GET, params: url, quality)",
            "/stream/audio": "Stream audio (GET, params: url)",
            "/info": "Get video info (GET, params: url)",
            "/search": "Search videos (GET, params: q, limit)",
            "/formats": "Get available formats (GET, params: url)",
            "/download/video": "Download video (GET, params: url, quality)",
            "/download/audio": "Download audio (GET, params: url)",
            "/health": "Health check (GET)",
            "/stats": "API statistics (GET)"
        },
        "note": "All endpoints support CORS. Use ?url=YOUTUBE_URL parameter."
    }

@app.get("/stream/video")
async def stream_video(
    request: Request,
    url: str = Query(..., description="YouTube video URL"),
    quality: str = Query("best", description="Quality: low, medium, high, best")
):
    """
    Stream YouTube video directly
    Returns redirect to direct stream URL
    """
    # Validate URL
    if not youtube_utils.is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    
    # Validate quality
    if quality not in ["low", "medium", "high", "best"]:
        raise HTTPException(status_code=400, detail="Invalid quality parameter")
    
    try:
        # Get stream info
        result = await downloader.get_stream_info(url, "video", quality)
        
        if result['status'] != 'success':
            raise HTTPException(status_code=500, detail=result.get('message', 'Stream error'))
        
        stream_url = result['stream_url']
        
        logger.info(f"🎬 Streaming video: {result.get('title', 'Unknown')} | Quality: {quality}")
        
        # Return redirect with proper headers
        response = RedirectResponse(url=stream_url, status_code=302)
        
        # Add headers for better streaming
        response.headers.update({
            "Accept-Ranges": "bytes",
            "Content-Type": "video/mp4",
            "Cache-Control": "public, max-age=7200",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "Content-Length,Content-Range",
            "X-Video-Title": youtube_utils.clean_title(result.get('title', '')),
            "X-Video-Id": result.get('video_id', ''),
            "X-Stream-Url": stream_url[:100] + "..." if len(stream_url) > 100 else stream_url
        })
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Stream video error: {e}")
        raise HTTPException(status_code=500, detail=f"Streaming error: {str(e)}")

@app.get("/stream/audio")
async def stream_audio(
    request: Request,
    url: str = Query(..., description="YouTube video URL"),
    force_refresh: bool = Query(False, description="Force refresh, bypass cache")
):
    """
    Stream YouTube audio directly with multiple fallback methods
    """
    if not youtube_utils.is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    
    try:
        video_id = youtube_utils.extract_video_id(url)
        
        # Check cache if not forcing refresh
        if not force_refresh:
            cache_key = f"audio:{video_id}"
            cached_result = await cache.get(cache_key)
            if cached_result and cached_result.get('status') == 'success':
                logger.info(f"🎵 Using cached audio for {video_id}")
                result = cached_result
            else:
                result = await downloader.get_audio_stream(url)
        else:
            result = await downloader.get_audio_stream(url)
        
        if result['status'] != 'success':
            # Try one more time without cookies if cookies method failed
            if "cookies" in result.get('message', '').lower():
                logger.info("🔄 Retrying without cookies...")
                result = await downloader.get_audio_stream(url, use_cookies=False)
            
            if result['status'] != 'success':
                raise HTTPException(
                    status_code=500,
                    detail=f"Audio extraction failed: {result.get('message', 'Unknown error')}. "
                          f"Video may be age-restricted or region-locked. Try adding cookies.txt file."
                )
        
        stream_url = result['stream_url']
        method_used = result.get('method_used', 'unknown')
        
        logger.info(f"🎵 Streaming audio: {result.get('title', 'Unknown')} | Method: {method_used}")
        
        # Return redirect with enhanced headers
        response = RedirectResponse(url=stream_url, status_code=302)
        
        response.headers.update({
            "Accept-Ranges": "bytes",
            "Content-Type": get_content_type(result.get('format', {}).get('ext', 'm4a')),
            "Cache-Control": "public, max-age=86400",  # 24 hours
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "*",
            "X-Audio-Title": youtube_utils.clean_title(result.get('title', '')),
            "X-Audio-Bitrate": str(result.get('format', {}).get('abr', 128)),
            "X-Audio-Codec": result.get('format', {}).get('acodec', 'unknown'),
            "X-Video-Codec": result.get('format', {}).get('vcodec', 'none'),
            "X-Video-Id": result.get('video_id', ''),
            "X-Extraction-Method": method_used,
            "X-Stream-Url-Hash": hashlib.md5(stream_url.encode()).hexdigest()[:8]
        })
        
        # Cache successful result
        if result['status'] == 'success':
            cache_key = f"audio:{video_id}"
            await cache.set(cache_key, result)
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"🎵 Stream audio error: {e}")
        raise HTTPException(status_code=500, detail=f"Audio streaming error: {str(e)}")


def get_content_type(ext: str) -> str:
    """Get content type based on file extension"""
    content_types = {
        'mp3': 'audio/mpeg',
        'm4a': 'audio/mp4',
        'webm': 'audio/webm',
        'ogg': 'audio/ogg',
        'opus': 'audio/ogg',
        'flac': 'audio/flac',
        'wav': 'audio/wav',
        'aac': 'audio/aac',
    }
    return content_types.get(ext.lower(), 'audio/mpeg')

@app.get("/info")
async def get_video_info(
    url: str = Query(..., description="YouTube video URL")
):
    """Get detailed video information"""
    if not youtube_utils.is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    
    try:
        video_id = youtube_utils.extract_video_id(url)
        
        # Check cache
        cache_key = f"info:{video_id}"
        cached_info = await cache.get(cache_key)
        if cached_info:
            return cached_info
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'skip_download': True,
        }
        
        if config.COOKIES_FILE and os.path.exists(config.COOKIES_FILE):
            ydl_opts['cookiefile'] = config.COOKIES_FILE
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            if not info:
                raise HTTPException(status_code=404, detail="Video not found")
            
            # Format response
            video_info = {
                'video_id': video_id,
                'title': info.get('title'),
                'description': info.get('description', '')[:500] + '...' if info.get('description') else '',
                'duration': info.get('duration'),
                'duration_formatted': youtube_utils.format_duration(info.get('duration', 0)),
                'thumbnail': info.get('thumbnail'),
                'channel': info.get('channel'),
                'channel_id': info.get('channel_id'),
                'view_count': info.get('view_count'),
                'like_count': info.get('like_count'),
                'upload_date': info.get('upload_date'),
                'categories': info.get('categories', []),
                'tags': info.get('tags', [])[:10],
                'age_limit': info.get('age_limit', 0),
                'is_live': info.get('is_live', False),
                'formats_count': len(info.get('formats', [])),
                'webpage_url': info.get('webpage_url'),
            }
            
            # Get available formats summary
            formats_summary = []
            for fmt in info.get('formats', []):
                if fmt.get('filesize') or fmt.get('filesize_approx'):
                    formats_summary.append({
                        'format_id': fmt.get('format_id'),
                        'ext': fmt.get('ext'),
                        'resolution': fmt.get('resolution', 'N/A'),
                        'filesize': fmt.get('filesize') or fmt.get('filesize_approx'),
                        'vcodec': fmt.get('vcodec', 'none'),
                        'acodec': fmt.get('acodec', 'none'),
                        'format_note': fmt.get('format_note', '')
                    })
            
            video_info['formats'] = formats_summary[:20]  # Limit to 20 formats
            
            # Cache the info
            await cache.set(cache_key, video_info)
            
            return video_info
            
    except Exception as e:
        logger.error(f"Info error: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting info: {str(e)}")

@app.get("/search")
async def search_videos(
    q: str = Query(..., description="Search query"),
    limit: int = Query(10, ge=1, le=50, description="Number of results (1-50)")
):
    """Search YouTube videos"""
    if not q or len(q.strip()) < 2:
        return {
            "success": False,
            "query": q,
            "results": [],
            "error": "Search query too short"
        }
    
    try:
        # Check cache
        cache_key = f"search:{q}:{limit}"
        cached_results = await cache.get(cache_key)
        if cached_results:
            cached_results["success"] = True
            return cached_results
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'default_search': f'ytsearch{limit}',
            'skip_download': True,
        }
        
        if config.COOKIES_FILE and os.path.exists(config.COOKIES_FILE):
            ydl_opts['cookiefile'] = config.COOKIES_FILE
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
            
            results = []
            for entry in info.get('entries', []):
                if entry and entry.get('id'):
                    results.append({
                        'video_id': entry.get('id'),
                        'title': entry.get('title', 'No Title'),
                        'duration': entry.get('duration'),
                        'duration_formatted': youtube_utils.format_duration(entry.get('duration', 0)),
                        'thumbnail': entry.get('thumbnail'),
                        'channel': entry.get('channel'),
                        'view_count': entry.get('view_count'),
                        'upload_date': entry.get('upload_date'),
                        'url': f"https://youtube.com/watch?v={entry.get('id')}",
                    })
            
            response = {
                'success': True,
                'query': q,
                'count': len(results),
                'results': results[:limit]
            }
            
            # Cache results
            await cache.set(cache_key, response)
            
            return response
            
    except Exception as e:
        logger.error(f"Search error: {e}")
        return {
            "success": False,
            "query": q,
            "results": [],
            "error": str(e)
        }

@app.get("/formats")
async def get_available_formats(
    url: str = Query(..., description="YouTube video URL")
):
    """Get all available formats for a video"""
    if not youtube_utils.is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    
    try:
        video_id = youtube_utils.extract_video_id(url)
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'listformats': True,
            'skip_download': True,
        }
        
        if config.COOKIES_FILE and os.path.exists(config.COOKIES_FILE):
            ydl_opts['cookiefile'] = config.COOKIES_FILE
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            formats = []
            for fmt in info.get('formats', []):
                if fmt.get('filesize') or fmt.get('filesize_approx'):
                    formats.append({
                        'format_id': fmt.get('format_id'),
                        'ext': fmt.get('ext'),
                        'resolution': fmt.get('resolution', 'N/A'),
                        'filesize': fmt.get('filesize') or fmt.get('filesize_approx'),
                        'filesize_mb': round((fmt.get('filesize') or fmt.get('filesize_approx') or 0) / (1024 * 1024), 2),
                        'vcodec': fmt.get('vcodec', 'none'),
                        'acodec': fmt.get('acodec', 'none'),
                        'format_note': fmt.get('format_note', ''),
                        'fps': fmt.get('fps'),
                        'tbr': fmt.get('tbr'),  # Average bitrate
                        'protocol': fmt.get('protocol', '')
                    })
            
            # Sort by resolution/filesize
            formats.sort(key=lambda x: (
                x.get('resolution', '0x0'),
                x.get('filesize', 0)
            ), reverse=True)
            
            return {
                'video_id': video_id,
                'title': info.get('title', 'Unknown'),
                'total_formats': len(formats),
                'formats': formats
            }
            
    except Exception as e:
        logger.error(f"Formats error: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting formats: {str(e)}")

@app.get("/download/video")
async def download_video(
    url: str = Query(..., description="YouTube video URL"),
    quality: str = Query("best", description="Quality: low, medium, high, best")
):
    """
    Download video file directly
    Returns the video file as attachment
    """
    if not youtube_utils.is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    
    try:
        result = await downloader.get_stream_info(url, "video", quality)
        
        if result['status'] != 'success':
            raise HTTPException(status_code=500, detail=result.get('message', 'Download error'))
        
        stream_url = result['stream_url']
        video_title = youtube_utils.clean_title(result.get('title', 'video'))
        video_id = result.get('video_id', 'download')
        
        # Download file
        import aiohttp
        
        async def file_generator():
            async with aiohttp.ClientSession() as session:
                async with session.get(stream_url) as response:
                    # Set content length header
                    content_length = response.headers.get('Content-Length')
                    
                    # Stream content
                    async for chunk in response.content.iter_chunked(8192):
                        yield chunk
        
        filename = f"{video_title}_{video_id}.mp4"
        
        return StreamingResponse(
            file_generator(),
            media_type="video/mp4",
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Accept-Ranges': 'bytes',
                'Cache-Control': 'public, max-age=3600',
                'X-Video-Title': video_title,
                'X-Video-Id': video_id
            }
        )
        
    except Exception as e:
        logger.error(f"Download video error: {e}")
        raise HTTPException(status_code=500, detail=f"Download error: {str(e)}")

@app.get("/download/audio")
async def download_audio(
    url: str = Query(..., description="YouTube video URL")
):
    """Download audio file directly"""
    if not youtube_utils.is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    
    try:
        result = await downloader.get_stream_info(url, "audio")
        
        if result['status'] != 'success':
            raise HTTPException(status_code=500, detail=result.get('message', 'Download error'))
        
        stream_url = result['stream_url']
        audio_title = youtube_utils.clean_title(result.get('title', 'audio'))
        video_id = result.get('video_id', 'download')
        
        import aiohttp
        
        async def file_generator():
            async with aiohttp.ClientSession() as session:
                async with session.get(stream_url) as response:
                    async for chunk in response.content.iter_chunked(8192):
                        yield chunk
        
        filename = f"{audio_title}_{video_id}.mp3"
        
        return StreamingResponse(
            file_generator(),
            media_type="audio/mpeg",
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Accept-Ranges': 'bytes',
                'Cache-Control': 'public, max-age=3600',
                'X-Audio-Title': audio_title,
                'X-Video-Id': video_id
            }
        )
        
    except Exception as e:
        logger.error(f"Download audio error: {e}")
        raise HTTPException(status_code=500, detail=f"Download error: {str(e)}")

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "service": "YouTube Streaming API",
        "version": "2.0.0",
        "cache_size": len(cache.cache),
        "rate_limits": len(rate_limiter.requests)
    }

@app.get("/stats")
async def api_statistics():
    """API statistics"""
    return {
        "total_requests": sum(_request_count.values()),
        "requests_by_endpoint": _request_count,
        "cache_hits": getattr(cache, 'hits', 0),
        "cache_misses": getattr(cache, 'misses', 0),
        "cache_size": len(cache.cache),
        "uptime": time.time() - getattr(app, 'start_time', time.time()),
        "rate_limited_ips": len(rate_limiter.requests)
    }

@app.get("/clear-cache")
async def clear_cache():
    """Clear all cache (admin endpoint)"""
    cache.cache.clear()
    return {"status": "success", "message": "Cache cleared"}

# WebSocket endpoint for real-time updates
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time updates"""
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            
            # Handle different commands
            if data == "ping":
                await websocket.send_json({"type": "pong", "timestamp": time.time()})
            elif data.startswith("info:"):
                video_url = data[5:]
                if youtube_utils.is_valid_youtube_url(video_url):
                    result = await downloader.get_stream_info(video_url, "video")
                    await websocket.send_json(result)
                else:
                    await websocket.send_json({"error": "Invalid URL"})
            else:
                await websocket.send_json({"type": "message", "text": f"Received: {data}"})
                
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
        

# Error handlers
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """Handle HTTP exceptions"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "HTTP Error",
            "message": exc.detail,
            "status_code": exc.status_code,
            "path": request.url.path
        }
    )

@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """Handle general exceptions"""
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "message": "An unexpected error occurred",
            "path": request.url.path
        }
    )

if __name__ == "__main__":
    # Record startup time
    app.start_time = time.time()
    
    # Print startup banner
    print("=" * 60)
    print("🎬 YOUTUBE STREAMING API SERVER v2.0.0")
    print("=" * 60)
    print(f"📁 Download directory: {config.DOWNLOAD_DIR}")
    print(f"🌐 Server URL: http://{config.HOST}:{config.PORT}")
    print(f"📚 Documentation: http://{config.HOST}:{config.PORT}/docs")
    print(f"📊 Health check: http://{config.HOST}:{config.PORT}/health")
    print("=" * 60)
    
    if config.COOKIES_FILE and os.path.exists(config.COOKIES_FILE):
        print("🍪 Cookies file: DETECTED")
    else:
        print("⚠️  Cookies file: NOT DETECTED (age-restricted videos may not work)")
    
    print("🚀 Starting server...")
    print("=" * 60)
    
    # Start server
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level="info",
        access_log=True,
        timeout_keep_alive=30
    )