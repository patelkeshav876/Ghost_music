"""
handlers/admin.py — Admin-only commands
"""

from pyrogram import Client, filters
from pyrogram.types import Message
from config.settings import cfg
from utils.decorators import sudo_only


def register(app):
    bot = app.bot
    eng = app.stream
    db  = app.db

    @bot.on_message(filters.command(["broadcast"], prefixes=cfg.COMMAND_PREFIX))
    @sudo_only
    async def broadcast_cmd(client: Client, msg: Message):
        text = " ".join(msg.command[1:]).strip()
        if not text:
            await msg.reply("Usage: `/broadcast <message>`")
            return
        chat_ids = await db.all_chat_ids()
        sent = failed = 0
        for cid in chat_ids:
            try:
                await bot.send_message(cid, text)
                sent += 1
            except Exception:
                failed += 1
        await msg.reply(f"📢 Broadcast done.\n✅ Sent: {sent}\n❌ Failed: {failed}")

    @bot.on_message(filters.command(["stats"], prefixes=cfg.COMMAND_PREFIX))
    @sudo_only
    async def global_stats_cmd(client, msg):
        stats = await db.global_stats()
        total_users = await db.total_users()
        active = len([s for s in eng._states.values() if s.is_playing])
        await msg.reply(
            f"📊 **Global Stats**\n\n"
            f"👥 Total users: {total_users}\n"
            f"💬 Active streams: {active}\n"
            f"🎵 Total plays: {stats.get('total_plays', 0)}\n"
            f"📡 Chats registered: {stats.get('total_chats', 0)}"
        )

    @bot.on_message(filters.command(["adminsonly"], prefixes=cfg.COMMAND_PREFIX))
    async def admins_only_cmd(client, msg):
        # Toggle admins-only mode for this chat
        chat = await db.get_chat(msg.chat.id)
        new_val = not chat.get("admins_only", False)
        await db.update_chat(msg.chat.id, {"admins_only": new_val})
        await msg.reply(f"🔒 Admins-only mode: **{'ON' if new_val else 'OFF'}**")
