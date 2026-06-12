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
from pytgcalls.types.input_stream import AudioPiped
from pytgcalls.types.input_stream.audio_parameters import AudioParameters
from pyrogram import Client

from config.settings import cfg
from database.mongo import Database
from utils.helpers import format_duration

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

    def __init__(self, calls: PyTgCalls, bot: Client, db: Database):
        self.calls   = calls
        self.bot     = bot
        self.db      = db
        self._states: dict[int, ChatState] = {}
        self._locks:  dict[int, asyncio.Lock] = {}

        # v1.x uses the @on_stream_end decorator
        @self.calls.on_stream_end()
        async def stream_end_handler(client, update):
            await self._on_stream_end(client, update)

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
        # AudioPiped streams any URL/file through ffmpeg — the correct v1.x way
        stream = AudioPiped(track.url, audio_parameters=_AUDIO_PARAMS)
        st = self.state(chat_id)
        try:
            # Check if already in a call → use change_stream, else join fresh
            active_chats = [c.chat_id for c in self.calls.active_calls]
            if chat_id in active_chats:
                await self.calls.change_stream(chat_id, stream)
            else:
                await self.calls.join_group_call(chat_id, stream)

            logger.info(f"[{chat_id}] Streaming: {track.title}")

            try:
                await self.calls.change_volume_call(chat_id, st.volume)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[{chat_id}] Stream start error: {e}")
            # Try to play next if this one fails
            await self.play_next(chat_id)

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
        text, buttons = build_now_playing(track, st)
        if st.now_playing_msg:
            try:
                await self.bot.edit_message_text(
                    chat_id, st.now_playing_msg, text,
                    reply_markup=buttons, disable_web_page_preview=True
                )
                return
            except Exception:
                pass
        msg = await self.bot.send_message(
            chat_id, text, reply_markup=buttons, disable_web_page_preview=True
        )
        st.now_playing_msg = msg.id

    async def _send_queue_empty(self, chat_id: int):
        await self.bot.send_message(chat_id, "✅ Queue finished. Add more songs with /play!")
