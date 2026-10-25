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
from utils.decorators import rate_limit, admin_only, voice_chat_required, authorized_only
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
    @authorized_only
    async def play_cmd(client: Client, msg: Message):
        if eng is None:
            await msg.reply("Voice chat unavailable: SESSION_STRING is invalid/expired. Ask the bot owner to regenerate it.")
            return
        query = " ".join(msg.command[1:]).strip()

        # Also accept a reply to an audio/video
        if not query and msg.reply_to_message:
            rm = msg.reply_to_message
            if rm.audio or rm.voice or rm.video:
                await _play_telegram_file(client, msg, rm, eng, db)
                return

        if not query:
            err_msg = await msg.reply("🎵 Usage: `/play <song name or YouTube URL>`", quote=True)
            asyncio.create_task(delete_after(err_msg, 10))
            asyncio.create_task(delete_after(msg, 10))
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
            asyncio.create_task(delete_after(loading, 10))
            asyncio.create_task(delete_after(msg, 10))
            return
        except Exception as e:
            logger.exception(e)
            await loading.edit("❌ An unexpected error occurred. Please try again.")
            asyncio.create_task(delete_after(loading, 10))
            asyncio.create_task(delete_after(msg, 10))
            return

        st = eng.state(msg.chat.id)

        # Check if actually actively streaming in the call right now
        is_active_in_call = False
        if eng.calls:
            try:
                is_active_in_call = msg.chat.id in [c.chat_id for c in eng.calls.active_calls]
            except Exception:
                is_active_in_call = False

        if st.is_playing and is_active_in_call:
            # Queue all resolved tracks
            positions = []
            for t in tracks:
                try:
                    pos = await eng.enqueue(msg.chat.id, t)
                    positions.append(pos)
                    await db.increment_user_songs(user_id)
                except OverflowError as e:
                    await loading.edit(str(e))
                    asyncio.create_task(delete_after(loading, 10))
                    asyncio.create_task(delete_after(msg, 10))
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
            asyncio.create_task(delete_after(loading, 10))
            asyncio.create_task(delete_after(msg, 10))
        else:
            # Not currently streaming in call or player was stuck -> clear queue and play immediately!
            st.queue.clear()
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
                # Delegate entirely to engine._start_stream which handles:
                # 1. VC creation via assistant if no voice chat exists
                # 2. join_group_call or change_stream as appropriate
                await eng._start_stream(msg.chat.id, first)
            except Exception as e:

                logger.exception(f"Play error in {msg.chat.id}: {e}")

                err = str(e)

                asst_display = "Assistant"

                if hasattr(eng, "assistant_user") and eng.assistant_user:

                    if getattr(eng.assistant_user, "username", None):

                        asst_display = f"@{eng.assistant_user.username}"

                    elif getattr(eng.assistant_user, "first_name", None):

                        asst_display = eng.assistant_user.first_name

                if "NO_ACTIVE_GROUP_CALL" in err or "NoActiveGroupCall" in err:

                    hint = (

                        "❌ **No active Voice Chat found!**\n\n"

                        "Please start a Voice Chat in this group first:\n"

                        "1. Tap the group title at the top\n"

                        "2. Tap the **voice chat / video chat** icon → **Start Voice Chat**\n\n"

                        "Then send `/play` again!"

                    )

                elif any(k in err for k in ["GROUPCALL_FORBIDDEN", "CHAT_ADMIN_REQUIRED", "USER_BANNED", "BOT_METHOD_INVALID", "CreateGroupCall"]):

                    hint = (

                        f"❌ **Permission Issue for Assistant ({asst_display})**\n\n"

                        f"Telegram prevented the assistant account from joining the voice chat.\n\n"

                        f"**To Fix:**\n"

                        f"1. Open group settings → **Administrators** → **Add Admin**\n"

                        f"2. Select **{asst_display}** *(Note: this is the user account, NOT @Ghost_music_bot)*\n"

                        f"3. Enable **Manage Video Chats**\n"

                        f"4. Save and retry `/play`"

                    )

                elif "Peer id invalid" in err or "peer" in err.lower():

                    hint = (

                        f"❌ **Assistant ({asst_display}) Not in Group!**\n\n"

                        f"1. Add **{asst_display}** to this group as a member\n"

                        f"2. Promote **{asst_display}** to Admin with **Manage Video Chats**\n"

                        f"3. Retry `/play`"

                    )

                else:

                    hint = f"❌ Could not join voice chat.\n\nError: `{e}`"

                await loading.edit(hint)

                asyncio.create_task(delete_after(loading, 25))

                asyncio.create_task(delete_after(msg, 25))
                st.current = None; st.is_playing = False
                return

            try:
                await loading.delete()
            except Exception:
                pass
            await eng._update_now_playing(msg.chat.id, first)

            if len(tracks) > 1:
                more_msg = await client.send_message(msg.chat.id, f"📥 Added **{len(tracks)-1}** more tracks to the queue.")
                asyncio.create_task(delete_after(more_msg, 10))
            
            try:
                await msg.delete()
            except Exception:
                pass

    # ─── /skip ────────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["skip", "s"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    @authorized_only
    async def skip_cmd(client, msg):
        st = eng.state(msg.chat.id)
        if st.now_playing_msg:
            try:
                await client.delete_messages(msg.chat.id, st.now_playing_msg)
            except Exception:
                pass
            st.now_playing_msg = None
        
        next_track = await eng.skip(msg.chat.id)
        if next_track:
            await eng._update_now_playing(msg.chat.id, next_track)
        else:
            empty_msg = await msg.reply("⏭ Skipped. Queue is now empty.")
            asyncio.create_task(delete_after(empty_msg, 10))
            
        try:
            await msg.delete()
        except Exception:
            pass

    # ─── /pause / /resume ─────────────────────────────────────────────────────
    @bot.on_message(filters.command(["pause"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    @authorized_only
    async def pause_cmd(client, msg):
        ok = await eng.pause(msg.chat.id)
        if ok:
            st = eng.state(msg.chat.id)
            if st.current:
                await eng._update_now_playing(msg.chat.id, st.current)
        else:
            err_msg = await msg.reply("Nothing is playing right now.")
            asyncio.create_task(delete_after(err_msg, 10))
            
        try:
            await msg.delete()
        except Exception:
            pass

    @bot.on_message(filters.command(["resume", "r"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    @authorized_only
    async def resume_cmd(client, msg):
        ok = await eng.resume(msg.chat.id)
        if ok:
            st = eng.state(msg.chat.id)
            if st.current:
                await eng._update_now_playing(msg.chat.id, st.current)
        else:
            err_msg = await msg.reply("Nothing is paused.")
            asyncio.create_task(delete_after(err_msg, 10))
            
        try:
            await msg.delete()
        except Exception:
            pass

    # ─── /stop ────────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["stop", "end"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    @authorized_only
    async def stop_cmd(client, msg):
        st = eng.state(msg.chat.id)
        if st.now_playing_msg:
            try:
                await client.delete_messages(msg.chat.id, st.now_playing_msg)
            except Exception:
                pass
            st.now_playing_msg = None
        await eng.stop(msg.chat.id)
        stop_msg = await msg.reply("⏹ Stopped and left the voice chat.")
        asyncio.create_task(delete_after(stop_msg, 10))
        try:
            await msg.delete()
        except Exception:
            pass

    # ─── /queue ───────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["queue", "q"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def queue_cmd(client, msg):
        st = eng.state(msg.chat.id)
        if not st.current and not st.queue:
            err_msg = await msg.reply("Queue is empty. Use /play to add songs!")
            asyncio.create_task(delete_after(err_msg, 10))
            try:
                await msg.delete()
            except Exception:
                pass
            return
        q_msg = await msg.reply(build_queue_text(st), disable_web_page_preview=True)
        asyncio.create_task(delete_after(q_msg, 15))
        try:
            await msg.delete()
        except Exception:
            pass

    # ─── /nowplaying ──────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["np", "nowplaying", "current"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def np_cmd(client, msg):
        st = eng.state(msg.chat.id)
        if not st.current:
            err_msg = await msg.reply("Nothing is playing right now.")
            asyncio.create_task(delete_after(err_msg, 10))
            try:
                await msg.delete()
            except Exception:
                pass
            return
        # Resend a fresh Now Playing card to float it at the bottom
        await eng._update_now_playing(msg.chat.id, st.current)
        try:
            await msg.delete()
        except Exception:
            pass

    # ─── /status / /vc ────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["status", "vc"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    async def status_cmd(client, msg):
        asst_id = eng.assistant_user.id if hasattr(eng, "assistant_user") and eng.assistant_user else "Unknown"
        asst_name = eng.assistant_user.first_name if hasattr(eng, "assistant_user") and eng.assistant_user else "Unknown"
        asst_user = f"@{eng.assistant_user.username}" if hasattr(eng, "assistant_user") and eng.assistant_user and eng.assistant_user.username else asst_name

        active_chats = [c.chat_id for c in eng.calls.active_calls] if eng.calls else []
        is_in_vc = msg.chat.id in active_chats

        st = eng.state(msg.chat.id)
        curr_song = st.current.title if st.current else "None"
        curr_source = st.current.source if st.current else "None"

        import shutil, subprocess
        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin:
            try:
                out = subprocess.run([ffmpeg_bin, "-version"], capture_output=True, text=True, timeout=2).stdout
                ffmpeg_ver = out.splitlines()[0] if out else "Unknown"
                ffmpeg_info = f"✅ Installed ({ffmpeg_ver[:35]})"
            except Exception:
                ffmpeg_info = f"✅ Installed at `{ffmpeg_bin}`"
        else:
            ffmpeg_info = "❌ NOT FOUND! (Audio streaming requires FFmpeg)"

        asst_in_group = "Unknown"
        asst_is_admin = False
        if hasattr(eng, "assistant_user") and eng.assistant_user:
            try:
                m = await client.get_chat_member(msg.chat.id, eng.assistant_user.id)
                if m.status in ("administrator", "creator"):
                    asst_in_group = "✅ Yes (Admin)"
                    asst_is_admin = True
                elif m.status not in ("left", "kicked"):
                    asst_in_group = "⚠️ Member (Not Admin)"
                else:
                    asst_in_group = "❌ Not in group"
            except Exception:
                asst_in_group = "❌ Not in group"

        tip = ""
        if not asst_is_admin:
            tip = f"\n\n💡 **Tip:** Make {asst_user} Admin with 'Manage Video Chats' permission."

        text = (
            f"🎧 **GhostMusic Voice Status**\n\n"
            f"🤖 **Bot:** @{(await client.get_me()).username}\n"
            f"👤 **Assistant Account:** {asst_name} ({asst_user})\n"
            f"🆔 **Assistant ID:** `{asst_id}`\n"
            f"👥 **Assistant In Group:** {asst_in_group}\n"
            f"🎬 **FFmpeg Engine:** {ffmpeg_info}\n"
            f"📡 **Connected to Voice Chat:** {'✅ Yes' if is_in_vc else '❌ No'}\n"
            f"🎵 **Current Song:** {curr_song} ({curr_source})\n"
            f"🔊 **Volume:** {st.volume}%\n"
            f"⏸ **Is Paused:** {st.is_paused}\n"
            f"📑 **Queue Length:** {len(st.queue)}"
            f"{tip}"
        )
        await msg.reply(text)


    # ─── /testaudio / /beep ───────────────────────────────────────────────────
    @bot.on_message(filters.command(["testaudio", "beep"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    @authorized_only
    async def testaudio_cmd(client, msg):
        import wave, math, struct, tempfile, os
        from streaming.engine import Track
        test_msg = await msg.reply("🔊 Generating 5-second test tone and streaming to Voice Chat...")
        try:
            fd, wav_path = tempfile.mkstemp(suffix="_test.wav", prefix="ghost_")
            os.close(fd)
            with wave.open(wav_path, "w") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(48000)
                samples = []
                for i in range(48000 * 5):
                    val = int(32767 * 0.5 * math.sin(2 * math.pi * 440 * (i / 48000)))
                    samples.append(struct.pack("<hh", val, val))
                wf.writeframes(b"".join(samples))

            test_track = Track(
                title="GhostMusic Audio Test Tone (440Hz)",
                url=wav_path,
                webpage=wav_path,
                duration=5,
                thumbnail="",
                requester_id=msg.from_user.id if msg.from_user else 0,
                requester_name=msg.from_user.first_name if msg.from_user else "User",
                chat_id=msg.chat.id,
                source="test",
            )
            await eng._start_stream(msg.chat.id, test_track)
            await test_msg.edit(
                "🎶 **Test tone is streaming now!**\n\n"
                "• If you hear the 440Hz beep: WebRTC and audio engine are working!\n"
                "• If complete silence: Render is blocking UDP WebRTC audio packets."
            )
        except Exception as e:
            await test_msg.edit(f"❌ Test audio failed:\n`{e}`")

    # ─── /volume ──────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["volume", "vol", "v"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    @authorized_only
    async def volume_cmd(client, msg):
        if len(msg.command) < 2:
            st = eng.state(msg.chat.id)
            vol_msg = await msg.reply(f"🔊 Current volume: **{st.volume}%**\nUsage: `/volume 1–200`")
            asyncio.create_task(delete_after(vol_msg, 10))
            try:
                await msg.delete()
            except Exception:
                pass
            return
        try:
            vol = int(msg.command[1])
        except ValueError:
            err_msg = await msg.reply("❌ Volume must be a number between 1 and 200.")
            asyncio.create_task(delete_after(err_msg, 10))
            try:
                await msg.delete()
            except Exception:
                pass
            return
        await eng.set_volume(msg.chat.id, vol)
        vol_msg = await msg.reply(f"🔊 Volume set to **{max(1, min(200, vol))}%**")
        asyncio.create_task(delete_after(vol_msg, 10))
        st = eng.state(msg.chat.id)
        if st.current:
            await eng._update_now_playing(msg.chat.id, st.current)
        try:
            await msg.delete()
        except Exception:
            pass

    # ─── /loop ────────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["loop", "repeat"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    @authorized_only
    async def loop_cmd(client, msg):
        mode_arg = (msg.command[1].lower() if len(msg.command) > 1 else "")
        cycle = {LoopMode.OFF: LoopMode.SONG, LoopMode.SONG: LoopMode.QUEUE, LoopMode.QUEUE: LoopMode.OFF}
        mode_map = {"off": LoopMode.OFF, "song": LoopMode.SONG, "queue": LoopMode.QUEUE}

        st = eng.state(msg.chat.id)
        new_mode = mode_map.get(mode_arg, cycle.get(st.loop, LoopMode.OFF))
        await eng.set_loop(msg.chat.id, new_mode)

        emoji = {"off": "🔁", "song": "🔂", "queue": "🔁"}
        label = {LoopMode.OFF: "Off", LoopMode.SONG: "Song", LoopMode.QUEUE: "Queue"}
        loop_msg = await msg.reply(f"{emoji.get(new_mode.value, '🔁')} Loop mode: **{label[new_mode]}**")
        asyncio.create_task(delete_after(loop_msg, 10))
        if st.current:
            await eng._update_now_playing(msg.chat.id, st.current)
        try:
            await msg.delete()
        except Exception:
            pass

    # ─── /shuffle ─────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["shuffle"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    @authorized_only
    async def shuffle_cmd(client, msg):
        count = await eng.shuffle_queue(msg.chat.id)
        if count == 0:
            err_msg = await msg.reply("Queue is empty, nothing to shuffle.")
            asyncio.create_task(delete_after(err_msg, 10))
        else:
            shuf_msg = await msg.reply(f"🔀 Queue shuffled! ({count} tracks)")
            asyncio.create_task(delete_after(shuf_msg, 10))
            st = eng.state(msg.chat.id)
            if st.current:
                await eng._update_now_playing(msg.chat.id, st.current)
        try:
            await msg.delete()
        except Exception:
            pass

    # ─── /clearqueue ──────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["clearqueue", "cq", "clear"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    @authorized_only
    async def clearq_cmd(client, msg):
        st = eng.state(msg.chat.id)
        n  = len(st.queue)
        st.queue.clear()
        clear_msg = await msg.reply(f"🗑 Cleared {n} track(s) from the queue.")
        asyncio.create_task(delete_after(clear_msg, 10))
        try:
            await msg.delete()
        except Exception:
            pass

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
        bot_username = client.me.username if client.me else (await client.get_me()).username
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add me to chat ➕", url=f"https://t.me/{bot_username}?startgroup=true")]
        ])
        await msg.reply(HELP_TEXT, reply_markup=keyboard, disable_web_page_preview=True)

    # ─── Inline buttons from now-playing card ─────────────────────────────────
    @bot.on_callback_query(filters.regex(r"^music:"))
    @rate_limit
    @authorized_only
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

        # Refresh now-playing card by deleting the old message and sending a new one (floating window)
        if st.current:
            try:
                await eng._update_now_playing(chat_id, st.current)
            except Exception:
                pass

    # ─── /promote ─────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["promote"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    @admin_only
    async def promote_cmd(client: Client, msg: Message):
        if msg.chat.id > 0:
            err_msg = await msg.reply("❌ This command can only be used in groups.", quote=True)
            asyncio.create_task(delete_after(err_msg, 10))
            try:
                await msg.delete()
            except Exception:
                pass
            return

        target_user_id = None
        target_name = ""

        if msg.reply_to_message:
            target = msg.reply_to_message.from_user
            if target:
                target_user_id = target.id
                target_name = target.first_name
        elif len(msg.command) > 1:
            arg = msg.command[1]
            if arg.isdigit():
                target_user_id = int(arg)
                target_name = f"User {arg}"
            else:
                try:
                    user_info = await client.get_users(arg)
                    target_user_id = user_info.id
                    target_name = user_info.first_name
                except Exception:
                    err_msg = await msg.reply("❌ User not found. Use ID, reply, or username.", quote=True)
                    asyncio.create_task(delete_after(err_msg, 10))
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                    return
        
        if not target_user_id:
            err_msg = await msg.reply("❌ Please reply to a user or provide username/ID to promote.", quote=True)
            asyncio.create_task(delete_after(err_msg, 10))
            try:
                await msg.delete()
            except Exception:
                pass
            return

        await db.promote_user(msg.chat.id, target_user_id)
        promo_msg = await msg.reply(f"✅ **{target_name}** has been promoted to music control operator in this group.", quote=True)
        asyncio.create_task(delete_after(promo_msg, 10))
        try:
            await msg.delete()
        except Exception:
            pass

    # ─── /demote ──────────────────────────────────────────────────────────────
    @bot.on_message(filters.command(["demote"], prefixes=cfg.COMMAND_PREFIX))
    @rate_limit
    @admin_only
    async def demote_cmd(client: Client, msg: Message):
        if msg.chat.id > 0:
            err_msg = await msg.reply("❌ This command can only be used in groups.", quote=True)
            asyncio.create_task(delete_after(err_msg, 10))
            try:
                await msg.delete()
            except Exception:
                pass
            return

        target_user_id = None
        target_name = ""

        if msg.reply_to_message:
            target = msg.reply_to_message.from_user
            if target:
                target_user_id = target.id
                target_name = target.first_name
        elif len(msg.command) > 1:
            arg = msg.command[1]
            if arg.isdigit():
                target_user_id = int(arg)
                target_name = f"User {arg}"
            else:
                try:
                    user_info = await client.get_users(arg)
                    target_user_id = user_info.id
                    target_name = user_info.first_name
                except Exception:
                    err_msg = await msg.reply("❌ User not found. Use ID, reply, or username.", quote=True)
                    asyncio.create_task(delete_after(err_msg, 10))
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                    return
        
        if not target_user_id:
            err_msg = await msg.reply("❌ Please reply to a user or provide username/ID to demote.", quote=True)
            asyncio.create_task(delete_after(err_msg, 10))
            try:
                await msg.delete()
            except Exception:
                pass
            return

        await db.demote_user(msg.chat.id, target_user_id)
        demote_msg = await msg.reply(f"🗑 **{target_name}** has been demoted and can no longer control music.", quote=True)
        asyncio.create_task(delete_after(demote_msg, 10))
        try:
            await msg.delete()
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
