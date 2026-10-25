"""
streaming/engine.py
Core streaming engine.
Manages per-chat queues, playback state, and PyTgCalls interactions.
Uses pytgcalls v1.1.6 API.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pytgcalls import PyTgCalls
from pytgcalls.types.input_stream import AudioPiped, MediaStream
from pytgcalls.types.input_stream.audio_parameters import AudioParameters
from pyrogram import Client

from config.settings import cfg
from database.mongo import Database
from utils.helpers import format_duration, delete_after

logger = logging.getLogger("streaming.engine")

# Standard high-quality stereo audio parameters
_AUDIO_PARAMS = AudioParameters(bitrate=48000, channels=2)


class LoopMode(Enum):
    OFF     = "off"
    SONG    = "song"    # loop current track
    QUEUE   = "queue"   # loop entire queue


@dataclass
class Track:
    """Represents a single queued track."""
    title:      str
    url:        str            # direct streamable URL (from yt-dlp)
    webpage:    str            # original user-provided URL / search term
    duration:   int            # seconds
    thumbnail:  str
    requester_id:   int
    requester_name: str
    chat_id:    int
    source:     str = "youtube"   # youtube | spotify | soundcloud | file
    file_id:    Optional[str] = None

    @property
    def duration_str(self) -> str:
        return format_duration(self.duration)


@dataclass
class ChatState:
    """Per-chat playback state — lives in memory only."""
    chat_id:        int
    queue:          list            = field(default_factory=list)   # list[Track]
    current:        Optional[Track] = None
    loop:           LoopMode        = LoopMode.OFF
    volume:         int             = cfg.DEFAULT_VOLUME
    is_playing:     bool            = False
    is_paused:      bool            = False
    shuffled:       bool            = False
    now_playing_msg: Optional[int]  = None   # message_id to edit
    idle_task:      Optional[asyncio.Task] = None


class StreamEngine:
    """
    Owns all ChatState objects and drives PyTgCalls v1.1.6.
    Thread-safe: uses asyncio.Lock per chat.
    """

    def __init__(self, calls: PyTgCalls, bot: Client, db: Database, assistant: Client = None):
        self.calls     = calls
        self.bot       = bot
        self.assistant = assistant  # userbot — used to create group calls
        self.assistant_user = None
        self.db        = db
        self._states: dict[int, ChatState] = {}
        self._locks:  dict[int, asyncio.Lock] = {}

        # v1.x uses the @on_stream_end decorator
        @self.calls.on_stream_end()
        async def stream_end_handler(client, update):
            await self._on_stream_end(client, update)

        self._ad_task = asyncio.create_task(self._ad_scheduler_loop())

    # ─────────────────────────────────────────────────────────────────────────
    #  Public API
    # ─────────────────────────────────────────────────────────────────────────

    def state(self, chat_id: int) -> ChatState:
        if chat_id not in self._states:
            self._states[chat_id] = ChatState(chat_id=chat_id)
            self._locks[chat_id] = asyncio.Lock()
        return self._states[chat_id]

    def lock(self, chat_id: int) -> asyncio.Lock:
        self.state(chat_id)   # ensure lock exists
        return self._locks[chat_id]

    async def enqueue(self, chat_id: int, track: Track) -> int:
        """Add track to queue. Returns queue position (1-indexed)."""
        async with self.lock(chat_id):
            st = self.state(chat_id)
            if len(st.queue) >= cfg.MAX_QUEUE_SIZE:
                raise OverflowError(f"Queue is full ({cfg.MAX_QUEUE_SIZE} tracks max).")
            st.queue.append(track)
            pos = len(st.queue)
            await self.db.increment_stat(chat_id, "tracks_queued")
        return pos

    async def play_next(self, chat_id: int) -> Optional[Track]:
        """
        Pops the next track from the queue and starts streaming.
        Returns the track or None if queue is empty.
        """
        async with self.lock(chat_id):
            st = self.state(chat_id)

            # Loop single song
            if st.loop == LoopMode.SONG and st.current:
                track = st.current
            elif st.queue:
                track = st.queue.pop(0)
                # Loop queue — re-add to end
                if st.loop == LoopMode.QUEUE and st.current:
                    st.queue.append(st.current)
            else:
                st.current   = None
                st.is_playing = False
                await self._schedule_auto_leave(chat_id)
                return None

            st.current   = track
            st.is_playing = True
            st.is_paused  = False

        await self._start_stream(chat_id, track)
        return track

    async def _start_stream(self, chat_id: int, track: Track):
        """Tell PyTgCalls v1.x to start/switch to a track."""
        # If enqueued from playlist, it has a webpage URL, so resolve it dynamically to a streamable URL first
        if track.source == "playlist" or (track.url and ("youtube.com" in track.url or "youtu.be" in track.url) and "manifest" not in track.url and "videoplayback" not in track.url):
            try:
                from services.resolver import resolver
                resolved_tracks = await resolver.resolve(track.webpage, track.requester_id, track.requester_name, chat_id)
                if resolved_tracks:
                    track.url = resolved_tracks[0].url
                    track.thumbnail = resolved_tracks[0].thumbnail
                    track.duration = resolved_tracks[0].duration
            except Exception as e:
                logger.error(f"[{chat_id}] Dynamic URL resolution failed for {track.title}: {e}")

        # MediaStream with IGNORE on video streams audio cleanly through ffmpeg
        stream = MediaStream(track.url, audio_parameters=_AUDIO_PARAMS, video_flags=MediaStream.IGNORE)
        st = self.state(chat_id)
        try:
            # Check if already in a call -> try change_stream; if that fails or not in call -> join fresh
            active_chats = [c.chat_id for c in self.calls.active_calls]
            joined_fresh = False
            from pyrogram.raw import functions, types
            input_call = None

            if chat_id in active_chats:
                try:
                    await self.calls.change_stream(chat_id, stream)
                    logger.info(f"[{chat_id}] Stream changed to: {track.title}")
                except Exception as change_err:
                    logger.warning(f"[{chat_id}] change_stream failed ({change_err}), falling back to fresh join...")
                    try:
                        await self.calls.leave_group_call(chat_id)
                    except Exception:
                        pass
                    joined_fresh = True
            else:
                joined_fresh = True

            if joined_fresh:
                # ── 0. Ensure Assistant is a member of the group ─────────────
                if self.assistant:
                    asst_id = self.assistant_user.id if self.assistant_user else None
                    in_group = False
                    if asst_id:
                        try:
                            m = await self.bot.get_chat_member(chat_id, asst_id)
                            if m and m.status not in ("left", "kicked"):
                                in_group = True
                        except Exception:
                            in_group = False

                    if not in_group:
                        logger.info(f"[{chat_id}] Assistant is not in group. Auto-adding...")
                        added = False
                        if asst_id:
                            try:
                                await self.bot.add_chat_members(chat_id, asst_id)
                                added = True
                                logger.info(f"[{chat_id}] Assistant added to group by bot.")
                            except Exception as add_err:
                                logger.debug(f"[{chat_id}] Bot add_chat_members failed: {add_err}")

                        if not added:
                            try:
                                invite = await self.bot.export_chat_invite_link(chat_id)
                                await self.assistant.join_chat(invite)
                                logger.info(f"[{chat_id}] Assistant joined group via invite link.")
                            except Exception as join_err:
                                logger.warning(f"[{chat_id}] Assistant auto-join failed: {join_err}")

                # -- Robust VC Resolution & Cache Pre-population -------------
                resolved_chat_id = chat_id
                try:
                    resolved_chat_id = await self.calls._resolve_chat_id(chat_id)
                except Exception:
                    pass

                # 1. Check via Bot client (Bot is already admin in group, Telegram returns full_chat.call)
                try:
                    bot_peer = await self.bot.resolve_peer(chat_id)
                    if isinstance(bot_peer, types.InputPeerChannel):
                        full = await self.bot.invoke(
                            functions.channels.GetFullChannel(
                                channel=types.InputChannel(
                                    channel_id=bot_peer.channel_id,
                                    access_hash=bot_peer.access_hash,
                                )
                            )
                        )
                        if full and hasattr(full, "full_chat") and full.full_chat.call:
                            input_call = full.full_chat.call
                    elif isinstance(bot_peer, types.InputPeerChat):
                        full = await self.bot.invoke(
                            functions.messages.GetFullChat(chat_id=bot_peer.chat_id)
                        )
                        if full and hasattr(full, "full_chat") and full.full_chat.call:
                            input_call = full.full_chat.call
                    if input_call:
                        logger.info(f"[{chat_id}] Found active Voice Chat via bot: id={input_call.id}")
                except Exception as b_err:
                    logger.debug(f"[{chat_id}] Bot GetFullChannel error: {b_err}")

                # 2. Check via Assistant client if bot didn't find it
                if input_call is None and self.assistant:
                    try:
                        asst_peer = await self.assistant.resolve_peer(chat_id)
                        if isinstance(asst_peer, types.InputPeerChannel):
                            full = await self.assistant.invoke(
                                functions.channels.GetFullChannel(
                                    channel=types.InputChannel(
                                        channel_id=asst_peer.channel_id,
                                        access_hash=asst_peer.access_hash,
                                    )
                                )
                            )
                            if full and hasattr(full, "full_chat") and full.full_chat.call:
                                input_call = full.full_chat.call
                        elif isinstance(asst_peer, types.InputPeerChat):
                            full = await self.assistant.invoke(
                                functions.messages.GetFullChat(chat_id=asst_peer.chat_id)
                            )
                            if full and hasattr(full, "full_chat") and full.full_chat.call:
                                input_call = full.full_chat.call
                        if input_call:
                            logger.info(f"[{chat_id}] Found active Voice Chat via assistant: id={input_call.id}")
                    except Exception as a_err:
                        logger.debug(f"[{chat_id}] Assistant GetFullChannel error: {a_err}")

                # 3. If call exists, populate pytgcalls cache directly
                if input_call is not None:
                    try:
                        self.calls._app._bind_client._cache.set_cache(chat_id, input_call)
                        self.calls._app._bind_client._cache.set_cache(resolved_chat_id, input_call)
                        logger.info(f"[{chat_id}] Cached InputGroupCall ({input_call.id}) in pytgcalls cache.")
                    except Exception as c_err:
                        logger.debug(f"Cache injection error: {c_err}")
                else:
                    # Voice chat not active on Telegram yet; try auto-creating via assistant
                    created = False
                    if self.assistant:
                        try:
                            import random
                            asst_peer = await self.assistant.resolve_peer(chat_id)
                            create_res = await self.assistant.invoke(
                                functions.phone.CreateGroupCall(
                                    peer=asst_peer,
                                    random_id=random.randint(10000, 99999),
                                )
                            )
                            if hasattr(create_res, "updates"):
                                for upd in create_res.updates:
                                    if isinstance(upd, types.UpdateGroupCall) and isinstance(upd.call, types.GroupCall):
                                        if upd.call.schedule_date is None:
                                            inp_call = types.InputGroupCall(
                                                id=upd.call.id,
                                                access_hash=upd.call.access_hash,
                                            )
                                            self.calls._app._bind_client._cache.set_cache(chat_id, inp_call)
                                            self.calls._app._bind_client._cache.set_cache(resolved_chat_id, inp_call)
                                            created = True
                                            break
                            await asyncio.sleep(1.0)
                        except Exception as create_err:
                            logger.warning(f"[{chat_id}] Auto-create voice chat failed: {create_err}")

                    if not created:
                        raise RuntimeError("NO_ACTIVE_GROUP_CALL")

                # Join group call with auto_start=False so pytgcalls never triggers bot-incompatible CreateGroupCall
                try:
                    await self.calls.join_group_call(chat_id, stream, auto_start=False)
                except Exception as join_err:
                    if "GROUPCALL_INVALID" in str(join_err):
                        logger.warning(f"[{chat_id}] Cached group call invalid. Invalidate cache and retry...")
                        try:
                            self.calls._app._bind_client._cache.drop_cache(chat_id)
                            self.calls._app._bind_client._cache.drop_cache(resolved_chat_id)
                        except Exception:
                            pass
                        bot_peer = await self.bot.resolve_peer(chat_id)
                        if isinstance(bot_peer, types.InputPeerChannel):
                            full = await self.bot.invoke(
                                functions.channels.GetFullChannel(
                                    channel=types.InputChannel(
                                        channel_id=bot_peer.channel_id,
                                        access_hash=bot_peer.access_hash,
                                    )
                                )
                            )
                            if full and hasattr(full, "full_chat") and full.full_chat.call:
                                input_call = full.full_chat.call
                                self.calls._app._bind_client._cache.set_cache(chat_id, input_call)
                                self.calls._app._bind_client._cache.set_cache(resolved_chat_id, input_call)
                                await self.calls.join_group_call(chat_id, stream, auto_start=False)
                            else:
                                raise RuntimeError("NO_ACTIVE_GROUP_CALL")
                    else:
                        raise join_err

            logger.info(f"[{chat_id}] Streaming: {track.title}")

            # ── Auto-unmute assistant in voice chat ──────────────────────────
            # Fixes silent playback when voice chat defaults new members to muted
            if self.assistant:
                # 1. Admin bot unmutes assistant participant
                try:
                    asst_id = self.assistant_user.id if self.assistant_user else (await self.assistant.get_me()).id
                    bot_asst_peer = await self.bot.resolve_peer(asst_id)
                    unmute_call = input_call or self.calls._app._bind_client._cache.get_full_chat(chat_id)
                    if unmute_call:
                        await self.bot.invoke(
                            functions.phone.EditGroupCallParticipant(
                                call=unmute_call,
                                participant=bot_asst_peer,
                                muted=False,
                            )
                        )
                        logger.info(f"[{chat_id}] Assistant microphone unmuted by admin bot.")
                except Exception as b_unmute_err:
                    logger.debug(f"[{chat_id}] Admin bot unmute note: {b_unmute_err}")

                # 2. Assistant self-unmutes
                try:
                    unmute_call = input_call or self.calls._app._bind_client._cache.get_full_chat(chat_id)
                    if unmute_call:
                        await self.assistant.invoke(
                            functions.phone.EditGroupCallParticipant(
                                call=unmute_call,
                                participant=types.InputPeerSelf(),
                                muted=False,
                            )
                        )
                        logger.info(f"[{chat_id}] Assistant self-unmuted.")
                except Exception as a_unmute_err:
                    logger.debug(f"[{chat_id}] Assistant self-unmute note: {a_unmute_err}")

            # 3. Unmute WebRTC stream in PyTgCalls
            try:
                await self.calls.unmute_stream(chat_id)
            except Exception as stream_unmute_err:
                logger.debug(f"[{chat_id}] Stream unmute note: {stream_unmute_err}")

            try:
                await self.calls.change_volume_call(chat_id, st.volume)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[{chat_id}] Stream start error: {e}")
            raise  # re-raise so callers can show a proper error message


    async def skip(self, chat_id: int) -> Optional[Track]:
        """Skip current track, play next."""
        st = self.state(chat_id)
        if not st.is_playing:
            return None
        # Override loop-song for explicit skip
        original_loop = st.loop
        if st.loop == LoopMode.SONG:
            st.loop = LoopMode.OFF
        track = await self.play_next(chat_id)
        if track is None and original_loop == LoopMode.SONG:
            st.loop = original_loop
        return track

    async def pause(self, chat_id: int) -> bool:
        st = self.state(chat_id)
        if not st.is_playing or st.is_paused:
            return False
        try:
            await self.calls.pause_stream(chat_id)
            st.is_paused = True
            return True
        except Exception as e:
            logger.error(f"Pause error: {e}")
            return False

    async def resume(self, chat_id: int) -> bool:
        st = self.state(chat_id)
        if not st.is_paused:
            return False
        try:
            await self.calls.resume_stream(chat_id)
            st.is_paused = False
            return True
        except Exception as e:
            logger.error(f"Resume error: {e}")
            return False

    async def stop(self, chat_id: int):
        """Stop playback and clear queue."""
        async with self.lock(chat_id):
            st = self.state(chat_id)
            st.queue.clear()
            st.current   = None
            st.is_playing = False
            st.is_paused  = False
        try:
            await self.calls.leave_group_call(chat_id)
        except Exception:
            pass
        await self._cancel_idle_task(chat_id)

    async def stop_all(self):
        """Graceful shutdown — stop all active streams."""
        if hasattr(self, "_ad_task") and self._ad_task:
            self._ad_task.cancel()
        for chat_id in list(self._states.keys()):
            try:
                await self.stop(chat_id)
            except Exception:
                pass

    async def set_volume(self, chat_id: int, volume: int) -> bool:
        volume = max(1, min(200, volume))
        st = self.state(chat_id)
        st.volume = volume
        if st.is_playing:
            try:
                await self.calls.change_volume_call(chat_id, volume)
            except Exception:
                pass
        return True

    async def shuffle_queue(self, chat_id: int) -> int:
        """Shuffle the pending queue. Returns new queue length."""
        import random
        async with self.lock(chat_id):
            st = self.state(chat_id)
            random.shuffle(st.queue)
            st.shuffled = True
            return len(st.queue)

    async def set_loop(self, chat_id: int, mode: LoopMode):
        self.state(chat_id).loop = mode

    # ─────────────────────────────────────────────────────────────────────────
    #  PyTgCalls callbacks
    # ─────────────────────────────────────────────────────────────────────────

    async def _on_stream_end(self, client, update):
        """Called by PyTgCalls when a stream finishes naturally."""
        chat_id = update.chat_id
        logger.debug(f"[{chat_id}] Stream ended, advancing queue.")
        track = await self.play_next(chat_id)
        if track:
            # Update now-playing message if we have one
            await self._update_now_playing(chat_id, track)
        else:
            await self._send_queue_empty(chat_id)

    async def _on_vc_closed(self, client, update):
        """Called when the voice chat is closed externally."""
        chat_id = update.chat_id
        logger.info(f"[{chat_id}] Voice chat closed externally, cleaning state.")
        async with self.lock(chat_id):
            st = self.state(chat_id)
            st.queue.clear()
            st.current   = None
            st.is_playing = False
            st.is_paused  = False

    # ─────────────────────────────────────────────────────────────────────────
    #  Auto-leave idle timer
    # ─────────────────────────────────────────────────────────────────────────

    async def _schedule_auto_leave(self, chat_id: int):
        await self._cancel_idle_task(chat_id)
        st = self.state(chat_id)

        async def _leave():
            await asyncio.sleep(cfg.AUTO_LEAVE_DELAY)
            st2 = self.state(chat_id)
            if not st2.is_playing:
                try:
                    await self.calls.leave_group_call(chat_id)
                    await self.bot.send_message(
                        chat_id, "👻 Left the voice chat (idle timeout)."
                    )
                except Exception:
                    pass

        st.idle_task = asyncio.create_task(_leave())

    async def _cancel_idle_task(self, chat_id: int):
        st = self.state(chat_id)
        if st.idle_task and not st.idle_task.done():
            st.idle_task.cancel()
        st.idle_task = None

    # ─────────────────────────────────────────────────────────────────────────
    #  UI helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _update_now_playing(self, chat_id: int, track: Track):
        from utils.ui import build_now_playing
        st = self.state(chat_id)
        
        # Load active ads from MongoDB
        ad_text, ad_btn_text, ad_btn_url = "", "", ""
        try:
            active_ads = await self.db.get_active_ads()
            if active_ads:
                import random
                ad = random.choice(active_ads)
                ad_text = ad.get("text", "")
                ad_btn_text = ad.get("button_text", "")
                ad_btn_url = ad.get("button_url", "")
        except Exception:
            pass

        text, buttons = build_now_playing(track, st, ad_text, ad_btn_text, ad_btn_url)

        
        # Try to generate a custom PIL Now Playing card image
        from utils.thumbnail import generate_now_playing_card
        photo_bytes = await generate_now_playing_card(
            title=track.title,
            artist=track.source.capitalize(),
            duration=track.duration_str,
            requester=track.requester_name,
            thumb_url=track.thumbnail,
            volume=st.volume
        )
        
        # Delete old message to avoid cluttering if we are changing to a new photo
        if st.now_playing_msg:
            try:
                await self.bot.delete_messages(chat_id, st.now_playing_msg)
            except Exception:
                pass
            st.now_playing_msg = None
            
        try:
            if photo_bytes:
                import io
                bio = io.BytesIO(photo_bytes)
                bio.name = "nowplaying.png"
                msg = await self.bot.send_photo(
                    chat_id,
                    photo=bio,
                    caption=text,
                    reply_markup=buttons
                )
            elif track.thumbnail:
                msg = await self.bot.send_photo(
                    chat_id,
                    photo=track.thumbnail,
                    caption=text,
                    reply_markup=buttons
                )
            else:
                msg = await self.bot.send_message(
                    chat_id, text, reply_markup=buttons, disable_web_page_preview=True
                )
            st.now_playing_msg = msg.id
        except Exception as e:
            logger.error(f"Failed to send now playing card: {e}")
            # Fallback to text message
            msg = await self.bot.send_message(
                chat_id, text, reply_markup=buttons, disable_web_page_preview=True
            )
            st.now_playing_msg = msg.id


    async def _send_queue_empty(self, chat_id: int):
        await self.bot.send_message(chat_id, "✅ Queue finished. Add more songs with /play!")

    async def _ad_scheduler_loop(self):
        """Sends active promotions to active streaming channels at configured interval."""
        # Wait a short duration on bot startup
        await asyncio.sleep(45)
        
        while True:
            try:
                # Find chats currently streaming
                active_chats = [
                    st.chat_id for st in self._states.values()
                    if st.is_playing and not st.is_paused
                ]
                
                if active_chats:
                    active_ads = await self.db.get_active_ads()
                    if active_ads:
                        import random
                        ad = random.choice(active_ads)
                        ad_text = ad.get("text", "")
                        btn_text = ad.get("button_text", "")
                        btn_url = ad.get("button_url", "")
                        
                        if ad_text:
                            from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                            markup = None
                            if btn_text and btn_url:
                                markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"🔗 {btn_text}", url=btn_url)]])
                            
                            for cid in active_chats:
                                try:
                                    sent = await self.bot.send_message(
                                        chat_id=cid,
                                        text=f"📢 **Promotion:**\n\n{ad_text}",
                                        reply_markup=markup,
                                        disable_web_page_preview=True
                                    )
                                    # Delete after 2 minutes to keep chat clean
                                    asyncio.create_task(delete_after(sent, 120))
                                except Exception:
                                    pass
            except Exception as e:
                logger.error(f"Error in ad scheduler loop: {e}")
            
            await asyncio.sleep(cfg.AD_INTERVAL)

