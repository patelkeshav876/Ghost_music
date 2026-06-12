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
    # Bypass YouTube IP Blocks on Render/Cloud servers
    "extractor_args":   {"youtube": {"client": ["android", "ios"]}},
    "source_address":   "0.0.0.0", # Force IPv4
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
                search_query = f"ytsearch5:{query}" # Fallback

        opts = {
            **_YDL_BASE,
            "format": _QUALITY_OPTS.get(cfg.STREAM_QUALITY, _QUALITY_OPTS["high"]),
        }

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
