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
        # Convert plain text to ytsearch
        search_query = query if _YT_REGEX.match(query) else f"ytsearch5:{query}"

        opts = {
            **_YDL_BASE,
            "format": _QUALITY_OPTS.get(cfg.STREAM_QUALITY, _QUALITY_OPTS["high"]),
        }

        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(
                None, lambda: self._extract(search_query, opts)
            )
        except Exception as e:
            logger.error(f"yt-dlp error: {e}")
            raise ValueError(f"Could not fetch audio. Try a different search term.")

        if not info:
            raise ValueError("No results found for that query.")

        entries = info.get("entries") or [info]
        # For search results show first result only; for playlists return all
        if "ytsearch" in search_query:
            entries = entries[:1]

        tracks = []
        for entry in entries:
            if not entry:
                continue
            url = entry.get("url") or entry.get("webpage_url")
            if not url:
                continue
            tracks.append(Track(
                title=entry.get("title", "Unknown"),
                url=url,
                webpage=entry.get("webpage_url", query),
                duration=int(entry.get("duration") or 0),
                thumbnail=entry.get("thumbnail", ""),
                requester_id=req_id,
                requester_name=req_name,
                chat_id=chat_id,
                source="youtube",
            ))
        if not tracks:
            raise ValueError("Couldn't extract a streamable URL.")
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
        opts = {**_YDL_BASE, "extract_flat": True}
        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(
                None, lambda: self._extract(f"ytsearch5:{query}", opts)
            )
        except Exception:
            return []
        entries = (info or {}).get("entries", [])
        return [
            {
                "id":       e.get("id", ""),
                "title":    e.get("title", "Unknown"),
                "url":      e.get("url") or f"https://youtu.be/{e.get('id', '')}",
                "duration": e.get("duration", 0),
                "thumbnail":e.get("thumbnail", ""),
            }
            for e in entries if e
        ]


# Singleton
resolver = Resolver()
