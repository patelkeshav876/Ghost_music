"""
services/lyrics.py
Fetches song lyrics via the Genius API.
Falls back to a simple scrape if the API token is set but HTML is returned.
"""

import asyncio
import logging
import re
from typing import Optional

import aiohttp
from config.settings import cfg

logger = logging.getLogger("services.lyrics")

_GENIUS_SEARCH = "https://api.genius.com/search"
_HEADERS = {"Authorization": f"Bearer {cfg.GENIUS_TOKEN}"}


async def get_lyrics(query: str) -> str:
    """
    Returns formatted lyrics string for a given song query.
    Raises ValueError with user-friendly message on failure.
    """
    if not cfg.GENIUS_TOKEN:
        raise ValueError("Genius token not configured.")

    async with aiohttp.ClientSession() as session:
        # Step 1: Search for song
        async with session.get(
            _GENIUS_SEARCH,
            params={"q": query},
            headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise ValueError("Genius API request failed.")
            data = await resp.json()

        hits = data.get("response", {}).get("hits", [])
        if not hits:
            raise ValueError(f"No lyrics found for: **{query}**")

        hit      = hits[0]["result"]
        title    = hit.get("full_title", query)
        song_url = hit.get("url", "")

        if not song_url:
            raise ValueError("Could not retrieve lyrics URL.")

        # Step 2: Scrape lyrics from Genius page
        lyrics = await _scrape_lyrics(session, song_url)
        if not lyrics:
            return f"🎵 **{title}**\n\n_Lyrics not available for this song._"

        # Truncate for Telegram 4096 char limit
        header = f"🎵 **{title}**\n\n"
        max_body = 4096 - len(header) - 50
        if len(lyrics) > max_body:
            lyrics = lyrics[:max_body] + "\n\n_…lyrics truncated_"

        return header + lyrics


async def _scrape_lyrics(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    """
    Lightly scrape the Genius page for lyrics text.
    Genius renders lyrics inside data-lyrics-container divs.
    """
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "Mozilla/5.0"},
        ) as resp:
            html = await resp.text()
    except Exception as e:
        logger.warning(f"Lyrics scrape failed: {e}")
        return None

    # Extract text from data-lyrics-container sections
    containers = re.findall(
        r'data-lyrics-container="true"[^>]*>(.*?)</div>',
        html, re.DOTALL
    )
    if not containers:
        return None

    # Strip HTML tags, decode entities
    raw = "\n".join(containers)
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = raw.replace("&amp;", "&").replace("&quot;", '"').replace("&#x27;", "'")
    return raw.strip()
