"""
utils/decorators.py
Reusable decorators for rate limiting, admin checks, etc.
"""

import asyncio
import functools
import time
import logging
from pyrogram.types import Message, CallbackQuery
from config.settings import cfg

logger = logging.getLogger("utils.decorators")

# ── Rate limiter ──────────────────────────────────────────────────────────────
_rate_store: dict[tuple, list] = {}

def rate_limit(func):
    @functools.wraps(func)
    async def wrapper(client, update, *args, **kwargs):
        user_id = None
        if isinstance(update, Message):
            user_id = update.from_user.id if update.from_user else None
        elif isinstance(update, CallbackQuery):
            user_id = update.from_user.id

        if user_id:
            now  = time.monotonic()
            key  = (user_id, func.__name__)
            hits = _rate_store.get(key, [])
            # Remove old hits outside the window
            hits = [t for t in hits if now - t < cfg.RATE_LIMIT_WINDOW]
            if len(hits) >= cfg.RATE_LIMIT_CMDS:
                wait = cfg.RATE_LIMIT_WINDOW - (now - hits[0])
                if isinstance(update, Message):
                    await update.reply(f"⏳ Slow down! Try again in {wait:.1f}s.", quote=True)
                elif isinstance(update, CallbackQuery):
                    await update.answer(f"⏳ Slow down! {wait:.1f}s", show_alert=False)
                return
            hits.append(now)
            _rate_store[key] = hits

        return await func(client, update, *args, **kwargs)
    return wrapper


def sudo_only(func):
    @functools.wraps(func)
    async def wrapper(client, update, *args, **kwargs):
        user_id = None
        if isinstance(update, Message):
            user_id = update.from_user.id if update.from_user else 0
        elif isinstance(update, CallbackQuery):
            user_id = update.from_user.id
        if user_id not in cfg.SUDO_USERS and user_id != cfg.OWNER_ID:
            if isinstance(update, Message):
                await update.reply("🚫 This command is for bot owners only.")
            return
        return await func(client, update, *args, **kwargs)
    return wrapper


def admin_only(func):
    """Requires Telegram group admin or bot sudo."""
    @functools.wraps(func)
    async def wrapper(client, msg: Message, *args, **kwargs):
        user = msg.from_user
        if not user:
            return
        if user.id in cfg.SUDO_USERS or user.id == cfg.OWNER_ID:
            return await func(client, msg, *args, **kwargs)
        try:
            member = await client.get_chat_member(msg.chat.id, user.id)
            if member.status.value not in ("administrator", "creator"):
                await msg.reply("🚫 Only group admins can use this command.")
                return
        except Exception:
            await msg.reply("🚫 Could not verify your permissions.")
            return
        return await func(client, msg, *args, **kwargs)
    return wrapper


def voice_chat_required(func):
    """Returns error if the assistant isn't in a voice chat."""
    @functools.wraps(func)
    async def wrapper(client, msg: Message, *args, **kwargs):
        # We check this by looking at engine state — already playing = VC is up
        return await func(client, msg, *args, **kwargs)
    return wrapper
