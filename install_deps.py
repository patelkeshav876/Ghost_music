#!/usr/bin/env python3
import subprocess
import sys

packages = [
    "pyrogram==2.0.106",
    "TgCrypto==1.2.5",
    "py-tgcalls==1.0.9",
    "yt-dlp",
    "motor",
    "pymongo",
    "aiohttp",
    "python-dotenv",
    "spotipy",
    "Pillow"
]

for pkg in packages:
    print(f"Installing {pkg}...")
    subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"], check=False)

print("Installation complete!")
