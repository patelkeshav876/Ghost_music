<<<<<<< HEAD
# Ghost_music
music player for telegram
=======
# 🎵 GhostMusic Bot

> Production-ready Telegram music streaming bot.
> Plays music in voice chats, supports queues, Spotify, playlists, lyrics, and more.

---

## ✨ Features

| Category | Features |
|---|---|
| **Playback** | Play by name, YouTube URL, Spotify URL, Telegram audio file |
| **Queue** | Add, view, shuffle, clear queue. Max 50 tracks per chat |
| **Controls** | Skip, pause, resume, stop, volume (1–200%) |
| **Loop** | Off / Song / Queue cycle |
| **Playlists** | Create, add, play, delete personal playlists (stored in MongoDB) |
| **Search** | `/search` shows 5 results as tappable buttons |
| **Inline Mode** | Search from any chat via `@BotUsername song name` |
| **Lyrics** | `/lyrics` via Genius API |
| **History** | `/history` — last 10 played tracks per chat |
| **AI** | GPT-powered Auto-DJ (optional) |
| **Admin** | `/broadcast`, `/stats`, `/adminsonly` |
| **Dashboard** | Web dashboard with live stream monitoring |
| **Auto-leave** | Bot leaves voice chat after configurable idle period |
| **Rate limit** | Per-user command rate limiting |

---

## 🏗️ Architecture

```
ghostmusic/
├── __main__.py          ← Entry point
├── config/
│   └── settings.py      ← All env vars, validated at startup
├── core/
│   └── bot.py           ← Composition root (Pyrogram + PyTgCalls)
├── streaming/
│   └── engine.py        ← Queue management, playback state, PyTgCalls bridge
├── database/
│   └── mongo.py         ← All MongoDB operations (motor async)
├── services/
│   ├── resolver.py      ← yt-dlp audio URL resolver (YouTube + Spotify)
│   ├── lyrics.py        ← Genius API lyrics fetcher
│   └── stats_api.py     ← aiohttp HTTP API for the web dashboard
├── handlers/
│   ├── music.py         ← All music commands (/play, /skip, /queue, etc.)
│   ├── search.py        ← /search with inline result buttons
│   ├── history.py       ← /history command
│   ├── admin.py         ← Admin-only commands
│   └── inline.py        ← @BotUsername inline search mode
├── utils/
│   ├── decorators.py    ← rate_limit, sudo_only, admin_only
│   ├── ui.py            ← Now-playing card text + inline keyboards
│   ├── thumbnail.py     ← Pillow now-playing card image generator
│   ├── helpers.py       ← format_duration, is_url, etc.
│   └── logger.py        ← Centralised logging setup
├── dashboard.html        ← Web dashboard (drop into your website)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## 🚀 Quick Start

### 1. Prerequisites

```bash
# System requirements
sudo apt-get update
sudo apt-get install -y ffmpeg git python3.11 python3.11-pip

# Verify
ffmpeg -version
python3.11 --version
```

### 2. Clone & Install

```bash
git clone https://github.com/yourusername/ghostmusic-bot
cd ghostmusic-bot
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
nano .env   # fill in all required values
```

**Required values:**
| Variable | How to get |
|---|---|
| `API_ID` + `API_HASH` | https://my.telegram.org/apps |
| `BOT_TOKEN` | @BotFather on Telegram |
| `SESSION_STRING` | See below |
| `MONGO_URI` | MongoDB Atlas (free) or self-hosted |
| `OWNER_ID` | Your Telegram user ID from @userinfobot |

### 4. Generate Session String

The bot needs a **second Telegram account** (the "assistant") to join voice chats.
This is required by Telegram's API — bots alone cannot stream audio.

```bash
python3 -c "
from pyrogram import Client
import asyncio

async def gen():
    async with Client(':memory:', api_id=YOUR_API_ID, api_hash='YOUR_API_HASH') as c:
        print(await c.export_session_string())

asyncio.run(gen())
"
```

Copy the printed string into `SESSION_STRING` in your `.env`.

### 5. Run

```bash
# Direct
python -m ghostmusic

# With PM2 (recommended for VPS)
pm2 start "python -m ghostmusic" --name ghostmusic
pm2 save
pm2 startup
```

### 6. Docker (easiest for production)

```bash
cp .env.example .env
# fill in .env ...

docker-compose up -d
docker-compose logs -f ghostmusic
```

---

## 🤖 Bot Setup in BotFather

1. `/mybots` → select your bot → **Edit Bot**
2. **Edit Commands** → paste:

```
play - Play a song or YouTube/Spotify link
search - Search YouTube for a song
skip - Skip current track
pause - Pause playback
resume - Resume playback
stop - Stop and leave voice chat
queue - View the queue
nowplaying - Show current track
volume - Set volume (1-200)
loop - Toggle loop mode
shuffle - Shuffle the queue
clearqueue - Clear the queue
playlist - Manage your playlists
lyrics - Get song lyrics
history - Recently played tracks
help - Show all commands
```

3. **Edit Inline Placeholder** → `Search for music…`
4. **Turn Inline Mode ON**

---

## 📊 Dashboard Setup

1. Copy `dashboard.html` to your website (or deploy alongside `ghosttalk-website.html`)
2. Open it in browser
3. Go to **Settings** tab
4. Enter your server URL: `http://your-server-ip:8080`
5. Enter your `STATS_SECRET` value
6. Click **Save & Test**

---

## ☁️ Deployment Options

### Option A — Railway (easiest, free tier)
1. Push to GitHub
2. railway.app → New Project → Deploy from GitHub
3. Add all env vars
4. Deploy

### Option B — VPS with Docker
```bash
# On Ubuntu 22.04 VPS
apt install docker.io docker-compose -y
git clone your-repo && cd ghostmusic-bot
cp .env.example .env && nano .env
docker-compose up -d
```

### Option C — PM2 on VPS
```bash
npm install -g pm2
pm2 start "python -m ghostmusic" --name ghostmusic
pm2 save && pm2 startup
```

---

## ⚠️ Important Notes

- **Two accounts needed**: one bot account (BOT_TOKEN) + one user account (SESSION_STRING)
- **The assistant account must be added** to the group as a member
- **Bot must be an admin** in the group to manage voice chats
- **FFmpeg must be installed** on the server — it's not a pip package
- **MongoDB Atlas free tier** (512MB) is enough for small-medium deployments

---

## 📜 Commands Reference

| Command | Description |
|---|---|
| `/play <name/URL>` | Play or queue a track |
| `/search <name>` | Search with tappable results |
| `/skip` | Skip current track |
| `/pause` | Pause |
| `/resume` | Resume |
| `/stop` | Stop + leave voice chat |
| `/queue` | View queue |
| `/nowplaying` | Now playing card |
| `/volume 1-200` | Set volume |
| `/loop` | Cycle loop mode |
| `/shuffle` | Shuffle queue |
| `/clearqueue` | Clear queue |
| `/playlist create <n>` | Create playlist |
| `/playlist add <n>` | Add current to playlist |
| `/playlist play <n>` | Play playlist |
| `/playlist delete <n>` | Delete playlist |
| `/playlist` | List playlists |
| `/lyrics [song]` | Get lyrics |
| `/history` | Recent plays |
| `/help` | Help message |
| `/broadcast <msg>` | *(sudo)* Send to all chats |
| `/stats` | *(sudo)* Global stats |

---

## 🔒 Security

- All secrets live in `.env` — never hardcoded
- Stats API requires `X-Secret` header for write operations
- Admin commands require Telegram group admin status or sudo user ID
- Rate limiting: 5 commands per 10 seconds per user

---

## 📦 Tech Stack

| Layer | Technology |
|---|---|
| Telegram client | Pyrogram 2.0.106 |
| Voice streaming | py-tgcalls 1.0.0 |
| Audio extraction | yt-dlp (latest) |
| Database | MongoDB (motor async) |
| HTTP API | aiohttp |
| Image generation | Pillow |
| Runtime | Python 3.11+ |
| Container | Docker |
>>>>>>> bfc7178 (first commit)
