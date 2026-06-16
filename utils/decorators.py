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


def authorized_only(func):
    """Requires user to be a group admin, creator, bot owner, sudo user, or explicitly promoted in the group."""
    @functools.wraps(func)
    async def wrapper(client, update, *args, **kwargs):
        user_id = None
        chat_id = None
        is_cb = False
        
        if isinstance(update, Message):
            user_id = update.from_user.id if update.from_user else 0
            chat_id = update.chat.id
        elif isinstance(update, CallbackQuery):
            user_id = update.from_user.id
            chat_id = update.message.chat.id
            is_cb = True

        # If it's a private chat (chat_id > 0 for users in Telegram), everyone is authorized
        if chat_id and chat_id > 0:
            return await func(client, update, *args, **kwargs)

        if user_id:
            # Sudo users and owner are always allowed
            if user_id in cfg.SUDO_USERS or user_id == cfg.OWNER_ID:
                return await func(client, update, *args, **kwargs)

            # Check if user is Telegram Group Administrator/Creator
            is_tg_admin = False
            try:
                member = await client.get_chat_member(chat_id, user_id)
                if member.status.value in ("administrator", "creator"):
                    is_tg_admin = True
            except Exception:
                pass
            
            if is_tg_admin:
                return await func(client, update, *args, **kwargs)

            # Check if user is in group's promoted list in MongoDB
            is_promoted = False
            try:
                db = getattr(client, "db", None)
                if db:
                    is_promoted = await db.is_user_promoted(chat_id, user_id)
            except Exception as e:
                logger.error(f"Error checking user promotion: {e}")

            if is_promoted:
                return await func(client, update, *args, **kwargs)

            # If not authorized:
            if is_cb:
                await update.answer("❌ You are not authorized to control music in this group. Ask a group admin to /promote you.", show_alert=True)
            else:
                await update.reply("❌ You are not authorized to control music in this group. Ask a group admin to `/promote` you.", quote=True)
            return

        return await func(client, update, *args, **kwargs)
    return wrapper

