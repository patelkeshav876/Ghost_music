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
        
        # Support multiple cookies separated by ===NEXT_ACCOUNT===
        self._cookie_files = []
        self._active_cookie_idx = 0
        
        if cfg.YOUTUBE_COOKIES:
            try:
                import tempfile
                import os
                # Split raw cookie content by delimiter
                accounts_cookies = [c.strip() for c in cfg.YOUTUBE_COOKIES.split("===NEXT_ACCOUNT===") if c.strip()]
                for i, cookie_content in enumerate(accounts_cookies):
                    fd, path = tempfile.mkstemp(suffix=f"_cookies_{i}.txt", prefix="ghostmusic_")
                    with os.fdopen(fd, "w") as f:
                        f.write(cookie_content)
                    self._cookie_files.append(path)
                logger.info(f"Loaded {len(self._cookie_files)} YouTube cookie account(s).")
            except Exception as e:
                logger.error(f"Failed to write YOUTUBE_COOKIES to files: {e}")


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

        # Direct YouTube link → skip JioSaavn and go to YouTube directly
        if _YT_REGEX.match(query):
            return await self._resolve_youtube(query, requester_id, requester_name, chat_id)

        # Plain text search → Search JioSaavn first!
        try:
            tracks = await self._resolve_jiosaavn(query, requester_id, requester_name, chat_id)
            if tracks:
                return tracks
        except Exception as e:
            logger.warning(f"JioSaavn resolution failed: {e}")

        # Search Jamendo next!
        try:
            tracks = await self._resolve_jamendo(query, requester_id, requester_name, chat_id)
            if tracks:
                return tracks
        except Exception as e:
            logger.warning(f"Jamendo resolution failed: {e}")

        # Search Archive.org next!
        try:
            tracks = await self._resolve_archive(query, requester_id, requester_name, chat_id)
            if tracks:
                return tracks
        except Exception as e:
            logger.warning(f"Archive.org resolution failed: {e}")

        # Fallback to YouTube
        return await self._resolve_youtube(query, requester_id, requester_name, chat_id)

    # ─────────────────────────────────────────────────────────────────────────
    async def _resolve_jamendo(
        self, query: str, req_id: int, req_name: str, chat_id: int
    ) -> list[Track]:
        import aiohttp
        url = "https://api.jamendo.com/v3.0/tracks/"
        params = {
            "client_id": "56d30c95",
            "format": "json",
            "limit": 1,
            "namesearch": query,
            "include": "musicinfo"
        }
        try:
            logger.info(f"Searching Jamendo for '{query}'")
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=6) as resp:
                    if resp.status == 200:
                        payload = await resp.json()
                        results = payload.get("results", [])
                        if results:
                            song = results[0]
                            stream_url = song.get("audio")
                            if stream_url:
                                track = Track(
                                    title=song.get("name", "Unknown Jamendo Song"),
                                    url=stream_url,
                                    webpage=song.get("shareurl", stream_url),
                                    duration=int(song.get("duration") or 0),
                                    thumbnail=song.get("image") or "",
                                    requester_id=req_id,
                                    requester_name=req_name,
                                    chat_id=chat_id,
                                    source="jiosaavn", # UI icon compatibility
                                )
                                logger.info(f"Successfully resolved '{query}' via Jamendo to '{track.title}'")
                                return [track]
        except Exception as e:
            logger.error(f"Jamendo API failed: {e}")
        return []

    # ─────────────────────────────────────────────────────────────────────────
    async def _resolve_archive(
        self, query: str, req_id: int, req_name: str, chat_id: int
    ) -> list[Track]:
        import aiohttp
        search_url = "https://advancedsearch.php" # we will use query on main domain
        # Advanced search endpoint
        url = "https://archive.org/advancedsearch.php"
        params = {
            "q": f"title:({query}) AND mediatype:(audio)",
            "fl[]": "identifier,title,downloads",
            "sort[]": "downloads desc",
            "rows": 1,
            "output": "json"
        }
        try:
            logger.info(f"Searching Archive.org for '{query}'")
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=6) as resp:
                    if resp.status == 200:
                        payload = await resp.json()
                        docs = payload.get("response", {}).get("docs", [])
                        if docs:
                            doc = docs[0]
                            identifier = doc.get("identifier")
                            title = doc.get("title", "Archive Song")
                            
                            # Get files for this identifier
                            meta_url = f"https://archive.org/metadata/{identifier}"
                            async with session.get(meta_url, timeout=6) as meta_resp:
                                if meta_resp.status == 200:
                                    meta_data = await meta_resp.json()
                                    files = meta_data.get("files", [])
                                    # Find mp3 files
                                    mp3_files = [
                                        f for f in files 
                                        if f.get("name", "").endswith(".mp3")
                                    ]
                                    if mp3_files:
                                        # Pick largest or first mp3 file
                                        mp3_file = max(mp3_files, key=lambda f: int(f.get("size") or 0))
                                        filename = mp3_file["name"]
                                        stream_url = f"https://archive.org/download/{identifier}/{filename}"
                                        
                                        track = Track(
                                            title=title,
                                            url=stream_url,
                                            webpage=f"https://archive.org/details/{identifier}",
                                            duration=int(float(mp3_file.get("length") or 0)),
                                            thumbnail="",
                                            requester_id=req_id,
                                            requester_name=req_name,
                                            chat_id=chat_id,
                                            source="jiosaavn", # UI icon compatibility
                                        )
                                        logger.info(f"Successfully resolved '{query}' via Archive.org to '{track.title}'")
                                        return [track]
        except Exception as e:
            logger.error(f"Archive.org query failed: {e}")
        return []


    # ─────────────────────────────────────────────────────────────────────────
    async def _resolve_jiosaavn(
        self, query: str, req_id: int, req_name: str, chat_id: int
    ) -> list[Track]:
        import aiohttp
        # Try a few popular public API instances
        endpoints = [
            "https://saavn.dev/api/search/songs",
            "https://saavn.sumit.co/api/search/songs",
            "https://jiosaavn-api.vercel.app/api/search/songs"
        ]
        
        for base_url in endpoints:
            try:
                logger.info(f"Searching JioSaavn via {base_url} for '{query}'")
                async with aiohttp.ClientSession() as session:
                    async with session.get(base_url, params={"query": query}, timeout=6) as resp:
                        if resp.status == 200:
                            payload = await resp.json()
                            data = payload.get("data", {})
                            results = data.get("results", []) if isinstance(data, dict) else data
                            
                            if not results:
                                continue
                                
                            song = results[0]
                            # Find highest quality download URL
                            download_urls = song.get("downloadUrl", [])
                            if not download_urls:
                                continue
                            
                            # Sort by quality if possible (e.g. 320kbps > 160kbps > 96kbps)
                            best_stream = None
                            for qual in ["320kbps", "160kbps", "96kbps", "48kbps"]:
                                found = [u for u in download_urls if u.get("quality") == qual]
                                if found:
                                    best_stream = found[0]
                                    break
                            
                            if not best_stream:
                                best_stream = download_urls[-1]
                                
                            stream_url = best_stream.get("link") or best_stream.get("url")
                            if not stream_url:
                                continue
                                
                            # Image resolution fallback
                            images = song.get("image", [])
                            thumb = ""
                            if images:
                                # Pick highest resolution image
                                thumb = images[-1].get("link") or images[-1].get("url") or ""
                                
                            # Duration
                            dur = int(song.get("duration") or 0)
                            
                            track = Track(
                                title=song.get("name", "Unknown JioSaavn Song"),
                                url=stream_url,
                                webpage=stream_url, # Stream is direct URL
                                duration=dur,
                                thumbnail=thumb,
                                requester_id=req_id,
                                requester_name=req_name,
                                chat_id=chat_id,
                                source="jiosaavn",
                            )
                            logger.info(f"Successfully resolved '{query}' via JioSaavn API to '{track.title}'")
                            return [track]
            except Exception as e:
                logger.error(f"JioSaavn API {base_url} failed: {e}")
                
        return []


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
        if self._cookie_files:
            # Inject currently active cookie
            active_idx = self._active_cookie_idx % len(self._cookie_files)
            opts["cookiefile"] = self._cookie_files[active_idx]
            logger.info(f"Using YouTube cookie file {active_idx + 1} of {len(self._cookie_files)}")

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
        
        async def do_extract(sq, current_opts):
            return await loop.run_in_executor(None, lambda: self._extract(sq, current_opts))

        max_attempts = max(1, len(self._cookie_files))
        attempt = 0
        info = None

        while attempt < max_attempts:
            # Re-build opts dynamically for each attempt to pick up rotated cookie file
            current_opts = {**opts}
            if self._cookie_files:
                active_idx = (self._active_cookie_idx + attempt) % len(self._cookie_files)
                current_opts["cookiefile"] = self._cookie_files[active_idx]
                logger.info(f"Extraction attempt {attempt + 1}: Using YouTube cookie file {active_idx + 1} of {len(self._cookie_files)}")
            
            try:
                info = await do_extract(search_query, current_opts)
                if info and (info.get("url") or (info.get("entries") and info.get("entries")[0] and info.get("entries")[0].get("url"))):
                    # Successfully extracted with working stream URL
                    if attempt > 0:
                        # Update the master active index so subsequent runs start here
                        self._active_cookie_idx = (self._active_cookie_idx + attempt) % len(self._cookie_files)
                        logger.info(f"Rotated active cookie index to {self._active_cookie_idx + 1}")
                    break
            except Exception as e:
                logger.error(f"Extraction attempt {attempt + 1} failed: {e}")
                
            # If we didn't get valid stream urls, treat as failed and rotate
            if not info or not (info.get("url") or (info.get("entries") and info.get("entries")[0] and info.get("entries")[0].get("url"))):
                attempt += 1
                if attempt < max_attempts:
                    logger.warning("Extraction failed or blocked. Rotating to next cookie account...")
                else:
                    logger.error("All cookie accounts failed extraction.")
            else:
                break


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
                    sc_info = await do_extract(sc_query, opts)
                    tracks = extract_tracks(sc_info)
                except Exception as e:
                    logger.error(f"SoundCloud fallback error: {e}")

        # ── AUTO FALLBACK TO PIPED API (YouTube Alternative Proxy) ──
        # If both yt-dlp extraction AND SoundCloud fallback failed,
        # try to resolve via a public Piped API instance. This does not require cookies/local IP.
        if not tracks:
            logger.warning("YouTube & SoundCloud extraction failed. Attempting Piped API fallback...")
            try:
                import aiohttp
                # Get video ID from the query
                video_id = None
                if _YT_REGEX.match(query):
                    # Extract video ID from URL
                    import urllib.parse as urlparse
                    parsed = urlparse.urlparse(query)
                    if parsed.hostname == 'youtu.be':
                        video_id = parsed.path[1:]
                    elif parsed.hostname in ('www.youtube.com', 'youtube.com'):
                        if parsed.path == '/watch':
                            video_id = urlparse.parse_qs(parsed.query).get('v', [None])[0]
                        elif parsed.path.startswith('/embed/'):
                            video_id = parsed.path.split('/')[2]
                        elif parsed.path.startswith('/v/'):
                            video_id = parsed.path.split('/')[2]
                
                # If it was a search query, first use the video resolved by Google API or VideosSearch
                if not video_id and _YT_REGEX.match(search_query):
                    import urllib.parse as urlparse
                    parsed = urlparse.urlparse(search_query)
                    if parsed.hostname == 'youtu.be':
                        video_id = parsed.path[1:]
                    else:
                        video_id = urlparse.parse_qs(parsed.query).get('v', [None])[0]

                if video_id:
                    # Query Piped APIs (we try a couple of popular public instances)
                    piped_instances = [
                        "https://pipedapi.kavin.rocks",
                        "https://api.piped.yt",
                        "https://piped-api.garudalinux.org"
                    ]
                    for instance in piped_instances:
                        try:
                            api_url = f"{instance}/streams/{video_id}"
                            async with aiohttp.ClientSession() as session:
                                async with session.get(api_url, timeout=6) as resp:
                                    if resp.status == 200:
                                        data = await resp.json()
                                        audio_streams = [
                                            s for s in data.get("audioStreams", [])
                                            if s.get("mimeType", "").startswith("audio/")
                                        ]
                                        if audio_streams:
                                            # Pick the highest quality stream
                                            best_stream = max(audio_streams, key=lambda s: s.get("bitrate", 0))
                                            tracks.append(Track(
                                                title=data.get("title", song_title_fallback or "Unknown"),
                                                url=best_stream["url"],
                                                webpage=query,
                                                duration=int(data.get("duration", 0)),
                                                thumbnail=data.get("thumbnailUrl", ""),
                                                requester_id=req_id,
                                                requester_name=req_name,
                                                chat_id=chat_id,
                                                source="youtube",
                                            ))
                                            logger.info(f"Successfully resolved video {video_id} via Piped API ({instance})")
                                            break
                        except Exception as e:
                            logger.error(f"Piped instance {instance} failed: {e}")
                        if tracks:
                            break
            except Exception as e:
                logger.error(f"Piped API fallback failed: {e}")
            
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
