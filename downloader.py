"""
downloader.py — Media acquisition via the *user* client.

Responsibilities:
    • Fetch the original message through the user account (MTProto), which
      can read restricted ("protect content") chats and private channels
      the user account has joined — places a bot token cannot reach.
    • Download media to TEMP_DOWNLOAD_DIR with progress reporting and a
      stall watchdog that actively cancels frozen transfers.
    • Extract the metadata (caption, mime type, duration, thumbnail, file
      name) the sender module needs to reproduce the post faithfully.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pyrogram import Client
from pyrogram.errors import FloodWait, MessageIdsEmpty

from config import DOWNLOAD_TIMEOUT, MAX_FILE_SIZE, TEMP_DOWNLOAD_DIR

log = logging.getLogger(__name__)

# Type alias for the async progress callback: (current, total) -> None
ProgressCallback = Callable[[int, int], Awaitable[None]]

# How often (seconds) the stall watchdog checks liveness.
_WATCHDOG_INTERVAL = 10.0


@dataclass
class MediaInfo:
    """Everything the sender needs to re-upload a piece of media."""

    path: str                       # local file path after download
    kind: str                       # photo | video | document | audio | voice | video_note | animation | sticker
    mime_type: str | None = None
    caption: str | None = None
    file_name: str | None = None
    duration: int | None = None     # seconds (audio/video)
    width: int | None = None
    height: int | None = None
    thumb_path: str | None = None   # downloaded thumbnail (may be None)


def get_media(message: Any) -> Any:
    """
    Return the media object attached to a message, or None for pure text.

    Works across photo, video, document, audio, voice, video_note,
    animation and sticker message types.
    """
    for attr in (
        "photo", "video", "document", "audio",
        "voice", "video_note", "animation", "sticker",
    ):
        obj = getattr(message, attr, None)
        if obj is not None:
            return obj
    return None


async def fetch_message(user: Client, chat_id: str | int, message_id: int):
    """
    Fetch one message with the user client, honouring FloodWait (one retry).

    Errors such as ChannelPrivate / ChatAdminRequired are intentionally NOT
    swallowed: the caller turns them into user-friendly feedback.
    """
    try:
        messages = await user.get_messages(chat_id, message_id)
        message = messages[0] if isinstance(messages, list) else messages
        if message is None or getattr(message, "empty", False):
            raise MessageIdsEmpty
        return message
    except FloodWait as e:
        log.warning("FloodWait %ss while fetching %s#%s", e.value, chat_id, message_id)
        await asyncio.sleep(e.value + 1)
        return await fetch_message(user, chat_id, message_id)


async def download_media(
    user: Client,
    message: Any,
    progress: ProgressCallback | None = None,
) -> MediaInfo:
    """
    Download `message`'s media with the user client and describe it.

    Robustness:
        • FloodWait → sleep the required time, retry once.
        • Stall watchdog → if no bytes arrive for DOWNLOAD_TIMEOUT seconds,
          the download task is cancelled and TimeoutError is raised.
    """
    media = get_media(message)
    if media is None:
        raise ValueError("Message has no downloadable media.")

    # ---- refuse absurdly large files before wasting disk space ---------- #
    file_size = getattr(media, "file_size", None)
    if file_size and file_size > MAX_FILE_SIZE:
        raise ValueError(f"File too large: {file_size / 1024**3:.2f} GiB limit reached.")

    # Unique staging file name inside the temp dir.
    # A uuid token guarantees two concurrent extractions (even of the exact
    # same file) never collide on disk — pyrofork stages as "<name>.temp" and
    # os.rename() refuses to overwrite on Windows, which would fail the job.
    base_name = (
        getattr(media, "file_name", None)
        or f"tg_{message.id}_{getattr(media, 'file_unique_id', 'media')}"
    )
    safe_name = os.path.basename(str(base_name)).replace(os.sep, "_") or "media.bin"
    stem, ext = os.path.splitext(safe_name)
    target_path = os.path.join(TEMP_DOWNLOAD_DIR, f"{stem}.{uuid.uuid4().hex[:8]}{ext}")

    # ---- progress tracking / liveness state ----------------------------- #
    state = {"tick": time.monotonic()}

    async def _track(current: int, total: int) -> None:
        """Pyrogram progress hook — record liveness and forward to caller."""
        state["tick"] = time.monotonic()
        if progress:
            await progress(current, total)

    async def _download_once() -> str:
        return await user.download_media(
            message,
            file_name=target_path,
            progress=_track,
        )

    async def _watchdog(task: asyncio.Task) -> None:
        """Cancel `task` if no progress happened for DOWNLOAD_TIMEOUT s."""
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            if time.monotonic() - state["tick"] > DOWNLOAD_TIMEOUT:
                log.warning("Download stalled >%ss — cancelling.", DOWNLOAD_TIMEOUT)
                task.cancel()
                return

    async def _guarded_download() -> str:
        """Run the download under watchdog supervision."""
        task = asyncio.create_task(_download_once())
        watchdog = asyncio.create_task(_watchdog(task))
        try:
            return await task
        except asyncio.CancelledError:
            # Distinguish "watchdog killed it" from "we were cancelled".
            if time.monotonic() - state["tick"] > DOWNLOAD_TIMEOUT:
                raise TimeoutError(
                    f"Download stalled for more than {DOWNLOAD_TIMEOUT}s."
                ) from None
            raise
        finally:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass

    # ---- run with a single FloodWait retry ------------------------------ #
    try:
        path = await _guarded_download()
    except FloodWait as e:
        log.warning("FloodWait %ss during download; retrying once.", e.value)
        await asyncio.sleep(e.value + 1)
        path = await _guarded_download()

    if not path or not os.path.exists(path):
        raise RuntimeError("Download finished but the file is missing on disk.")

    return _describe(message, media, path)


async def download_thumb(user: Client, message: Any) -> str | None:
    """Download the media thumbnail for use during re-upload (best effort)."""
    try:
        return await user.download_media(
            message,
            file_name=os.path.join(
                TEMP_DOWNLOAD_DIR, f"thumb_{getattr(message, 'id', 'x')}"
            ),
            thumb=True,
        )
    except Exception:  # noqa: BLE001 — thumbnails are strictly optional
        return None


def _describe(message: Any, media: Any, path: str) -> MediaInfo:
    """Build a MediaInfo record from the original message + media object."""
    caption = getattr(message, "caption", None) or getattr(message, "text", None)
    return MediaInfo(
        path=path,
        kind=_classify(media),
        mime_type=getattr(media, "mime_type", None),
        caption=caption or None,
        file_name=getattr(media, "file_name", None),
        duration=getattr(media, "duration", None),
        width=getattr(media, "width", None),
        height=getattr(media, "height", None),
    )


def _classify(media: Any) -> str:
    """Map a Pyrogram media object onto our internal 'kind' string."""
    name = type(media).__name__.lower()
    if "photo" in name:
        return "photo"
    if "videonote" in name:
        return "video_note"
    if "video" in name:
        return "video"
    if "voice" in name:
        return "voice"
    if "audio" in name:
        return "audio"
    if "animation" in name:
        return "animation"
    if "sticker" in name:
        return "sticker"
    return "document"
