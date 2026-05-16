"""
utils/thumbnail.py
Generates a now-playing card image using Pillow.
Falls back gracefully if Pillow isn't installed or the thumbnail URL fails.
"""

import asyncio
import io
import logging
import os
from typing import Optional

logger = logging.getLogger("utils.thumbnail")

_FONT_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "font.ttf")


async def generate_now_playing_card(
    title:      str,
    artist:     str = "",
    duration:   str = "",
    requester:  str = "",
    thumb_url:  str = "",
    volume:     int = 100,
) -> Optional[bytes]:
    """
    Returns PNG bytes of a now-playing card, or None if generation fails.
    Runs in a thread pool so it never blocks the event loop.
    """
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(
            None,
            _draw_card,
            title, artist, duration, requester, thumb_url, volume
        )
    except Exception as e:
        logger.warning(f"Thumbnail generation failed: {e}")
        return None


def _draw_card(
    title: str,
    artist: str,
    duration: str,
    requester: str,
    thumb_url: str,
    volume: int,
) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    import urllib.request

    # ── Canvas ──────────────────────────────────────────────────────────────
    W, H = 800, 240
    img  = Image.new("RGB", (W, H), color=(13, 17, 35))
    draw = ImageDraw.Draw(img)

    # ── Background gradient (manual horizontal bands) ────────────────────────
    for x in range(W):
        ratio = x / W
        r = int(27  + (59  - 27)  * ratio)
        g = int(79  + (123 - 79)  * ratio)
        b = int(219 + (255 - 219) * ratio)
        draw.line([(x, 0), (x, H)], fill=(r, g, b, 30))

    # ── Thumbnail image ───────────────────────────────────────────────────────
    thumb_x = 20
    if thumb_url:
        try:
            with urllib.request.urlopen(thumb_url, timeout=4) as resp:
                raw = resp.read()
            thumb = Image.open(io.BytesIO(raw)).convert("RGB")
            thumb = thumb.resize((200, 200), Image.LANCZOS)
            # Rounded mask
            mask = Image.new("L", (200, 200), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle([0, 0, 200, 200], radius=16, fill=255)
            img.paste(thumb, (thumb_x, 20), mask)
        except Exception:
            _draw_placeholder(draw, thumb_x, 20, 200, 200)
    else:
        _draw_placeholder(draw, thumb_x, 20, 200, 200)

    # ── Text area ─────────────────────────────────────────────────────────────
    tx = 240

    # Fonts — fallback to default if custom font not found
    try:
        f_title  = ImageFont.truetype(_FONT_PATH, 28)
        f_sub    = ImageFont.truetype(_FONT_PATH, 18)
        f_small  = ImageFont.truetype(_FONT_PATH, 14)
    except Exception:
        f_title = f_sub = f_small = ImageFont.load_default()

    # Title
    title_short = title[:42] + "…" if len(title) > 42 else title
    draw.text((tx, 30),  title_short, font=f_title, fill=(240, 244, 255))

    # Artist / sub
    if artist:
        draw.text((tx, 70), artist[:50], font=f_sub, fill=(150, 170, 220))

    # Duration
    if duration:
        draw.text((tx, 100), f"⏱  {duration}", font=f_small, fill=(120, 140, 190))

    # Requester
    if requester:
        draw.text((tx, 125), f"👤  {requester}", font=f_small, fill=(120, 140, 190))

    # Volume bar
    bar_y = 170
    draw.rounded_rectangle([tx, bar_y, tx + 400, bar_y + 8], radius=4, fill=(40, 60, 100))
    vol_w = int(400 * min(volume, 200) / 200)
    if vol_w > 0:
        draw.rounded_rectangle([tx, bar_y, tx + vol_w, bar_y + 8], radius=4, fill=(59, 123, 255))

    draw.text((tx, bar_y + 14), f"🔊  {volume}%", font=f_small, fill=(120, 140, 190))

    # ── Watermark ─────────────────────────────────────────────────────────────
    draw.text((W - 150, H - 24), "🎵 GhostMusic", font=f_small, fill=(60, 80, 130))

    # ── Subtle top accent line ────────────────────────────────────────────────
    draw.rectangle([0, 0, W, 3], fill=(59, 123, 255))

    # ── Export ────────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _draw_placeholder(draw, x: int, y: int, w: int, h: int):
    """Draw a ghost icon placeholder when no thumbnail is available."""
    from PIL import ImageDraw
    draw.rounded_rectangle([x, y, x+w, y+h], radius=16, fill=(27, 40, 80))
    # Simple ghost shape using ellipses
    gx, gy = x + w//2, y + h//2 - 10
    draw.ellipse([gx-30, gy-35, gx+30, gy+25], fill=(200, 210, 240))
    draw.rectangle([gx-30, gy+5, gx+30, gy+35], fill=(200, 210, 240))
    # Eyes
    draw.ellipse([gx-15, gy-10, gx-5,  gy],    fill=(27, 40, 80))
    draw.ellipse([gx+5,  gy-10, gx+15, gy],    fill=(27, 40, 80))
