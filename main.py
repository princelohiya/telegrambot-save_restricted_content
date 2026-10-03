"""
main.py — Entry point: dual-client startup, handlers and the extraction queue.

Clients
    • `bot`  — BotFather token. Talks to the user: commands, link intake,
               progress updates, delivery of extracted content.
    • `user` — Exported session string. Performs the privileged MTProto work
               (get_messages / download_media) inside restricted and private
               chats where the bot account has no reach.

IMPORTANT (loop-safety):
    Pyrogram's Dispatcher captures the asyncio loop *at client construction
    time* and schedules handler registration on it. Clients must therefore be
    constructed INSIDE the running event loop (inside `main()`), never at
    module import time — otherwise handlers silently never register and the
    bot appears "up" while ignoring every message.

Flow
    user sends link(s) in chat
        -> text listener validates and enqueues them
        -> a single queue worker processes one link batch at a time
        -> media: user client downloads -> bot client re-uploads -> temp file removed
        -> text:  bot client re-sends the text directly
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from pyrogram import Client, compose, filters
from pyrogram.handlers import MessageHandler
from pyrogram.errors import (
    ChannelPrivate,
    ChatAdminRequired,
    FloodWait,
    MessageEmpty,
    MessageIdsEmpty,
    RPCError,
)
from pyrogram.types import Message

from config import (
    API_HASH,
    API_ID,
    BOT_TOKEN,
    DOWNLOAD_WORKERS,
    QUEUE_MAX_SIZE,
    SESSION_STRING,
)
from downloader import download_media, download_thumb, fetch_message, get_media
from parser import ParsedLink, extract_links, parse_link
from sender import send_media, send_text

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("main")

# --------------------------------------------------------------------------- #
# Clients — constructed inside main() (see loop-safety note in the docstring)
# --------------------------------------------------------------------------- #
bot: Client | None = None
user: Client | None = None

# --------------------------------------------------------------------------- #
# Extraction queue
# --------------------------------------------------------------------------- #


@dataclass
class Job:
    """One extraction job: a parsed link plus the chat to report back to."""

    link: ParsedLink
    chat_id: int
    reply_to: int


queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)


async def enqueue(job: Job) -> bool:
    """Put a job on the queue; report back instead of hanging when full."""
    try:
        queue.put_nowait(job)
        return True
    except asyncio.QueueFull:
        return False


# --------------------------------------------------------------------------- #
# Progress reporting
# --------------------------------------------------------------------------- #


def _make_progress(job: Job, label: str):
    """
    Build an async progress callback that edits one status message.

    Telegram-facing edits are throttled (min 4 s apart) to stay clear of
    message-edit flood limits; console logs are throttled separately
    (min 30 s) so long transfers remain observable in the terminal.
    """
    state = {"last_edit": 0.0, "last_log": 0.0}

    async def progress(current: int, total: int) -> None:
        now = time.monotonic()
        pct = (current / total * 100) if total else 0

        # Console: keep the operator informed (throttled).
        if now - state["last_log"] >= 30:
            state["last_log"] = now
            log.info(
                "%s: %.1f%% (%.0f/%.0f MiB)",
                label, pct, current / 1048576, (total or 0) / 1048576,
            )

        # Telegram: edit the status message (throttled, best-effort).
        # 15 s spacing keeps edit traffic far below flood limits — progress
        # spam earlier triggered multi-minute FloodWaits that stalled jobs.
        if now - state["last_edit"] < 15:
            return
        state["last_edit"] = now

        filled = int(pct // 10)
        bar = "█" * filled + "░" * (10 - filled)
        text = (
            f"{label}\n"
            f"{bar} {pct:.1f}%  "
            f"({current / 1024**2:.1f}/{(total or 0) / 1024**2:.1f} MiB)"
        )
        try:
            await bot.edit_message_text(job.chat_id, job.reply_to, text)
        except FloodWait as e:
            log.warning("Progress-edit FloodWait %ss — skipping update.", e.value)
        except RPCError:
            pass  # transient edit failures are not worth interrupting the transfer

    return progress


# --------------------------------------------------------------------------- #
# Core extraction
# --------------------------------------------------------------------------- #


async def _status(job: Job, text: str) -> None:
    """
    Edit the job's status message. Cosmetic only — NEVER raises.

    ``reply_to`` may point at a message that was deleted (or that the bot
    cannot edit), which raises MESSAGE_ID_INVALID. Losing a status update
    must never kill a job that may have spent minutes downloading, so all
    errors are swallowed here and logged at debug level.
    """
    try:
        await bot.edit_message_text(job.chat_id, job.reply_to, text)
    except Exception:  # noqa: BLE001 — status updates are best-effort
        log.debug("Status edit failed (chat=%s msg=%s)", job.chat_id, job.reply_to)


async def process_message(job: Job, message_id: int) -> None:
    """Extract a single message: text re-sent directly, media re-uploaded."""
    message = await fetch_message(user, job.link.chat_id, message_id)

    media = get_media(message)
    if media is None:
        # Pure text (or unsupported/empty) — mirror the text via the bot.
        text = message.text or message.caption
        if not text:
            raise MessageEmpty
        await send_text(bot, job.chat_id, text)
        return

    info = await download_media(
        user,
        message,
        progress=_make_progress(job, "⬇️ Downloading"),
    )

    # Thumbnails are optional; best-effort fetch for nicer re-uploads.
    info.thumb_path = await download_thumb(user, message)

    await _status(job, "⬆️ Uploading…")
    await send_media(
        bot,
        job.chat_id,
        info,
        progress=_make_progress(job, "⬆️ Uploading"),
    )
    # send_media removes the temp file(s) itself in its finally-block.


def _report(job: Job, text: str) -> None:
    """Best-effort status reply to the requester; never raises."""
    try:
        asyncio.get_running_loop().create_task(
            bot.send_message(job.chat_id, text, reply_to_message_id=job.reply_to)
        )
    except Exception:  # noqa: BLE001 — reporting must never break the worker
        log.error("Could not deliver status message: %s", text, exc_info=True)


async def worker() -> None:
    """
    Serial queue consumer: one job at a time keeps rate limits happy.

    The outer try/except guarantees this task NEVER dies silently — any
    unexpected failure is logged and the loop simply continues with the
    next job.
    """
    log.info("Queue worker running.")
    while True:
        job = await queue.get()
        try:
            started = time.monotonic()
            count = 0
            total = len(job.link.message_ids)
            log.info(
                "Job started: chat=%s ids=%s..%s (%d msg)",
                job.link.chat_id,
                job.link.message_ids[0],
                job.link.message_ids[-1],
                total,
            )

            for message_id in job.link.message_ids:
                try:
                    await process_message(job, message_id)
                    count += 1
                except FloodWait as e:
                    log.warning("FloodWait %ss — sleeping.", e.value)
                    await asyncio.sleep(e.value + 1)
                    await process_message(job, message_id)  # retry once
                    count += 1
                except MessageIdsEmpty:
                    _report(job, f"⚠️ Message #{message_id} not found (deleted or out of range).")
                except (ChannelPrivate, ChatAdminRequired) as e:
                    _report(
                        job,
                        f"🔒 {e.__class__.__name__}: the account cannot access this chat. "
                        f"Make sure the user account has joined {job.link.chat_id}.",
                    )
                    break  # nothing else in this batch will succeed either
                except Exception as e:  # noqa: BLE001 — isolate per-message failures
                    log.exception("Job %s#%s failed", job.link.chat_id, message_id)
                    _report(
                        job,
                        f"❌ Failed on #{message_id}: {e.__class__.__name__}: {e}",
                    )

            if job.link.is_range and count:
                _report(
                    job,
                    f"✅ Batch complete: {count}/{total} messages extracted "
                    f"in {time.monotonic() - started:.0f}s.",
                )
            log.info(
                "Job finished: %d/%d ok in %.0fs",
                count, total, time.monotonic() - started,
            )
        except asyncio.CancelledError:
            log.info("Worker cancelled.")
            return
        except Exception:  # noqa: BLE001 — the worker must survive anything
            log.exception("Worker loop error — continuing with next job.")
        finally:
            queue.task_done()


# --------------------------------------------------------------------------- #
# Helpers used by handlers
# --------------------------------------------------------------------------- #


async def _safe_reply(message: Message, text: str) -> None:
    """
    reply_text that works in private chats AND groups.

    ``quote=True`` can raise MESSAGE_ID_INVALID in some non-private chats,
    so we fall back to a plain reply when quoting is not possible.
    FloodWait is honoured (sleep) and the reply is then skipped — a lost
    bot reply must never crash an update handler (which would make
    Telegram redeliver the same update over and over).
    """
    try:
        await message.reply_text(text, quote=True)
    except FloodWait as e:
        log.warning("FloodWait %ss on reply — sleeping, then skipping.", e.value)
        await asyncio.sleep(e.value + 1)
    except RPCError:
        try:
            await message.reply_text(text)
        except FloodWait as e:
            log.warning("FloodWait %ss on reply — sleeping, then skipping.", e.value)
            await asyncio.sleep(e.value + 1)


# --------------------------------------------------------------------------- #
# Handler callbacks (plain functions — registered explicitly in main())
# --------------------------------------------------------------------------- #


async def cmd_start(_: Client, message: Message) -> None:
    """/start — welcome + usage (private chat only)."""
    await message.reply_text(
        "**👋 Content Extractor Bot**\n\n"
        "Send me any Telegram post link and I'll return its content, "
        "even from chats with *restrict saving content* enabled.\n\n"
        "**Supported links**\n"
        "• `https://t.me/channel/123` — public post\n"
        "• `https://t.me/c/1234567890/123` — private post\n"
        "• `https://t.me/c/1234567890/100-110` — a range (batch)\n\n"
        "**Commands**\n"
        "/start — this message\n"
        "/help — detailed help\n"
        "/batch `<link>` — extract a post (works in groups too)\n\n"
        "_Note: private chats require the user account to be a member._",
        quote=True,
    )


async def cmd_help(_: Client, message: Message) -> None:
    """/help — detailed usage notes (private chat only)."""
    await message.reply_text(
        "**ℹ️ How this works**\n\n"
        "1. You send a post link.\n"
        "2. My *user session* fetches the raw message via MTProto "
        "(bots cannot read protected chats — user accounts can).\n"
        "3. Media is re-uploaded by the *bot* as a clean, unrestricted file.\n\n"
        "**Limits**\n"
        "• Files up to 2 GiB\n"
        "• Ranges capped at 200 messages\n"
        "• Jobs are processed one at a time (FloodWait-safe)",
        quote=True,
    )


async def cmd_batch(_: Client, message: Message) -> None:
    """
    /batch <link> — extract a post.

    Works in private chat AND groups. In groups you may either inline the
    link (`/batch https://t.me/...`) or reply to a message containing one.
    """
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    # Convenience: /batch as a reply to a message that contains a link.
    if not arg and message.reply_to_message and message.reply_to_message.text:
        arg = message.reply_to_message.text

    if not arg:
        usage = (
            "Usage: /batch `https://t.me/c/1234567890/100-110`\n"
            "or reply to a message containing a post link with /batch"
        )
        await _safe_reply(message, usage)
        return

    await handle_links(message, arg)


async def text_listener(_: Client, message: Message) -> None:
    """Auto-detect post links in ordinary private-chat messages."""
    if extract_links(message.text or ""):
        await handle_links(message, message.text)
    else:
        await _safe_reply(
            message,
            "Send me a Telegram post link, e.g. `https://t.me/channel/123`.\n"
            "In groups use /batch with the link (or reply to it with /batch).",
        )


async def handle_links(message: Message, text: str) -> None:
    """Validate every link in `text`, enqueue valid ones, report failures."""
    links = extract_links(text)
    if not links:
        await _safe_reply(
            message,
            "🤔 No Telegram post links found. Examples:\n"
            "`https://t.me/channel/123`\n"
            "`https://t.me/c/1234567890/100-110`",
        )
        return

    status = await _safe_reply_status(message)

    enqueued, total_ids = 0, 0
    for raw_link in links:
        try:
            link = parse_link(raw_link)
        except ValueError as exc:
            await _safe_reply(message, f"⚠️ `{raw_link}` — {exc}")
            continue

        job = Job(
            link=link,
            chat_id=message.chat.id,
            reply_to=status.id if status else message.id,
        )
        if await enqueue(job):
            enqueued += 1
            total_ids += len(link.message_ids)
        else:
            await _safe_reply(
                message, "🚦 Queue is full — please wait a moment and try again."
            )
            break

    if enqueued and status:
        await status.edit_text(
            f"✅ Queued {enqueued} link(s) — {total_ids} message(s) total. "
            f"I'll report progress here."
        )


async def _safe_reply_status(message: Message) -> Message | None:
    """Create the progress status message; None if Telegram rate-limits us."""
    try:
        return await message.reply_text("🔎 Validating link(s)…", quote=True)
    except FloodWait as e:
        log.warning("FloodWait %ss on status create — skipping.", e.value)
        await asyncio.sleep(e.value + 1)
        return None
    except RPCError:
        try:
            return await message.reply_text("🔎 Validating link(s)…")
        except FloodWait as e:
            log.warning("FloodWait %ss on status create — skipping.", e.value)
            await asyncio.sleep(e.value + 1)
            return None


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #


def register_handlers() -> None:
    """
    Explicitly register all message handlers on the bot client.

    Explicit registration (instead of the @bot.on_message decorator at
    module level) is REQUIRED for correctness here: clients are constructed
    inside the running loop, so this must run after construction.
    """
    bot.add_handler(MessageHandler(cmd_start, filters.command("start") & filters.private))
    bot.add_handler(MessageHandler(cmd_help, filters.command("help") & filters.private))
    # /batch intentionally works in private chats AND groups.
    bot.add_handler(MessageHandler(cmd_batch, filters.command("batch")))
    # Free-text link auto-detection: private chats only (groups opt in via /batch).
    bot.add_handler(
        MessageHandler(
            text_listener,
            filters.private & filters.text & ~filters.command(["start", "help", "batch"]),
        )
    )
    log.info("Message handlers registered.")


async def main() -> None:
    """
    Construct both clients INSIDE the running event loop, register handlers,
    launch the queue worker, then idle until shutdown.
    """
    global bot, user

    bot = Client(
        "extractor-bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )
    user = Client(
        "extractor-user",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True,
        # Upper bound for the parallel downloader's media connections.
        max_concurrent_transmissions=DOWNLOAD_WORKERS,
    )

    register_handlers()

    worker_task = asyncio.create_task(worker())
    try:
        # compose() starts both clients concurrently and blocks in idle()
        # until Ctrl+C. The worker consumes the extraction queue meanwhile.
        await compose([user, bot])
    finally:
        worker_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutdown requested — bye.")
