"""
sender.py — Re-upload cleaned media through the *bot* client.

Takes a MediaInfo record produced by downloader.py and sends the freshly
downloaded (now unrestricted) file to the target chat, preserving as much
of the original metadata as possible: caption, file name, mime type,
duration and thumbnail.

Robustness:
    • FloodWait during upload → sleep the required time, retry once.
    • Upload stall watchdog → if no progress happens for
      UPLOAD_STALL_TIMEOUT seconds, the upload task is cancelled and
      TimeoutError is raised (pyrogram send_* has NO built-in timeout:
      a wedged connection would otherwise hang the worker forever).
    • Temp files are deleted in a finally-block on every outcome.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable

from pyrogram import Client
from pyrogram.errors import FloodWait

from downloader import MediaInfo
from config import UPLOAD_STALL_TIMEOUT

log = logging.getLogger(__name__)

# Type alias for the async progress callback: (current, total) -> None
ProgressCallback = Callable[[int, int], Awaitable[None]]

# How often (seconds) the upload watchdog checks liveness.
_UPLOAD_WATCHDOG_INTERVAL = 15.0


async def send_media(
    bot: Client,
    chat_id: int | str,
    info: MediaInfo,
    progress: ProgressCallback | None = None,
) -> Any:
    """
    Send `info.path` to `chat_id` via the bot client, choosing the right
    Pyrogram send_* method from `info.kind`.

    FloodWait during upload: sleep the required time and retry once.
    """
    try:
        for attempt in (1, 2):
            try:
                return await _guarded_dispatch(bot, chat_id, info, progress)
            except FloodWait as e:
                if attempt == 2:
                    raise
                log.warning("FloodWait %ss during upload — sleeping, retrying.", e.value)
                await asyncio.sleep(e.value + 1)
        raise RuntimeError("unreachable")
    finally:
        # ALWAYS remove the staged file: success, failure or exception.
        _cleanup(info)


async def _guarded_dispatch(
    bot: Client,
    chat_id: int | str,
    info: MediaInfo,
    progress: ProgressCallback | None,
) -> Any:
    """
    Run the actual upload as a supervised task.

    A poll loop awaits the task in short slices; if no progress tick has
    arrived for UPLOAD_STALL_TIMEOUT seconds, the task is cancelled and
    TimeoutError raised — turning a potential infinite hang into a
    clean, reportable failure.
    """
    last_tick = time.monotonic()

    async def tracked(current: int, total: int) -> None:
        """Progress wrapper: record liveness, then forward to the caller."""
        nonlocal last_tick
        last_tick = time.monotonic()
        if progress:
            await progress(current, total)

    async def _upload() -> Any:
        return await _dispatch(bot, chat_id, info, tracked)

    task = asyncio.create_task(_upload())
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_UPLOAD_WATCHDOG_INTERVAL)
            if done:
                return task.result()  # re-raises upload errors (FloodWait etc.)
            if time.monotonic() - last_tick > UPLOAD_STALL_TIMEOUT:
                log.warning("Upload stalled >%ss — cancelling.", UPLOAD_STALL_TIMEOUT)
                task.cancel()
                raise TimeoutError(
                    f"Upload stalled for more than {UPLOAD_STALL_TIMEOUT}s."
                )
    finally:
        task.cancel()  # no-op when already done
        try:
            await task
        except BaseException:  # noqa: BLE001 — cleanup must not mask the outcome
            pass


async def _dispatch(
    bot: Client,
    chat_id: int | str,
    info: MediaInfo,
    progress: ProgressCallback | None,
) -> Any:
    """Call the send_* method matching `info.kind` with valid kwargs only."""
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "caption": info.caption,
    }
    if progress is not None:
        kwargs["progress"] = progress

    if info.kind == "photo":
        return await bot.send_photo(**kwargs, photo=info.path)

    if info.kind == "video":
        return await bot.send_video(
            **kwargs,
            video=info.path,
            duration=info.duration,
            width=info.width,
            height=info.height,
            thumb=info.thumb_path,
            supports_streaming=True,
        )

    if info.kind == "audio":
        return await bot.send_audio(
            **kwargs,
            audio=info.path,
            duration=info.duration,
            thumb=info.thumb_path,
        )

    if info.kind == "animation":
        return await bot.send_animation(
            **kwargs,
            animation=info.path,
            duration=info.duration,
            width=info.width,
            height=info.height,
            thumb=info.thumb_path,
        )

    if info.kind == "voice":
        # send_voice supports caption/duration but not thumb.
        return await bot.send_voice(
            chat_id=chat_id,
            voice=info.path,
            caption=info.caption or "",
            duration=info.duration,
        )

    if info.kind == "video_note":
        # send_video_note accepts neither caption nor progress.
        return await bot.send_video_note(
            chat_id=chat_id,
            video_note=info.path,
            duration=info.duration,
            length=info.width or 1,
        )

    if info.kind == "sticker":
        # send_sticker only takes the sticker itself — no caption/thumb.
        return await bot.send_sticker(chat_id=chat_id, sticker=info.path)

    # document & anything unrecognised — keep original bytes and file name
    return await bot.send_document(
        **kwargs,
        document=info.path,
        file_name=info.file_name,
        thumb=info.thumb_path,
        force_document=True,
    )


async def send_text(bot: Client, chat_id: int | str, text: str) -> Any:
    """Forward a plain-text message body through the bot client."""
    return await bot.send_message(chat_id, text)


def _cleanup(info: MediaInfo) -> None:
    """Delete the staged temp file (and thumb) to conserve disk space."""
    for path in (info.path, info.thumb_path):
        if path and os.path.exists(path):
            try:
                os.remove(path)
                log.debug("Removed temp file %s", path)
            except OSError as exc:
                log.warning("Could not remove %s: %s", path, exc)
