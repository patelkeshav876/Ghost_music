from pyrogram import Client
import asyncio

async def gen():
    async with Client("temp_session_gen", in_memory=True, api_id=31942889, api_hash="6e84cb671c7241f0dc73fb55fd00ec7d") as c:
        print(await c.export_session_string())

asyncio.run(gen())