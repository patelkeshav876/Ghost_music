"""
utils/ui.py
Builds formatted message text and inline keyboards for bot responses.
"""

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from streaming.engine import ChatState, Track, LoopMode


_LOOP_ICON = {LoopMode.OFF: "🔁", LoopMode.SONG: "🔂", LoopMode.QUEUE: "🔁✓"}
_SOURCE_ICON = {"youtube": "▶️", "spotify": "💚", "soundcloud": "☁️", "file": "📎", "playlist": "🎵"}


def _esc(text: str) -> str:
    """Escape Markdown special characters to avoid ENTITY_BOUNDS_INVALID."""
    for ch in ("_", "*", "[", "]", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


def build_now_playing(track: Track, st: ChatState) -> tuple[str, InlineKeyboardMarkup]:
    src_icon  = _SOURCE_ICON.get(track.source, "🎵")
    loop_lbl  = {LoopMode.OFF: "Off", LoopMode.SONG: "Song", LoopMode.QUEUE: "Queue"}[st.loop]
    q_count   = len(st.queue)
    title     = _esc(track.title)
    requester = _esc(track.requester_name)

    text = (
        f"{src_icon} **Now Playing**\n\n"
        f"🎵 **{title}**\n"
        f"⏱ Duration: `{track.duration_str}`\n"
        f"👤 Requested by: {requester}\n"
        f"🔊 Volume: {st.volume}%\n"
        f"🔁 Loop: {loop_lbl}\n"
        f"📋 Queue: {q_count} track(s) remaining"
        + ("\n⏸ *Paused*" if st.is_paused else "")
    ).strip()

    pause_btn = ("▶️ Resume", "music:resume") if st.is_paused else ("⏸ Pause", "music:pause")
    buttons   = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(pause_btn[0], callback_data=pause_btn[1]),
            InlineKeyboardButton("⏭ Skip",    callback_data="music:skip"),
            InlineKeyboardButton("⏹ Stop",    callback_data="music:stop"),
        ],
        [
            InlineKeyboardButton(f"{_LOOP_ICON[st.loop]} Loop", callback_data="music:loop"),
            InlineKeyboardButton("🔀 Shuffle",  callback_data="music:shuffle"),
            InlineKeyboardButton("📋 Queue",    callback_data="music:queue"),
        ],
    ])
    return text, buttons


def build_queue_text(st: ChatState) -> str:
    lines = [f"📋 **Queue** ({len(st.queue)} track(s))"]
    if st.current:
        lines.append(f"\n▶️ **Now:** {_esc(st.current.title)} `[{st.current.duration_str}]`")
    if st.queue:
        lines.append("\n**Up next:**")
        for i, t in enumerate(st.queue[:15], 1):
            lines.append(f"{i}. {_esc(t.title)} `[{t.duration_str}]`")
        if len(st.queue) > 15:
            lines.append(f"… and {len(st.queue)-15} more")
    return "\n".join(lines)

