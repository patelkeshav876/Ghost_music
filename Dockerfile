# ══════════════════════════════════════════════════════════
#  GhostMusic Bot — Production Dockerfile
#  Multi-stage build for minimal image size.
# ══════════════════════════════════════════════════════════

FROM python:3.11-slim AS base

# System deps: ffmpeg is required for audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    build-essential \
    python3-dev \
    libffi-dev \
    libssl-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps — order matters to avoid resolver conflicts
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir pyrogram==2.0.106 TgCrypto==1.2.5
RUN pip install --no-cache-dir ntgcalls==1.1.3
RUN pip install --no-cache-dir py-tgcalls==1.1.6
RUN pip install --no-cache-dir yt-dlp youtube-search-python motor pymongo aiohttp python-dotenv spotipy Pillow

# Copy source
COPY . .

# Create non-root user for security
RUN useradd -m -u 1000 ghostmusic \
 && chown -R ghostmusic:ghostmusic /app
USER ghostmusic

# Health check via stats API
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${STATS_PORT:-8080}/api/health || exit 1

EXPOSE ${STATS_PORT:-8080}

CMD ["python", "__main__.py"]
