"""
services/stats_api.py
Lightweight aiohttp HTTP API server.
Exposes bot stats to the web dashboard.
All write endpoints require the STATS_SECRET header.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from core.bot import GhostMusicBot

logger = logging.getLogger("services.stats_api")


class StatsAPI:
    def __init__(self, bot_app, port: int, secret: str):
        self._app   = bot_app          # GhostMusicBot instance
        self._port  = port
        self._secret = secret
        self._server: web.AppRunner | None = None
        self._http  = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        r = self._http.router
        r.add_get("/",            self._handle_root)
        r.add_get("/api/health",  self._handle_health)
        r.add_get("/api/stats",   self._handle_stats)
        r.add_get("/api/streams", self._handle_streams)
        r.add_post("/api/broadcast", self._handle_broadcast)
        r.add_post("/api/stop/{chat_id}", self._handle_stop_chat)
        r.add_get("/api/ads",     self._handle_get_ads)
        r.add_post("/api/ads",    self._handle_post_ads)
        r.add_delete("/api/ads/{ad_id}", self._handle_delete_ad)

        # CORS middleware
        self._http.middlewares.append(self._cors_middleware)

    @web.middleware
    async def _cors_middleware(self, request, handler):
        if request.method == "OPTIONS":
            resp = web.Response()
        else:
            resp = await handler(request)
        
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Secret"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        return resp

    def _auth(self, request: web.Request) -> bool:
        return request.headers.get("X-Secret") == self._secret

    # ─────────────────────────────────────────────────────────────────────────

    async def _handle_root(self, req):
        return web.json_response({"service": "GhostMusic Stats API", "version": "2.0"})

    async def _handle_health(self, req):
        return web.json_response({
            "status":    "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def _handle_stats(self, req):
        eng    = self._app.stream
        db     = self._app.db

        # Live data from memory
        active_streams = [
            {
                "chat_id":  cid,
                "title":    st.current.title if st.current else None,
                "paused":   st.is_paused,
                "volume":   st.volume,
                "queue":    len(st.queue),
                "loop":     st.loop.value,
            }
            for cid, st in eng._states.items()
            if st.is_playing
        ]

        # DB stats
        try:
            global_db = await db.global_stats()
            total_users = await db.total_users()
        except Exception:
            global_db = {}
            total_users = 0

        return web.json_response({
            "active_streams":  len(active_streams),
            "total_chats":     global_db.get("total_chats", 0),
            "total_plays":     global_db.get("total_plays", 0),
            "total_users":     total_users,
            "streams":         active_streams,
        })

    async def _handle_streams(self, req):
        eng = self._app.stream
        data = []
        for cid, st in eng._states.items():
            data.append({
                "chat_id": cid,
                "is_playing": st.is_playing,
                "is_paused":  st.is_paused,
                "current":    st.current.title if st.current else None,
                "queue_size": len(st.queue),
                "volume":     st.volume,
                "loop":       st.loop.value,
            })
        return web.json_response(data)

    async def _handle_broadcast(self, req: web.Request):
        if not self._auth(req):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            body = await req.json()
            text = body.get("message", "").strip()
            if not text:
                return web.json_response({"error": "Empty message"}, status=400)
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        bot    = self._app.bot
        db     = self._app.db
        chats  = await db.all_chat_ids()
        sent   = failed = 0
        for cid in chats:
            try:
                await bot.send_message(cid, text)
                sent += 1
            except Exception:
                failed += 1
        return web.json_response({"sent": sent, "failed": failed})

    async def _handle_stop_chat(self, req: web.Request):
        if not self._auth(req):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            chat_id = int(req.match_info["chat_id"])
        except ValueError:
            return web.json_response({"error": "Invalid chat_id"}, status=400)
        await self._app.stream.stop(chat_id)
        return web.json_response({"ok": True, "chat_id": chat_id})

    async def _handle_get_ads(self, req: web.Request):
        if not self._auth(req):
            return web.json_response({"error": "Unauthorized"}, status=401)
        db = self._app.db
        ads = await db.list_all_ads()
        # Clean Mongo _id and format datetime
        for ad in ads:
            ad.pop("_id", None)
            if "updated_at" in ad and isinstance(ad["updated_at"], datetime):
                ad["updated_at"] = ad["updated_at"].isoformat()
        return web.json_response(ads)

    async def _handle_post_ads(self, req: web.Request):
        if not self._auth(req):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            body = await req.json()
            ad_id = body.get("ad_id", "").strip()
            text = body.get("text", "").strip()
            button_text = body.get("button_text", "").strip()
            button_url = body.get("button_url", "").strip()
            active = bool(body.get("active", True))
            
            if not ad_id or not text:
                return web.json_response({"error": "Missing ad_id or text"}, status=400)
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        db = self._app.db
        await db.save_ad(ad_id, text, button_text, button_url, active)
        return web.json_response({"ok": True})

    async def _handle_delete_ad(self, req: web.Request):
        if not self._auth(req):
            return web.json_response({"error": "Unauthorized"}, status=401)
        ad_id = req.match_info.get("ad_id")
        if not ad_id:
            return web.json_response({"error": "Missing ad_id"}, status=400)
        
        db = self._app.db
        deleted = await db.delete_ad(ad_id)
        return web.json_response({"ok": deleted})

    # ─────────────────────────────────────────────────────────────────────────

    async def start(self):
        self._server = web.AppRunner(self._http)
        await self._server.setup()
        site = web.TCPSite(self._server, "0.0.0.0", self._port)
        await site.start()
        logger.info(f"Stats API listening on :{self._port}")

    async def stop(self):
        if self._server:
            await self._server.cleanup()
