"""
handlers/history.py
/history command — shows recently played tracks in this chat.
"""

from pyrogram import Client, filters
from pyrogram.types import Message
from config.settings import cfg
from utils.decorators import rate_limit


def register(app):
    bot = app.bot
    db  = app.db

    @bot.on_message(filters.command(["history", "recent"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def history_cmd(client: Client, msg: Message):
        records = await db.recent_history(msg.chat.id, limit=10)
        if not records:
            await msg.reply(
                "📭 No play history yet.\nUse `/play` to start listening!",
                quote=True
            )
            return

        lines = ["🕘 **Recently Played**\n"]
        for i, rec in enumerate(records, 1):
            track   = rec.get("track", {})
            title   = track.get("title", "Unknown")
            played  = rec.get("played_at", "")
            # Format datetime if available
            if played:
                try:
                    from datetime import datetime, timezone
                    if isinstance(played, str):
                        dt = datetime.fromisoformat(played.replace("Z", "+00:00"))
                    else:
                        dt = played
                    time_str = dt.strftime("%d %b, %H:%M")
                except Exception:
                    time_str = str(played)[:16]
            else:
                time_str = "—"
            lines.append(f"`{i:>2}.` **{title}** · _{time_str}_")

        await msg.reply("\n".join(lines), quote=True)
