"""
handlers/music.py
All music-related command handlers.
Each command validates permissions, resolves the track, and delegates to StreamEngine.
"""

import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from streaming.engine import LoopMode, Track
from services.resolver import resolver
from utils.decorators import rate_limit, admin_only, voice_chat_required
from utils.ui import build_now_playing, build_queue_text
from utils.helpers import delete_after
from config.settings import cfg

logger = logging.getLogger("handlers.music")


def register(app):
    """Auto-called by GhostMusicBot._load_handlers()"""

    bot  = app.bot
    eng  = app.stream
    db   = app.db

    # ─── /play ────────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["play", "p"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def play_cmd(client: Client, msg: Message):
        query = " ".join(msg.command[1:]).strip()

        # Also accept a reply to an audio/video
        if not query and msg.reply_to_message:
            rm = msg.reply_to_message
            if rm.audio or rm.voice or rm.video:
                await _play_telegram_file(client, msg, rm, eng, db)
                return

        if not query:
            await msg.reply("🎵 Usage: `/play <song name or YouTube URL>`", quote=True)
            return

        loading = await msg.reply("🔍 Searching…", quote=True)

        user_id   = msg.from_user.id   if msg.from_user else 0
        user_name = msg.from_user.first_name if msg.from_user else "Unknown"

        try:
            tracks = await resolver.resolve(
                query,
                requester_id=user_id,
                requester_name=user_name,
                chat_id=msg.chat.id,
            )
        except ValueError as e:
            await loading.edit(f"❌ {e}")
            return
        except Exception as e:
            logger.exception(e)
            await loading.edit("❌ An unexpected error occurred. Please try again.")
            return

        st = eng.state(msg.chat.id)

        if st.is_playing:
            # Queue all resolved tracks
            positions = []
            for t in tracks:
                try:
                    pos = await eng.enqueue(msg.chat.id, t)
                    positions.append(pos)
                    await db.increment_user_songs(user_id)
                except OverflowError as e:
                    await loading.edit(str(e))
                    return
            if len(tracks) == 1:
                t = tracks[0]
                text = (
                    f"📥 **Added to queue** (position #{positions[0]})\n\n"
                    f"🎵 **{t.title}**\n"
                    f"⏱ Duration: {t.duration_str}\n"
                    f"👤 Requested by: {t.requester_name}"
                )
            else:
                text = f"📥 Added **{len(tracks)} tracks** to the queue."
            await loading.edit(text)
        else:
            # Start playing immediately with the first track
            first = tracks[0]
            for t in tracks[1:]:
                await eng.enqueue(msg.chat.id, t)

            st.current   = first
            st.is_playing = True
            st.is_paused  = False
            await db.increment_user_songs(user_id)
            await db.log_play(msg.chat.id, {"title": first.title, "url": first.webpage})
            await db.increment_stat(msg.chat.id, "tracks_queued")

            try:
                from pytgcalls.types import AudioPiped, AudioParameters
                stream = AudioPiped(first.url)
                active = eng.calls.active_calls
                if msg.chat.id in [c.chat_id for c in active]:
                    await eng.calls.change_stream(msg.chat.id, stream)
                else:
                    await eng.calls.join_group_call(msg.chat.id, stream)
                await eng.calls.change_volume_call(msg.chat.id, st.volume)
            except Exception as e:
                logger.error(f"Play error: {e}")
                await loading.edit(f"❌ Could not join voice chat. Make sure the assistant account is a member and the bot is admin with voice chat permissions.")
                st.current = None; st.is_playing = False
                return

            np_text, buttons = build_now_playing(first, st)
            np_msg = await loading.edit(np_text, reply_markup=buttons, disable_web_page_preview=True)
            st.now_playing_msg = np_msg.id

            if len(tracks) > 1:
                await msg.reply(f"📥 Added **{len(tracks)-1}** more tracks to the queue.")

    # ─── /skip ────────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["skip", "s"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def skip_cmd(client, msg):
        next_track = await eng.skip(msg.chat.id)
        if next_track:
            await msg.reply(f"⏭ Skipped! Now playing: **{next_track.title}**")
        else:
            await msg.reply("⏭ Skipped. Queue is now empty.")

    # ─── /pause / /resume ─────────────────────────────────────────────────────
    @bot.on_message(filters.command(["pause"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def pause_cmd(client, msg):
        ok = await eng.pause(msg.chat.id)
        await msg.reply("⏸ Paused." if ok else "Nothing is playing right now.")

    @bot.on_message(filters.command(["resume", "r"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def resume_cmd(client, msg):
        ok = await eng.resume(msg.chat.id)
        await msg.reply("▶️ Resumed!" if ok else "Nothing is paused.")

    # ─── /stop ────────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["stop", "end"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def stop_cmd(client, msg):
        await eng.stop(msg.chat.id)
        await msg.reply("⏹ Stopped and left the voice chat.")

    # ─── /queue ───────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["queue", "q"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def queue_cmd(client, msg):
        st = eng.state(msg.chat.id)
        if not st.current and not st.queue:
            await msg.reply("Queue is empty. Use /play to add songs!")
            return
        await msg.reply(build_queue_text(st), disable_web_page_preview=True)

    # ─── /nowplaying ──────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["np", "nowplaying", "current"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def np_cmd(client, msg):
        st = eng.state(msg.chat.id)
        if not st.current:
            await msg.reply("Nothing is playing right now.")
            return
        text, buttons = build_now_playing(st.current, st)
        await msg.reply(text, reply_markup=buttons, disable_web_page_preview=True)

    # ─── /volume ──────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["volume", "vol", "v"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def volume_cmd(client, msg):
        if len(msg.command) < 2:
            st = eng.state(msg.chat.id)
            await msg.reply(f"🔊 Current volume: **{st.volume}%**\nUsage: `/volume 1–200`")
            return
        try:
            vol = int(msg.command[1])
        except ValueError:
            await msg.reply("❌ Volume must be a number between 1 and 200.")
            return
        await eng.set_volume(msg.chat.id, vol)
        await msg.reply(f"🔊 Volume set to **{max(1, min(200, vol))}%**")

    # ─── /loop ────────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["loop", "repeat"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def loop_cmd(client, msg):
        mode_arg = (msg.command[1].lower() if len(msg.command) > 1 else "")
        cycle = {LoopMode.OFF: LoopMode.SONG, LoopMode.SONG: LoopMode.QUEUE, LoopMode.QUEUE: LoopMode.OFF}
        mode_map = {"off": LoopMode.OFF, "song": LoopMode.SONG, "queue": LoopMode.QUEUE}

        st = eng.state(msg.chat.id)
        new_mode = mode_map.get(mode_arg, cycle.get(st.loop, LoopMode.OFF))
        await eng.set_loop(msg.chat.id, new_mode)

        emoji = {"off": "🔁", "song": "🔂", "queue": "🔁"}
        label = {LoopMode.OFF: "Off", LoopMode.SONG: "Song", LoopMode.QUEUE: "Queue"}
        await msg.reply(f"{emoji.get(new_mode.value, '🔁')} Loop mode: **{label[new_mode]}**")

    # ─── /shuffle ─────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["shuffle"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def shuffle_cmd(client, msg):
        count = await eng.shuffle_queue(msg.chat.id)
        if count == 0:
            await msg.reply("Queue is empty, nothing to shuffle.")
        else:
            await msg.reply(f"🔀 Queue shuffled! ({count} tracks)")

    # ─── /clearqueue ──────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["clearqueue", "cq", "clear"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def clearq_cmd(client, msg):
        st = eng.state(msg.chat.id)
        n  = len(st.queue)
        st.queue.clear()
        await msg.reply(f"🗑 Cleared {n} track(s) from the queue.")

    # ─── /lyrics ──────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["lyrics", "ly"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def lyrics_cmd(client, msg):
        if not cfg.GENIUS_TOKEN:
            await msg.reply("Lyrics service is not configured. Set GENIUS_TOKEN in .env")
            return
        st    = eng.state(msg.chat.id)
        query = " ".join(msg.command[1:]).strip() or (st.current.title if st.current else "")
        if not query:
            await msg.reply("Usage: `/lyrics <song name>`")
            return
        loading = await msg.reply("🔍 Fetching lyrics…")
        try:
            from services.lyrics import get_lyrics
            text = await get_lyrics(query)
            await loading.edit(text[:4096])
        except Exception as e:
            await loading.edit(f"❌ Could not fetch lyrics: {e}")

    # ─── /playlist ────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["playlist", "pl"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def playlist_cmd(client, msg):
        sub = msg.command[1].lower() if len(msg.command) > 1 else "list"

        if sub == "create" and len(msg.command) > 2:
            name = msg.command[2]
            ok = await db.create_playlist(msg.from_user.id, name)
            await msg.reply(f"✅ Playlist **{name}** created!" if ok else f"❌ Playlist **{name}** already exists.")

        elif sub == "add" and len(msg.command) > 2:
            name = msg.command[2]
            st   = eng.state(msg.chat.id)
            if not st.current:
                await msg.reply("Nothing is playing right now.")
                return
            track_data = {"title": st.current.title, "url": st.current.webpage}
            ok = await db.add_to_playlist(msg.from_user.id, name, track_data)
            await msg.reply(f"✅ Added **{st.current.title}** to playlist **{name}**." if ok else f"❌ Playlist **{name}** not found.")

        elif sub == "play" and len(msg.command) > 2:
            name = msg.command[2]
            pl   = await db.get_playlist(msg.from_user.id, name)
            if not pl:
                await msg.reply(f"❌ Playlist **{name}** not found.")
                return
            tracks = pl.get("tracks", [])
            if not tracks:
                await msg.reply(f"Playlist **{name}** is empty.")
                return
            added = 0
            for t in tracks[:cfg.MAX_PLAYLIST_SIZE]:
                try:
                    await eng.enqueue(msg.chat.id, Track(
                        title=t["title"], url=t["url"], webpage=t["url"],
                        duration=0, thumbnail="",
                        requester_id=msg.from_user.id,
                        requester_name=msg.from_user.first_name,
                        chat_id=msg.chat.id, source="playlist",
                    ))
                    added += 1
                except OverflowError:
                    break
            await msg.reply(f"▶️ Added **{added}** tracks from playlist **{name}** to the queue.")
            st = eng.state(msg.chat.id)
            if not st.is_playing:
                await eng.play_next(msg.chat.id)

        elif sub == "delete" and len(msg.command) > 2:
            name = msg.command[2]
            ok = await db.delete_playlist(msg.from_user.id, name)
            await msg.reply(f"✅ Deleted playlist **{name}**." if ok else f"❌ Playlist **{name}** not found.")

        else:  # list
            pls = await db.list_playlists(msg.from_user.id)
            if not pls:
                await msg.reply("You have no saved playlists. Create one with `/playlist create <name>`")
                return
            lines = "\n".join(f"• **{p['name']}** ({len(p.get('tracks',[]))} tracks)" for p in pls)
            await msg.reply(f"🎵 **Your Playlists:**\n\n{lines}")

    # ─── /help ────────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["help", "start"], prefixes=cfg.COMMAND_PREFIX))
    async def help_cmd(client, msg):
        await msg.reply(HELP_TEXT, disable_web_page_preview=True)

    # ─── Inline buttons from now-playing card ─────────────────────────────────
    @bot.on_callback_query(filters.regex(r"^music:"))
    @rate_limit
    async def music_callback(client, cb: CallbackQuery):
        action = cb.data.split(":")[1]
        chat_id = cb.message.chat.id
        st = eng.state(chat_id)

        if action == "pause":
            ok = await eng.pause(chat_id)
            await cb.answer("⏸ Paused" if ok else "Already paused")
        elif action == "resume":
            ok = await eng.resume(chat_id)
            await cb.answer("▶️ Resumed" if ok else "Not paused")
        elif action == "skip":
            await eng.skip(chat_id)
            await cb.answer("⏭ Skipped")
        elif action == "stop":
            await eng.stop(chat_id)
            await cb.answer("⏹ Stopped")
        elif action == "loop":
            cycle = {LoopMode.OFF: LoopMode.SONG, LoopMode.SONG: LoopMode.QUEUE, LoopMode.QUEUE: LoopMode.OFF}
            await eng.set_loop(chat_id, cycle[st.loop])
            await cb.answer(f"Loop: {st.loop.value}")
        elif action == "shuffle":
            count = await eng.shuffle_queue(chat_id)
            await cb.answer(f"🔀 Shuffled {count} tracks")
        elif action == "queue":
            await cb.answer(build_queue_text(st)[:200], show_alert=True)

        # Refresh now-playing card
        if st.current:
            text, buttons = build_now_playing(st.current, st)
            try:
                await cb.message.edit_text(text, reply_markup=buttons, disable_web_page_preview=True)
            except Exception:
                pass

    # ─── Telegram file playback ───────────────────────────────────────────────
    async def _play_telegram_file(client, msg, rm, eng, db):
        media = rm.audio or rm.voice or rm.video
        title = getattr(media, "title", None) or getattr(media, "file_name", "Telegram File")
        duration = getattr(media, "duration", 0) or 0

        track = Track(
            title=title, url=rm.link, webpage=rm.link,
            duration=duration, thumbnail="",
            requester_id=msg.from_user.id,
            requester_name=msg.from_user.first_name,
            chat_id=msg.chat.id, source="file",
            file_id=media.file_id,
        )
        st = eng.state(msg.chat.id)
        if st.is_playing:
            pos = await eng.enqueue(msg.chat.id, track)
            await msg.reply(f"📥 Added **{title}** to queue at position #{pos}")
        else:
            st.current = track; st.is_playing = True
            await msg.reply(f"▶️ Playing: **{title}**")


HELP_TEXT = """
🎵 **GhostMusic Bot Commands**

**Playback**
`/play <song/URL>` — Play or queue a song
`/skip` — Skip current song
`/pause` / `/resume` — Pause / resume
`/stop` — Stop and leave voice chat
`/nowplaying` — Show current track

**Queue**
`/queue` — View queue
`/shuffle` — Shuffle queue
`/clearqueue` — Clear the queue

**Controls**
`/volume <1-200>` — Set volume
`/loop [off|song|queue]` — Loop mode

**Playlists**
`/playlist create <name>` — Create playlist
`/playlist add <name>` — Add current song
`/playlist play <name>` — Play playlist
`/playlist delete <name>` — Delete playlist
`/playlist` — List your playlists

**Extras**
`/lyrics [song]` — Get song lyrics
`/history` — Recent plays

_Supports YouTube & Spotify links, and Telegram audio files._
"""
