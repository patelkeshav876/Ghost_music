from pyrogram import Client
import asyncio

async def gen():
    async with Client("ghostmusic_session", api_id=26059638, api_hash="647c189153b70a804dff6cb64de7c523") as c:
        print(await c.export_session_string())

asyncio.run(gen())