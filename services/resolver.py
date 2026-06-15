"""
services/resolver.py
Resolves user queries / URLs into streamable Track objects.
Supports: YouTube search, YouTube URLs, Spotify (metadata → YT), direct files.
Uses yt-dlp for extraction — no third-party paid APIs required.
"""

import asyncio
import logging
import re
from typing import Optional
from functools import lru_cache

import yt_dlp

from streaming.engine import Track
from config.settings import cfg

logger = logging.getLogger("services.resolver")

# ── yt-dlp options ────────────────────────────────────────────────────────────
_YDL_BASE = {
    "quiet":            True,
    "no_warnings":      True,
    "extract_flat":     False,
    "skip_download":    True,
    "force_generic_extractor": False,
    "ignoreerrors":     True,
    "geo_bypass":       True,
    "source_address":   "0.0.0.0", # Force IPv4
    # Spoof a real browser user-agent to reduce bot detection
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    },
}


_QUALITY_OPTS = {
    "high":   "bestaudio",
    "medium": "bestaudio",
    "low":    "bestaudio",
}

# Detect raw YouTube URL
_YT_REGEX = re.compile(
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+"
)
# Detect Spotify URL
_SP_REGEX = re.compile(
    r"https://open\.spotify\.com/(track|album|playlist)/([a-zA-Z0-9]+)"
)


class Resolver:
    """
    Async resolver — all heavy yt-dlp work runs in a thread pool
    so it never blocks the event loop.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._spotify = None
        self._cookie_file = None
        
        # Write cookies from environment variable to a file if provided
        if cfg.YOUTUBE_COOKIES:
            try:
                import tempfile
                # Create a secure temporary file for cookies
                fd, path = tempfile.mkstemp(suffix="_cookies.txt", prefix="ghostmusic_")
                import os
                with os.fdopen(fd, "w") as f:
                    f.write(cfg.YOUTUBE_COOKIES)
                self._cookie_file = path
                logger.info(f"Loaded YouTube cookies into {self._cookie_file}")
            except Exception as e:
                logger.error(f"Failed to write YOUTUBE_COOKIES to file: {e}")

        if cfg.SPOTIFY_CLIENT_ID and cfg.SPOTIFY_SECRET:
            try:
                import spotipy
                from spotipy.oauth2 import SpotifyClientCredentials
                self._spotify = spotipy.Spotify(
                    auth_manager=SpotifyClientCredentials(
                        client_id=cfg.SPOTIFY_CLIENT_ID,
                        client_secret=cfg.SPOTIFY_SECRET,
                    )
                )
                logger.info("Spotify integration enabled.")
            except ImportError:
                logger.warning("spotipy not installed — Spotify links won't work.")


    # ─────────────────────────────────────────────────────────────────────────
    async def resolve(
        self,
        query: str,
        requester_id: int,
        requester_name: str,
        chat_id: int,
    ) -> list[Track]:
        """
        Returns a list of Tracks (1 for single song, many for playlist/album).
        Raises ValueError with a user-friendly message on failure.
        """
        query = query.strip()

        # Spotify URL
        if _SP_REGEX.match(query):
            return await self._resolve_spotify(query, requester_id, requester_name, chat_id)

        # YouTube URL or plain search
        return await self._resolve_youtube(query, requester_id, requester_name, chat_id)

    # ─────────────────────────────────────────────────────────────────────────
    async def _resolve_youtube(
        self, query: str, req_id: int, req_name: str, chat_id: int
    ) -> list[Track]:
        # Convert plain text to youtube url via youtube-search-python
        search_query = query
        song_title_fallback = query if not _YT_REGEX.match(query) else "Unknown"

        if not _YT_REGEX.match(query):
            resolved_via_api = False
            if cfg.YOUTUBE_API_KEY:
                try:
                    import aiohttp
                    url = "https://www.googleapis.com/youtube/v3/search"
                    params = {
                        "part": "snippet",
                        "q": query,
                        "type": "video",
                        "maxResults": 1,
                        "key": cfg.YOUTUBE_API_KEY
                    }
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, params=params) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if data.get("items"):
                                    video_id = data["items"][0]["id"]["videoId"]
                                    search_query = f"https://www.youtube.com/watch?v={video_id}"
                                    song_title_fallback = data["items"][0]["snippet"]["title"]
                                    resolved_via_api = True
                                    logger.info(f"Resolved query '{query}' via YouTube API to '{song_title_fallback}'")
                except Exception as e:
                    logger.error(f"YouTube Data API search failed: {e}")

            if not resolved_via_api:
                try:
                    from youtubesearchpython.__future__ import VideosSearch
                    videosSearch = VideosSearch(query, limit=1)
                    videosResult = await videosSearch.next()
                    if videosResult and videosResult.get('result'):
                        search_query = videosResult['result'][0]['link']
                        song_title_fallback = videosResult['result'][0]['title']
                    else:
                        raise ValueError("No results found for that query.")
                except ValueError as e:
                    raise e
                except Exception as e:
                    logger.error(f"youtube-search-python error: {e}")
                    # Keep search_query as-is but let song_title_fallback = query
                    # so the SoundCloud fallback below can resolve it instead of
                    # hitting YouTube again with ytsearch5: (which also gets blocked)
                    song_title_fallback = query
                    search_query = f"ytsearch1:{query}"  # Try anyway, may work with new clients


        opts = {
            **_YDL_BASE,
            "format": _QUALITY_OPTS.get(cfg.STREAM_QUALITY, _QUALITY_OPTS["high"]),
        }
        if self._cookie_file:
            opts["cookiefile"] = self._cookie_file
        elif cfg.YOUTUBE_CLIENT_ID and cfg.YOUTUBE_CLIENT_SECRET:
            # Enable official YouTube OAuth2 authentication via yt-dlp native options
            opts["username"] = "oauth2"
            opts["password"] = ""
            # Inject client credentials dynamically via youtube extractor args
            opts["extractor_args"] = {
                "youtube": {
                    "oauth_client_id": [cfg.YOUTUBE_CLIENT_ID],
                    "oauth_client_secret": [cfg.YOUTUBE_CLIENT_SECRET]
                }
            }
            logger.info("Configured yt-dlp to use YouTube OAuth2 with custom Client credentials")
        else:
            # Fallback when no cookies/oauth: use clients that sometimes work without login
            opts["extractor_args"] = {"youtube": {"player_client": ["tv_embedded,mweb"]}}


        # Add proxy configuration if defined
        if cfg.YOUTUBE_PROXY:
            opts["proxy"] = cfg.YOUTUBE_PROXY




        loop = asyncio.get_event_loop()
        
        async def do_extract(sq):
            return await loop.run_in_executor(None, lambda: self._extract(sq, opts))

        try:
            info = await do_extract(search_query)
        except Exception as e:
            logger.error(f"yt-dlp error: {e}")
            info = None

        def extract_tracks(inf):
            if not inf: return []
            entries = inf.get("entries") or [inf]
            if "ytsearch" in search_query or "scsearch" in search_query:
                entries = entries[:1]
            trks = []
            for entry in entries:
                if not entry: continue
                # yt-dlp puts stream url in 'url'. 'webpage_url' is the video link.
                # If 'url' is missing, it means extraction failed (e.g. YouTube blocked IP).
                url = entry.get("url")
                if not url: continue
                
                trks.append(Track(
                    title=entry.get("title", "Unknown"),
                    url=url,
                    webpage=entry.get("webpage_url", query),
                    duration=int(entry.get("duration") or 0),
                    thumbnail=entry.get("thumbnail", ""),
                    requester_id=req_id,
                    requester_name=req_name,
                    chat_id=chat_id,
                    source="youtube" if "soundcloud" not in url else "soundcloud",
                ))
            return trks

        tracks = extract_tracks(info)

        # ── AUTO FALLBACK TO SOUNDCLOUD ──
        # If YouTube blocks the stream extraction, we silently fallback to SoundCloud
        # using the title of the video we wanted.
        if not tracks:
            logger.warning(f"YouTube extraction failed for {search_query}. Falling back to SoundCloud...")
            
            # If the original query was a direct YouTube link, try to get its title
            if _YT_REGEX.match(query) and info and info.get("title"):
                song_title_fallback = info.get("title")
                
            if song_title_fallback and song_title_fallback != "Unknown":
                sc_query = f"scsearch1:{song_title_fallback}"
                try:
                    sc_info = await do_extract(sc_query)
                    tracks = extract_tracks(sc_info)
                except Exception as e:
                    logger.error(f"SoundCloud fallback error: {e}")
            
        if not tracks:
            raise ValueError("Couldn't extract a streamable URL. Try a different search term.")
        return tracks

    async def _resolve_spotify(
        self, url: str, req_id: int, req_name: str, chat_id: int
    ) -> list[Track]:
        if not self._spotify:
            raise ValueError("Spotify integration is not configured.")

        m = _SP_REGEX.match(url)
        kind, sp_id = m.group(1), m.group(2)

        loop = asyncio.get_event_loop()

        try:
            if kind == "track":
                data = await loop.run_in_executor(None, lambda: self._spotify.track(sp_id))
                searches = [f"{data['name']} {data['artists'][0]['name']}"]
            elif kind == "playlist":
                data   = await loop.run_in_executor(None, lambda: self._spotify.playlist_tracks(sp_id))
                items  = data["items"][:cfg.MAX_PLAYLIST_SIZE]
                searches = [
                    f"{i['track']['name']} {i['track']['artists'][0]['name']}"
                    for i in items if i.get("track")
                ]
            elif kind == "album":
                data   = await loop.run_in_executor(None, lambda: self._spotify.album_tracks(sp_id))
                items  = data["items"][:cfg.MAX_PLAYLIST_SIZE]
                searches = [
                    f"{i['name']} {i['artists'][0]['name']}"
                    for i in items
                ]
            else:
                raise ValueError("Unsupported Spotify link type.")
        except Exception as e:
            logger.error(f"Spotify error: {e}")
            raise ValueError("Failed to fetch Spotify data.")

        # Resolve each search term → YouTube
        tracks = []
        for s in searches[:cfg.MAX_PLAYLIST_SIZE]:
            try:
                result = await self._resolve_youtube(s, req_id, req_name, chat_id)
                if result:
                    t = result[0]
                    t.source = "spotify"
                    tracks.append(t)
            except Exception:
                pass
        if not tracks:
            raise ValueError("Could not find any Spotify tracks on YouTube.")
        return tracks

    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _extract(query: str, opts: dict) -> Optional[dict]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)

    async def resolve_for_search_inline(self, query: str) -> list[dict]:
        """
        Used by inline mode: returns lightweight dicts (no full extraction).
        """
        if cfg.YOUTUBE_API_KEY:
            try:
                import aiohttp
                url = "https://www.googleapis.com/youtube/v3/search"
                params = {
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "maxResults": 5,
                    "key": cfg.YOUTUBE_API_KEY
                }
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            results = []
                            for item in data.get("items", []):
                                video_id = item["id"].get("videoId")
                                if not video_id:
                                    continue
                                snippet = item.get("snippet", {})
                                results.append({
                                    "id":       video_id,
                                    "title":    snippet.get("title", "Unknown"),
                                    "url":      f"https://www.youtube.com/watch?v={video_id}",
                                    "duration": 0, # API search doesn't return duration directly without video call
                                    "thumbnail": snippet.get("thumbnails", {}).get("default", {}).get("url", ""),
                                })
                            return results
            except Exception as e:
                logger.error(f"YouTube Data API inline search failed: {e}")

        try:
            from youtubesearchpython.__future__ import VideosSearch
            videosSearch = VideosSearch(query, limit=5)
            videosResult = await videosSearch.next()
            if not videosResult or not videosResult.get('result'):
                return []
            
            results = []
            for e in videosResult['result']:
                # Parse duration like '4:20' to seconds
                dur_str = e.get('duration', '0')
                try:
                    parts = dur_str.split(':')
                    if len(parts) == 3:
                        dur_sec = int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
                    elif len(parts) == 2:
                        dur_sec = int(parts[0])*60 + int(parts[1])
                    else:
                        dur_sec = int(parts[0])
                except:
                    dur_sec = 0

                results.append({
                    "id":       e.get("id", ""),
                    "title":    e.get("title", "Unknown"),
                    "url":      e.get("link") or f"https://youtu.be/{e.get('id', '')}",
                    "duration": dur_sec,
                    "thumbnail": e.get("thumbnails", [{}])[0].get("url", ""),
                })
            return results
        except Exception as e:
            logger.error(f"youtube-search-python inline error: {e}")
            return []



# Singleton
resolver = Resolver()
