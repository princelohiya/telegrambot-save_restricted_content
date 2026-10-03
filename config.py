"""
config.py — Central configuration loader.

Reads runtime settings from environment variables (optionally provided via a
local `.env` file, thanks to python-dotenv) and exposes them as module-level
constants that the rest of the application imports.

Required environment variables:
    API_ID           int    - Telegram API ID   (my.telegram.org -> API development tools)
    API_HASH         str    - Telegram API hash (same page as API_ID)
    BOT_TOKEN        str    - Bot token issued by @BotFather
    SESSION_STRING   str    - Pyrogram session string of the *user* account

Optional:
    TEMP_DOWNLOAD_DIR       - Where media is staged before re-upload (default: ./downloads)
"""

import os
import sys

from dotenv import load_dotenv

# Load variables from a `.env` file sitting next to this project (if present).
# Environment variables already set in the shell take precedence over .env.
load_dotenv()

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _require(name: str) -> str:
    """Fetch a mandatory env var or abort with a readable error message."""
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(
            f"[config] Missing required environment variable: {name}\n"
            f"         Add it to your `.env` file or export it in your shell."
        )
    return value


def _require_int(name: str) -> int:
    """Fetch a mandatory integer env var or abort with a readable error."""
    raw = _require(name)
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"[config] Environment variable {name} must be an integer, got: {raw!r}")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

# Telegram API credentials (from https://my.telegram.org -> API development tools)
API_ID: int = _require_int("API_ID")
API_HASH: str = _require("API_HASH")

# Bot client credentials (from @BotFather)
BOT_TOKEN: str = _require("BOT_TOKEN")

# User client credentials — an exported Pyrogram session string (MTProto login).
# This is what lets us access restricted/private chats the bot account cannot.
SESSION_STRING: str = _require("SESSION_STRING")

# Directory where media is temporarily downloaded before re-upload.
TEMP_DOWNLOAD_DIR: str = os.getenv("TEMP_DOWNLOAD_DIR", "./downloads").strip() or "./downloads"

# Ensure the download directory exists at import time.
os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)

# --------------------------------------------------------------------------- #
# Optional tuning knobs (sane defaults for production use)
# --------------------------------------------------------------------------- #

# How many messages may sit in the extraction queue before new links are rejected.
QUEUE_MAX_SIZE: int = int(os.getenv("QUEUE_MAX_SIZE", "100"))

# Seconds of download inactivity after which a stalled transfer is aborted.
DOWNLOAD_TIMEOUT: int = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))

# Parallel download connections (each streams 1 MiB chunks from Telegram).
# 4-8 gives a near-linear speedup on typical home links; too high may trip
# network/FloodWait limits. Must be <= max_concurrent_transmissions of the user client.
DOWNLOAD_WORKERS: int = int(os.getenv("DOWNLOAD_WORKERS", "6"))

# Chunks (1 MiB each) per parallel segment: 16 => every connection grabs a
# 16 MiB slice before moving to the next. Bigger = fewer session handshakes,
# slightly laggier progress.
DOWNLOAD_PART_BATCH: int = int(os.getenv("DOWNLOAD_PART_BATCH", "16"))

# Seconds of upload inactivity after which a stalled upload is aborted.
# (pyrogram send_* has no built-in timeout — a wedged upload would hang forever.)
UPLOAD_STALL_TIMEOUT: int = int(os.getenv("UPLOAD_STALL_TIMEOUT", "120"))

# Maximum media size we are willing to stage on disk (bytes). 2 GiB default.
MAX_FILE_SIZE: int = int(os.getenv("MAX_FILE_SIZE", str(2 * 1024 * 1024 * 1024)))
