"""
database/mongo.py
All MongoDB interactions, cleanly separated from business logic.
With fallback to mock mode when MongoDB is unavailable.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError:
    AsyncIOMotorClient = None

logger = logging.getLogger("database.mongo")


class Database:
    def __init__(self, uri: str, db_name: str):
        self._uri  = uri
        self._name = db_name
        self._client: Optional[AsyncIOMotorClient] = None
        self._db = None
        self._mock_mode = False
        self._mock_data = {
            "chats": {},
            "users": {},
            "playlists": {},
            "history": [],
            "stats": {},
        }
        self._chat_counter = 0
        self._user_counter = 0

    # ─────────────────────────────────────────────────────────────────────────
    async def connect(self):
        if AsyncIOMotorClient is None:
            logger.warning("motor not available, using mock mode")
            self._mock_mode = True
            return
            
        try:
            self._client = AsyncIOMotorClient(self._uri, serverSelectionTimeoutMS=5000)
            self._db = self._client[self._name]
            # Verify connection
            await self._client.admin.command("ping")
            logger.info(f"MongoDB connected to '{self._name}'")
            await self._create_indexes()
        except Exception as e:
            logger.warning(f"MongoDB connection failed: {e}")
            logger.info("Falling back to mock mode (data will not persist)")
            self._mock_mode = True

    async def close(self):
        if self._client:
            self._client.close()
            logger.info("MongoDB connection closed.")

    async def _create_indexes(self):
        if self._mock_mode:
            return
        try:
            await self._db.chats.create_index("chat_id", unique=True)
            await self._db.users.create_index("user_id", unique=True)
            await self._db.playlists.create_index([("owner_id", 1), ("name", 1)])
            await self._db.history.create_index([("chat_id", 1), ("played_at", -1)])
            await self._db.stats.create_index("chat_id", unique=True)
        except Exception as e:
            logger.warning(f"Failed to create indexes: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    #  Chat settings
    # ─────────────────────────────────────────────────────────────────────────

    async def get_chat(self, chat_id: int) -> dict:
        if self._mock_mode:
            if chat_id not in self._mock_data["chats"]:
                self._mock_data["chats"][chat_id] = {
                    "chat_id": chat_id,
                    "language": "en",
                    "volume": 100,
                    "loop": "off",
                    "admins_only": False,
                    "auto_clean": True,
                    "joined_at": datetime.now(timezone.utc),
                }
            return self._mock_data["chats"][chat_id]
        
        doc = await self._db.chats.find_one({"chat_id": chat_id})
        if not doc:
            doc = await self._upsert_chat(chat_id)
        return doc

    async def _upsert_chat(self, chat_id: int) -> dict:
        default = {
            "chat_id":       chat_id,
            "language":      "en",
            "volume":        100,
            "loop":          "off",
            "admins_only":   False,
            "auto_clean":    True,
            "joined_at":     datetime.now(timezone.utc),
        }
        if self._mock_mode:
            self._mock_data["chats"][chat_id] = default
            return default
        
        await self._db.chats.update_one(
            {"chat_id": chat_id}, {"$setOnInsert": default}, upsert=True
        )
        return default

    async def update_chat(self, chat_id: int, data: dict):
        if self._mock_mode:
            if chat_id in self._mock_data["chats"]:
                self._mock_data["chats"][chat_id].update(data)
            return
        
        await self._db.chats.update_one(
            {"chat_id": chat_id}, {"$set": data}, upsert=True
        )

    async def all_chat_ids(self) -> list[int]:
        if self._mock_mode:
            return list(self._mock_data["chats"].keys())
        
        cursor = self._db.chats.find({}, {"chat_id": 1})
        return [doc["chat_id"] async for doc in cursor]

    # ─────────────────────────────────────────────────────────────────────────
    #  User settings / stats
    # ─────────────────────────────────────────────────────────────────────────

    async def get_user(self, user_id: int) -> dict:
        if self._mock_mode:
            if user_id not in self._mock_data["users"]:
                self._mock_data["users"][user_id] = {
                    "user_id": user_id,
                    "language": "en",
                    "songs_played": 0,
                    "joined_at": datetime.now(timezone.utc),
                }
            return self._mock_data["users"][user_id]
        
        doc = await self._db.users.find_one({"user_id": user_id})
        if not doc:
            doc = {
                "user_id":    user_id,
                "language":   "en",
                "songs_played": 0,
                "joined_at":  datetime.now(timezone.utc),
            }
            await self._db.users.insert_one(doc)
        return doc

    async def increment_user_songs(self, user_id: int):
        if self._mock_mode:
            user = await self.get_user(user_id)
            user["songs_played"] = user.get("songs_played", 0) + 1
            return
        
        await self._db.users.update_one(
            {"user_id": user_id}, {"$inc": {"songs_played": 1}}, upsert=True
        )

    async def total_users(self) -> int:
        if self._mock_mode:
            return len(self._mock_data["users"])
        return await self._db.users.count_documents({})

    # ─────────────────────────────────────────────────────────────────────────
    #  Playlists
    # ─────────────────────────────────────────────────────────────────────────

    async def create_playlist(self, owner_id: int, name: str) -> bool:
        if self._mock_mode:
            key = (owner_id, name)
            for pl in self._mock_data["playlists"].values():
                if pl["owner_id"] == owner_id and pl["name"] == name:
                    return False
            self._mock_data["playlists"][key] = {
                "owner_id": owner_id,
                "name": name,
                "tracks": [],
                "created_at": datetime.now(timezone.utc),
            }
            return True
        
        existing = await self._db.playlists.find_one({"owner_id": owner_id, "name": name})
        if existing:
            return False
        await self._db.playlists.insert_one({
            "owner_id": owner_id,
            "name":     name,
            "tracks":   [],
            "created_at": datetime.now(timezone.utc),
        })
        return True

    async def add_to_playlist(self, owner_id: int, name: str, track: dict) -> bool:
        if self._mock_mode:
            key = (owner_id, name)
            if key in self._mock_data["playlists"]:
                self._mock_data["playlists"][key]["tracks"].append(track)
                return True
            return False
        
        result = await self._db.playlists.update_one(
            {"owner_id": owner_id, "name": name},
            {"$push": {"tracks": track}},
        )
        return result.modified_count > 0

    async def get_playlist(self, owner_id: int, name: str) -> Optional[dict]:
        if self._mock_mode:
            key = (owner_id, name)
            return self._mock_data["playlists"].get(key)
        
        return await self._db.playlists.find_one({"owner_id": owner_id, "name": name})

    async def list_playlists(self, owner_id: int) -> list[dict]:
        if self._mock_mode:
            return [pl for pl in self._mock_data["playlists"].values() if pl["owner_id"] == owner_id]
        
        cursor = self._db.playlists.find({"owner_id": owner_id}, {"name": 1, "tracks": 1})
        return [doc async for doc in cursor]

    async def delete_playlist(self, owner_id: int, name: str) -> bool:
        if self._mock_mode:
            key = (owner_id, name)
            if key in self._mock_data["playlists"]:
                del self._mock_data["playlists"][key]
                return True
            return False
        
        result = await self._db.playlists.delete_one({"owner_id": owner_id, "name": name})
        return result.deleted_count > 0

    # ─────────────────────────────────────────────────────────────────────────
    #  Play history
    # ─────────────────────────────────────────────────────────────────────────

    async def log_play(self, chat_id: int, track_info: dict):
        if self._mock_mode:
            self._mock_data["history"].append({
                "chat_id": chat_id,
                "track": track_info,
                "played_at": datetime.now(timezone.utc),
            })
            return
        
        await self._db.history.insert_one({
            "chat_id":   chat_id,
            "track":     track_info,
            "played_at": datetime.now(timezone.utc),
        })

    async def recent_history(self, chat_id: int, limit: int = 10) -> list[dict]:
        if self._mock_mode:
            matching = [h for h in self._mock_data["history"] if h["chat_id"] == chat_id]
            return sorted(matching, key=lambda x: x["played_at"], reverse=True)[:limit]
        
        cursor = self._db.history.find({"chat_id": chat_id}).sort("played_at", -1).limit(limit)
        return [doc async for doc in cursor]

    # ─────────────────────────────────────────────────────────────────────────
    #  Stats (for dashboard API)
    # ─────────────────────────────────────────────────────────────────────────

    async def increment_stat(self, chat_id: int, field: str):
        if self._mock_mode:
            if chat_id not in self._mock_data["stats"]:
                self._mock_data["stats"][chat_id] = {}
            self._mock_data["stats"][chat_id][field] = self._mock_data["stats"][chat_id].get(field, 0) + 1
            return
        
        await self._db.stats.update_one(
            {"chat_id": chat_id}, {"$inc": {field: 1}}, upsert=True
        )

    async def get_stats(self, chat_id: int) -> dict:
        if self._mock_mode:
            return self._mock_data["stats"].get(chat_id, {})
        
        doc = await self._db.stats.find_one({"chat_id": chat_id}) or {}
        doc.pop("_id", None)
        return doc

    async def global_stats(self) -> dict:
        if self._mock_mode:
            total_chats = len(self._mock_data["chats"])
            total_plays = sum(len(h) for h in [self._mock_data["history"]])
            return {"total_plays": total_plays, "total_chats": total_chats}
        
        pipeline = [
            {"$group": {
                "_id":            None,
                "total_plays":    {"$sum": "$tracks_queued"},
                "total_chats":    {"$sum": 1},
            }}
        ]
        result = await self._db.stats.aggregate(pipeline).to_list(1)
        return result[0] if result else {"total_plays": 0, "total_chats": 0}

    # ─────────────────────────────────────────────────────────────────────────
    #  yt-dlp OAuth Cache Backup/Restore
    # ─────────────────────────────────────────────────────────────────────────

    async def save_yt_dlp_cache(self, cache_tar_bytes: bytes):
        if self._mock_mode:
            self._mock_data["stats"]["yt_dlp_cache"] = cache_tar_bytes
            return
        await self._db.stats.update_one(
            {"chat_id": 999999999}, # Special system doc
            {"$set": {"yt_dlp_cache": cache_tar_bytes, "updated_at": datetime.now(timezone.utc)}},
            upsert=True
        )

    async def load_yt_dlp_cache(self) -> Optional[bytes]:
        if self._mock_mode:
            return self._mock_data["stats"].get("yt_dlp_cache")
        doc = await self._db.stats.find_one({"chat_id": 999999999})
        if doc:
            return doc.get("yt_dlp_cache")
        return None

    # ─────────────────────────────────────────────────────────────────────────
    #  Advertisement Promotions Management
    # ─────────────────────────────────────────────────────────────────────────

    async def save_ad(self, ad_id: str, text: str, button_text: str = "", button_url: str = "", active: bool = True):
        doc = {
            "ad_id": ad_id,
            "text": text,
            "button_text": button_text,
            "button_url": button_url,
            "active": active,
            "updated_at": datetime.now(timezone.utc)
        }
        if self._mock_mode:
            if "ads" not in self._mock_data:
                self._mock_data["ads"] = {}
            self._mock_data["ads"][ad_id] = doc
            return
        await self._db.ads.update_one({"ad_id": ad_id}, {"$set": doc}, upsert=True)

    async def get_active_ads(self) -> list[dict]:
        if self._mock_mode:
            if "ads" not in self._mock_data:
                return []
            return [ad for ad in self._mock_data["ads"].values() if ad.get("active")]
        cursor = self._db.ads.find({"active": True})
        return [doc async for doc in cursor]

    async def list_all_ads(self) -> list[dict]:
        if self._mock_mode:
            if "ads" not in self._mock_data:
                return []
            return list(self._mock_data["ads"].values())
        cursor = self._db.ads.find({})
        return [doc async for doc in cursor]

    async def delete_ad(self, ad_id: str) -> bool:
        if self._mock_mode:
            if "ads" in self._mock_data and ad_id in self._mock_data["ads"]:
                del self._mock_data["ads"][ad_id]
                return True
            return False
        res = await self._db.ads.delete_one({"ad_id": ad_id})
        return res.deleted_count > 0

    # ─────────────────────────────────────────────────────────────────────────
    #  Group user promotions / demotions
    # ─────────────────────────────────────────────────────────────────────────

    async def promote_user(self, chat_id: int, user_id: int):
        if self._mock_mode:
            chat = await self.get_chat(chat_id)
            if "promoted_users" not in chat:
                chat["promoted_users"] = []
            if user_id not in chat["promoted_users"]:
                chat["promoted_users"].append(user_id)
            return
        await self._db.chats.update_one(
            {"chat_id": chat_id},
            {"$addToSet": {"promoted_users": user_id}},
            upsert=True
        )

    async def demote_user(self, chat_id: int, user_id: int):
        if self._mock_mode:
            chat = await self.get_chat(chat_id)
            if "promoted_users" in chat and user_id in chat["promoted_users"]:
                chat["promoted_users"].remove(user_id)
            return
        await self._db.chats.update_one(
            {"chat_id": chat_id},
            {"$pull": {"promoted_users": user_id}}
        )

    async def is_user_promoted(self, chat_id: int, user_id: int) -> bool:
        chat = await self.get_chat(chat_id)
        promoted = chat.get("promoted_users", [])
        return user_id in promoted



