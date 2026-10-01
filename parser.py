"""
parser.py — Telegram post-link parsing utilities.

Supports the following link shapes:

    Public : https://t.me/<username>/<message_id>
             https://t.me/<username>/<start>-<end>                (range)

    Forum  : https://t.me/<username>/<topic_id>/<message_id>
             https://t.me/<username>/<topic_id>/<start>-<end>
             (topic links tolerate extra trailing numeric segments, which
              some Telegram clients append when copying from inside a topic)

    Private: https://t.me/c/<internal_chat_id>/<message_id>
             https://t.me/c/<internal_chat_id>/<start>-<end>
             https://t.me/c/<internal_chat_id>/<topic_id>/<message_id>
             https://t.me/c/<internal_chat_id>/<topic_id>/<start>-<end>

    Share links (with `?single` etc. query strings) are tolerated.

Internal chat IDs from private links are converted to the MTProto peer id
expected by Pyrogram:  -100<internal_id>  (channels & supergroups).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Explicit patterns keep the grammar readable.  Shared groups:
#   topic — optional forum-topic id (3rd path segment in forum links)
#   start — first message id of the (possibly empty) range
#   end   — optional "-<end>" range suffix
_PUBLIC_RE = re.compile(
    r"^(?:https?://)?t\.me/"
    r"(?P<chat>[A-Za-z0-9_]{3,64})"
    r"(?:/(?P<topic>\d{1,14}))?"       # optional forum topic id
    r"/(?P<start>\d{1,14})"
    r"(?:-(?P<end>\d{1,14}))?"
    r"(?:/\d{1,14})*"                  # tolerate extra trailing numeric segments
    r"/?$",
    flags=re.I,
)

_PRIVATE_RE = re.compile(
    r"^(?:https?://)?t\.me/c/"
    r"(?P<internal>\d{1,19})"
    r"(?:/(?P<topic>\d{1,14}))?"       # optional forum topic id
    r"/(?P<start>\d{1,14})"
    r"(?:-(?P<end>\d{1,14}))?"
    r"(?:/\d{1,14})*"                  # tolerate extra trailing numeric segments
    r"/?$",
    flags=re.I,
)


@dataclass(frozen=True)
class ParsedLink:
    """A validated Telegram post link (single message or a 1..N range)."""

    chat_id: str | int            # "@username" or -100… internal peer id
    message_ids: tuple[int, ...]  # one or more sequential ids
    topic_id: int | None = None   # forum topic id, when present in the link

    @property
    def is_private(self) -> bool:
        return isinstance(self.chat_id, int)

    @property
    def is_range(self) -> bool:
        return len(self.message_ids) > 1


def parse_link(raw: str) -> ParsedLink:
    """
    Parse and validate a Telegram post link.

    Raises:
        ValueError: if the link is malformed or the range is invalid.
    """
    link = raw.strip().rstrip("/")
    # Tolerate a missing scheme and strip query strings (?single, ?comment=…).
    link = re.sub(r"^https?://", "", link, flags=re.I)
    link = link.split("?", 1)[0]

    # ---- private link: t.me/c/<id>[/<topic>]/<msg>[-<end>][/<extra>] ----- #
    m = _PRIVATE_RE.match(link)
    if m:
        internal_id = int(m.group("internal"))
        # MTProto peer id for channels/supergroups: -100<internal_id>
        # e.g. t.me/c/1234567890/12  ->  chat_id == -1001234567890
        chat_id = int(f"-100{internal_id}")
        topic = int(m.group("topic")) if m.group("topic") else None
        start = int(m.group("start"))
        end = int(m.group("end")) if m.group("end") else start
        _validate_range(start, end)
        return ParsedLink(chat_id, tuple(range(start, end + 1)), topic)

    # ---- public link: t.me/<username>[/<topic>]/<msg>[-<end>] ------------ #
    m = _PUBLIC_RE.match(link)
    if m:
        chat = f"@{m.group('chat')}"
        topic = int(m.group("topic")) if m.group("topic") else None
        start = int(m.group("start"))
        end = int(m.group("end")) if m.group("end") else start
        _validate_range(start, end)
        return ParsedLink(chat, tuple(range(start, end + 1)), topic)

    raise ValueError("Not a recognised Telegram post link.")


def _validate_range(start: int, end: int) -> None:
    """Reject inverted or oversized ranges."""
    if end < start:
        raise ValueError(f"Invalid range {start}-{end}: end is before start.")
    if end - start + 1 > 200:
        raise ValueError("Range too large: maximum 200 messages per batch.")


def extract_links(text: str) -> list[str]:
    """
    Pull every plausible t.me post link out of a free-form text message.

    Matches 2-, 3- or 4-segment paths (message, topic/message, and the
    odd duplicated form some clients produce). Full validation happens in
    `parse_link`.

    The lookbehind ensures `t.me` is a standalone token (start of string,
    whitespace, '//' or common separators) so we never match a suffix of
    a longer word — while still tolerating missing schemes like `t.me/foo/1`.
    """
    pattern = re.compile(
        r"(?<![\w.])"                          # token start: not a word char or dot
        r"(?:https?://)?"                      # optional scheme
        r"t\.me/"                              # the host
        r"(?:c/\d{1,19}|[A-Za-z0-9_]{3,64})"   # private internal id or public username
        r"(?:/\d{1,14}){1,4}"                  # 1-4 numeric path segments
        r"(?:-\d{1,14})?",                     # optional range suffix on the last one
        flags=re.I,
    )
    return [m.group(0) for m in pattern.finditer(text or "")]
