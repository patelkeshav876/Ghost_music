"""
GhostMusic Bot — Production Entry Point
Starts both the Pyrogram client and PyTgCalls streaming engine.
"""

import asyncio
import sys
import signal
import logging

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
