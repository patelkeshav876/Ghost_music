#!/usr/bin/env python3
import os
from pathlib import Path
from dotenv import load_dotenv

env_file = Path(".env")
print(f"File exists: {env_file.exists()}")
print(f"File size: {env_file.stat().st_size if env_file.exists() else 0}")

# Read raw content
if env_file.exists():
    raw = env_file.read_text()
    lines = raw.split('\n')
    print(f"Total lines: {len(lines)}")
    for i, line in enumerate(lines[:15]):
        print(f"Line {i}: {repr(line)}")

# Try loading
print("\n--- Loading with dotenv ---")
result = load_dotenv(env_file, override=True)
print(f"Load result: {result}")

# Check if API_ID is set
api_id = os.getenv("API_ID")
print(f"API_ID value: {repr(api_id)}")
