import asyncio
import sys
from pyrogram import Client
from config.settings import cfg
import logging
logging.basicConfig(level=logging.WARNING)

async def check():
    try:
        app = Client(":memory:", api_id=cfg.API_ID, api_hash=cfg.API_HASH, session_string=cfg.SESSION_STRING)
        await app.start()
        me = await app.get_me()
        print(f"ASSISTANT USERNAME: {me.username}")
        print(f"ASSISTANT IS BOT: {me.is_bot}")
        await app.stop()
    except Exception as e:
        print(f"Error checking session: {e}")

asyncio.run(check())
