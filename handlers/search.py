"""
handlers/search.py
/search command — shows 5 YouTube results as inline buttons.
User taps a result → bot plays it.
"""

from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from services.resolver import resolver
from utils.decorators import rate_limit
from utils.helpers import format_duration
from config.settings import cfg


def register(app):
    bot = app.bot
    eng = app.stream
    db  = app.db

    @bot.on_message(filters.command(["search", "find", "yt"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def search_cmd(client: Client, msg: Message):
        query = " ".join(msg.command[1:]).strip()
        if not query:
            await msg.reply("🔍 Usage: `/search <song name>`", quote=True)
            return

        loading = await msg.reply("🔍 Searching YouTube…", quote=True)

        try:
            results = await resolver.resolve_for_search_inline(query)
        except Exception:
            await loading.edit("❌ Search failed. Please try again.")
            return

        if not results:
            await loading.edit(f"😕 No results found for: **{query}**")
            return

        lines   = ["🎵 **Search Results** — tap to play:\n"]
        buttons = []
        for i, r in enumerate(results[:5], 1):
            dur   = format_duration(r.get("duration", 0))
            title = r.get("title", "Unknown")[:50]
            url   = r.get("url", "")
            lines.append(f"`{i}.` {title} `[{dur}]`")
            buttons.append([
                InlineKeyboardButton(
                    f"{'▶️' if i==1 else f'{i}.'} {title[:35]}",
                    callback_data=f"sr:{i-1}:{msg.id}"
                )
            ])
        buttons.append([
            InlineKeyboardButton("❌ Cancel", callback_data="sr:cancel")
        ])

        # Store results in memory keyed by original message id
        _search_cache[msg.id] = results

        await loading.edit(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    # Cache search results per message_id
    _search_cache: dict[int, list] = {}

    @bot.on_callback_query(filters.regex(r"^sr:"))
    @rate_limit
    async def search_result_cb(client: Client, cb: CallbackQuery):
        parts = cb.data.split(":")
        if parts[1] == "cancel":
            await cb.message.delete()
            await cb.answer("Cancelled")
            return

        idx    = int(parts[1])
        msg_id = int(parts[2]) if len(parts) > 2 else 0

        results = _search_cache.get(msg_id, [])
        if not results or idx >= len(results):
            await cb.answer("Result expired. Search again.", show_alert=True)
            return

        item = results[idx]
        url  = item.get("url", "")
        if not url:
            await cb.answer("Could not get track URL.", show_alert=True)
            return

        await cb.answer(f"▶️ Playing: {item.get('title','Unknown')[:40]}")
        await cb.message.edit_text(
            f"▶️ Playing: **{item.get('title','Unknown')}**"
        )

        # Trigger play by editing message and simulating /play
        from handlers.music import play_cmd  # noqa — circular avoided via app context
        # Re-use resolver directly
        try:
            tracks = await resolver.resolve(
                url,
                requester_id=cb.from_user.id,
                requester_name=cb.from_user.first_name,
                chat_id=cb.message.chat.id,
            )
        except Exception as e:
            await cb.message.edit_text(f"❌ Could not play: {e}")
            return

        st = eng.state(cb.message.chat.id)
        if tracks:
            track = tracks[0]
            if st.is_playing:
                pos = await eng.enqueue(cb.message.chat.id, track)
                await cb.message.edit_text(
                    f"📥 **Added to queue** (#{pos})\n🎵 {track.title}"
                )
            else:
                st.current = track
                st.is_playing = True
                try:
                    from pytgcalls.types import MediaStream
                    stream = MediaStream(track.url)
                    await eng.calls.play(cb.message.chat.id, stream)
                    try:
                        await eng.calls.change_volume_call(cb.message.chat.id, st.volume)
                    except:
                        pass
                except Exception as e:
                    st.current = None; st.is_playing = False
                    await cb.message.edit_text(f"❌ Could not join voice chat: {e}")
                    return
                from utils.ui import build_now_playing
                text, buttons_np = build_now_playing(track, st)
                np_msg = await cb.message.edit_text(text, reply_markup=buttons_np)
                st.now_playing_msg = np_msg.id

        _search_cache.pop(msg_id, None)
