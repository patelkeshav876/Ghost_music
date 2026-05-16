"""
handlers/inline.py
Inline query handler — lets users search songs from any chat
by typing @BotUsername <song name>.
"""

import logging
from pyrogram import Client, filters
from pyrogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from services.resolver import resolver
from utils.helpers import format_duration

logger = logging.getLogger("handlers.inline")


def register(app):
    bot = app.bot

    @bot.on_inline_query()
    async def inline_search(client: Client, query: InlineQuery):
        text = query.query.strip()

        if not text or len(text) < 2:
            await query.answer(
                results=[],
                cache_time=5,
                switch_pm_text="Type a song name to search 🎵",
                switch_pm_parameter="inline_help",
            )
            return

        try:
            results_raw = await resolver.resolve_for_search_inline(text)
        except Exception as e:
            logger.error(f"Inline search error: {e}")
            results_raw = []

        if not results_raw:
            await query.answer(
                results=[],
                cache_time=10,
                switch_pm_text="No results found. Try a different query.",
                switch_pm_parameter="inline_help",
            )
            return

        articles = []
        for item in results_raw[:8]:
            dur   = format_duration(item.get("duration", 0))
            title = item.get("title", "Unknown")
            url   = item.get("url", "")
            vid_id = item.get("id", "")
            thumb = f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg" if vid_id else ""

            articles.append(
                InlineQueryResultArticle(
                    title=title,
                    description=f"⏱ {dur} · YouTube",
                    thumb_url=thumb,
                    input_message_content=InputTextMessageContent(
                        f"/play {url}"
                    ),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("▶️ Play in Group", switch_inline_query_current_chat=f"")
                    ]]),
                    id=vid_id or title[:16],
                )
            )

        await query.answer(
            results=articles,
            cache_time=30,
            is_personal=True,
        )
