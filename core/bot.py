"""
core/bot.py
Main GhostMusicBot class — composes all layers.
"""

import asyncio
import importlib
import logging
from pathlib import Path

import pyrogram
from pyrogram import Client
from pytgcalls import PyTgCalls

from config.settings import cfg
from database.mongo import Database
from streaming.engine import StreamEngine
from services.stats_api import StatsAPI
from utils.logger import setup_logger

logger = setup_logger("core.bot")

HANDLER_DIRS = ["handlers"]


class GhostMusicBot:
    """
    Top-level composition root.
    Owns:  Pyrogram client (bot account)
           Pyrogram client (userbot/assistant for VC join)
           PyTgCalls engine
           MongoDB connection
           StatsAPI (aiohttp server for dashboard)
    """

    def __init__(self):
        # ── Pyrogram bot client ───────────────────────────────────────────────
        self.bot: Client = Client(
            name="ghostmusic_bot",
            api_id=cfg.API_ID,
            api_hash=cfg.API_HASH,
            bot_token=cfg.BOT_TOKEN,
            sleep_threshold=30,
            max_concurrent_transmissions=10,
        )

        # ── Pyrogram userbot (assistant) — required for voice chat ────────────
        self.assistant: Client = Client(
            name="ghostmusic_assistant",
            api_id=cfg.API_ID,
            api_hash=cfg.API_HASH,
            session_string=cfg.SESSION_STRING,
        )

        # ── PyTgCalls — wraps the assistant client ────────────────────────────
        try:
            self.calls: PyTgCalls = PyTgCalls(self.assistant)
        except Exception as e:
            logger.warning(f"PyTgCalls initialization failed: {e}. Voice calls disabled.")
            self.calls = None

        # ── Database ──────────────────────────────────────────────────────────
        self.db: Database = Database(cfg.MONGO_URI, cfg.DB_NAME)

        # ── Stream engine (queue management + playback state) ─────────────────
        if self.calls:
            self.stream: StreamEngine = StreamEngine(self.calls, self.bot, self.db)
        else:
            self.stream = None

        # ── Stats HTTP API ────────────────────────────────────────────────────
        self.stats_api: StatsAPI = StatsAPI(self, cfg.STATS_PORT, cfg.STATS_SECRET)

        self._running = False
        self._idle_event = asyncio.Event()

    # ─────────────────────────────────────────────────────────────────────────
    async def start(self):
        logger.info("Starting GhostMusic bot…")

        # DB first — everything else depends on it
        await self.db.connect()
        logger.info("MongoDB connected.")

        # Start Pyrogram clients
        await self.bot.start()
        await self.assistant.start()
        me = await self.bot.get_me()
        logger.info(f"Bot logged in as @{me.username} ({me.id})")

        # Start PyTgCalls
        if self.calls:
            await self.calls.start()
            logger.info("PyTgCalls engine started.")
        else:
            logger.warning("PyTgCalls engine not initialized. Voice chat features will be disabled.")

        # Load all handler modules dynamically
        self._load_handlers()

        # Start stats API
        await self.stats_api.start()
        logger.info(f"Stats API running on port {cfg.STATS_PORT}")

        self._running = True
        logger.info("GhostMusic is ready! 🎵")

    async def stop(self):
        if not self._running:
            return
        logger.info("Shutting down GhostMusic…")
        self._running = False
        self._idle_event.set()

        try:
            await self.stream.stop_all()
            await self.calls.stop()
            await self.assistant.stop()
            await self.bot.stop()
            await self.db.close()
            await self.stats_api.stop()
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")

    async def idle(self):
        """Block until stop() is called."""
        await self._idle_event.wait()

    # ─────────────────────────────────────────────────────────────────────────
    def _load_handlers(self):
        """
        Auto-discover and register all handler modules inside handlers/.
        Each module must expose a `register(bot)` function.
        """
        base = Path(__file__).parent.parent / "handlers"
        for path in sorted(base.glob("*.py")):
            if path.name.startswith("_"):
                continue
            module_name = f"handlers.{path.stem}"
            try:
                mod = importlib.import_module(module_name)
                if hasattr(mod, "register"):
                    mod.register(self)
                    logger.debug(f"Loaded handler: {module_name}")
            except Exception as e:
                logger.error(f"Failed to load handler {module_name}: {e}")
