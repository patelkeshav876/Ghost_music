"""
utils/logger.py
Centralised logging setup — structured, coloured, with optional file output.
"""

import logging
import sys
from config.settings import cfg


def setup_logger(name: str = "ghostmusic") -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger  # already configured

    level = getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Suppress noisy third-party loggers
    for noisy in ("pyrogram", "pytgcalls", "motor", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logger
