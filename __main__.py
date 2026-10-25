"""
GhostMusic Bot — Production Entry Point
Starts both the Pyrogram client and PyTgCalls streaming engine.
"""

import asyncio
import sys
import signal
import logging

# ── HOTFIX: Telegram extended channel IDs (up to 64-bit integers) ─────────────
# Pyrogram 2.0.106 has MIN_CHANNEL_ID = -1002147483647 (capped at 2^31-1).
# Modern Telegram channels and supergroups have IDs like -1003885728979.
# Without this patch, pyrogram.utils.get_peer_type() raises "Peer id invalid".
import pyrogram.utils
pyrogram.utils.MIN_CHANNEL_ID = -100999999999999
# ─────────────────────────────────────────────────────────────────────────────

from core.bot import GhostMusicBot
from utils.logger import setup_logger

logger = setup_logger("ghostmusic")


async def main():
    bot = GhostMusicBot()
    
    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.stop()))
    except NotImplementedError:
        pass  # Windows doesn't support add_signal_handler

    try:
        await bot.start()
        await bot.idle()
    except KeyboardInterrupt:
        pass
    finally:
        await bot.stop()
        logger.info("GhostMusic bot stopped cleanly.")


if __name__ == "__main__":
    if sys.version_info < (3, 10):
        print("Python 3.10+ required. Exiting.")
        sys.exit(1)
    asyncio.run(main())
