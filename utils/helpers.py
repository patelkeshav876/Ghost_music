"""
utils/helpers.py
Shared utility functions used across the bot.
"""

import asyncio
import logging
import os
import re
from typing import Optional

logger = logging.getLogger("utils.helpers")


def format_duration(seconds: int) -> str:
    """Convert seconds to MM:SS or HH:MM:SS string."""
    if not seconds or seconds <= 0:
        return "Live"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_size(bytes_: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_ < 1024:
            return f"{bytes_:.1f} {unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f} TB"


def sanitize_text(text: str, max_len: int = 200) -> str:
    """Strip markdown special chars and truncate."""
    clean = re.sub(r"[*_`\[\]()~>#+\-=|{}.!]", "", text)
    return clean[:max_len].strip()


async def delete_after(msg, delay: int = 30):
    """
    Delete a Pyrogram message after `delay` seconds.
    Silently ignores errors (message already deleted, no permission, etc.).
    """
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


def is_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")


def is_youtube_url(text: str) -> bool:
    return bool(re.match(
        r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+", text
    ))


def is_spotify_url(text: str) -> bool:
    return "open.spotify.com" in text


def chunk_list(lst: list, size: int) -> list:
    """Split a list into chunks of given size."""
    return [lst[i:i+size] for i in range(0, len(lst), size)]


def humane_number(n: int) -> str:
    """1500 → '1.5K', 1200000 → '1.2M'"""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)
