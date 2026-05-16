from pyrogram import Client
import asyncio

async def gen():
    async with Client("temp_session_gen", in_memory=True, api_id=*, api_hash="*") as c:
        print(await c.export_session_string())

asyncio.run(gen())
