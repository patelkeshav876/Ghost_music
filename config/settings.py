"""
config/settings.py
Centralised, validated configuration loaded from environment variables.
All secrets live in .env — never hardcoded.
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

# Load .env from the project root
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path, override=True)


def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise EnvironmentError(f"Required env var '{key}' is missing or empty.")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


@dataclass(frozen=True)
class Settings:
    # ── Telegram credentials ──────────────────────────────────────────────────
    API_ID:             int    = field(default_factory=lambda: int(_require("API_ID")))
    API_HASH:           str    = field(default_factory=lambda: _require("API_HASH"))
    BOT_TOKEN:          str    = field(default_factory=lambda: _require("BOT_TOKEN"))
    # Session string for the userbot / assistant account (required for VC)
    SESSION_STRING:     str    = field(default_factory=lambda: _require("SESSION_STRING"))

    # ── Database ──────────────────────────────────────────────────────────────
    # MongoDB URI: mongodb+srv://user:pass@cluster.mongodb.net/ghostmusic
    MONGO_URI:          str    = field(default_factory=lambda: _require("MONGO_URI"))
    DB_NAME:            str    = field(default_factory=lambda: _optional("DB_NAME", "ghostmusic"))

    # ── Optional integrations ─────────────────────────────────────────────────
    SPOTIFY_CLIENT_ID:  str    = field(default_factory=lambda: _optional("SPOTIFY_CLIENT_ID"))
    SPOTIFY_SECRET:     str    = field(default_factory=lambda: _optional("SPOTIFY_SECRET"))
    GENIUS_TOKEN:       str    = field(default_factory=lambda: _optional("GENIUS_TOKEN"))   # lyrics
    LASTFM_KEY:         str    = field(default_factory=lambda: _optional("LASTFM_KEY"))
    YOUTUBE_COOKIES:    str    = field(default_factory=lambda: _optional("YOUTUBE_COOKIES"))


    # ── Bot behaviour ─────────────────────────────────────────────────────────
    MAX_QUEUE_SIZE:     int    = field(default_factory=lambda: _int("MAX_QUEUE_SIZE", 50))
    MAX_PLAYLIST_SIZE:  int    = field(default_factory=lambda: _int("MAX_PLAYLIST_SIZE", 100))
    DEFAULT_VOLUME:     int    = field(default_factory=lambda: _int("DEFAULT_VOLUME", 100))
    STREAM_QUALITY:     str    = field(default_factory=lambda: _optional("STREAM_QUALITY", "high"))
    AUTO_LEAVE_DELAY:   int    = field(default_factory=lambda: _int("AUTO_LEAVE_DELAY", 60))   # secs idle before leaving VC
    AUTO_CLEAN_MSGS:    bool   = field(default_factory=lambda: _bool("AUTO_CLEAN_MSGS", True))
    COMMAND_PREFIX:     str    = field(default_factory=lambda: _optional("COMMAND_PREFIX", "/"))

    # ── Admin / sudo ──────────────────────────────────────────────────────────
    # Comma-separated Telegram user IDs with global sudo access
    SUDO_USERS:         list   = field(default_factory=lambda:
                                    [int(i) for i in _optional("SUDO_USERS", "").split(",") if i.strip().isdigit()])
    OWNER_ID:           int    = field(default_factory=lambda: int(_require("OWNER_ID")))

    # ── Rate limiting ─────────────────────────────────────────────────────────
    RATE_LIMIT_CMDS:    int    = field(default_factory=lambda: _int("RATE_LIMIT_CMDS", 5))   # cmds per window
    RATE_LIMIT_WINDOW:  int    = field(default_factory=lambda: _int("RATE_LIMIT_WINDOW", 10)) # seconds

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL:          str    = field(default_factory=lambda: _optional("LOG_LEVEL", "INFO"))
    # Allow empty or missing LOG_CHANNEL without raising ValueError
    LOG_CHANNEL:        Optional[int] = field(default_factory=lambda:
                                    (int(_optional("LOG_CHANNEL").strip())
                                     if _optional("LOG_CHANNEL").strip() else None))

    # ── Stats API (for the web dashboard) ────────────────────────────────────
    STATS_PORT:         int    = field(default_factory=lambda: _int("STATS_PORT", 8080))
    STATS_SECRET:       str    = field(default_factory=lambda: _optional("STATS_SECRET", "changeme"))


# Singleton — import this everywhere
cfg = Settings()
