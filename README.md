# Telegram Content Extractor Bot

A production-ready, modular Python bot that extracts content from Telegram
channels — including chats with **"Restrict saving content"** (noforwards)
enabled and **private channels** the user account has joined — and re-sends
it cleanly to you in a private bot chat.

It works with a **dual-client architecture**:

| Client | Auth | Role |
|---|---|---|
| **Bot client** | BotFather token | UI: commands, link intake, progress updates, final delivery |
| **User client** | Pyrogram `SESSION_STRING` | MTProto powerhorse: fetches raw messages & media from restricted/private chats where bots cannot go |

> **Legal note:** only extract content you have the right to access. This tool
> does not bypass membership: the user account must be a legitimate member of
> the private channel. Respect Telegram's ToS and copyright.

---

## Features

- ✅ Public links: `https://t.me/channel_username/1234`
- ✅ Private links: `https://t.me/c/1234567890/1234` (proper `-100…` peer conversion)
- ✅ Ranges / batches: `https://t.me/c/1234567890/100-110`
- ✅ Media types: photo, video (streamable), document, audio, voice, video note, animation, sticker
- ✅ Metadata preserved: caption, file name, mime type, duration, dimensions, thumbnail
- ✅ Live progress bars (download + upload)
- ✅ `FloodWait` auto-sleep, `ChannelPrivate`/`ChatAdminRequired` friendly errors
- ✅ Stall watchdog aborts frozen downloads (`DOWNLOAD_TIMEOUT`)
- ✅ Temp files deleted immediately after upload

---

## Project layout

```
.
├── main.py              # entry point: dual clients, handlers, queue worker
├── config.py            # env-var loading & validation
├── parser.py            # t.me link parsing (public/private/range)
├── downloader.py        # user-client fetching + watchdog-guarded downloads
├── sender.py            # bot-client re-uploads with metadata + cleanup
├── generate_session.py  # CLI: creates your SESSION_STRING
├── requirements.txt
├── .env.example         # copy to .env and fill in
└── README.md
```

---

## Setup

### 1. Get `API_ID` and `API_HASH`

1. Go to <https://my.telegram.org> and log in with your phone number.
2. Open **API development tools**.
3. Create an app (any title, e.g. `extractor`).
4. Copy **api_id** and **api_hash**.

### 2. Create the bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot`, choose a name and username.
3. Copy the **bot token** (`1234567890:AA…`).

### 3. Install dependencies

Python **3.10+** required.

> **Library note:** this project uses **PyroFork** (a maintained Pyrogram fork,
> installed automatically as `pyrofork` — your code still imports `pyrogram`).
> Official Pyrogram 2.0.106 is unmaintained and rejects modern 64-bit channel
> IDs (`Peer id invalid` on newer private channels), so don't swap it back.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> **Windows note:** if `TgCrypto` fails to build, install
> [Build Tools for Visual Studio](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
> or run without it (slower but functional).

### 4. Generate your session string

```bash
python generate_session.py
```

Follow the prompts: `API_ID`, `API_HASH`, phone number, login code
(and 2FA password if enabled). Copy the printed string.

> ⚠️ This string **is** your Telegram account. Never share it, never commit it.

### 5. Configure environment

```bash
cp .env.example .env
```

Fill in:

```env
API_ID=1234567
API_HASH=abcdef0123456789abcdef0123456789
BOT_TOKEN=1234567890:AA-your-bot-token
SESSION_STRING=1BQANOTEuMTA0LjAu...   # output of generate_session.py
TEMP_DOWNLOAD_DIR=./downloads         # optional
```

### 6. Run

```bash
python main.py
```

You'll see `Bot is up` in the console. Open your bot in Telegram and send
`/start`.

---

## Usage

| Input | Behaviour |
|---|---|
| `/start` | Welcome + quick usage guide |
| `/help` | Detailed explanation and limits |
| `/batch <link>` | Explicitly queue a link (same as pasting it) |
| Paste any `t.me/...` post link | Auto-detected, validated, queued, extracted |

**Examples**

```
https://t.me/durov/42                      # single public post
https://t.me/c/1697797156/150              # single private post
https://t.me/c/1697797156/100-110          # range of 11 posts
```

Progress is reported by editing the status message; finished media arrives as
a fresh, unrestricted upload in the same chat.

---

## Configuration reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `API_ID` | ✔ | — | Telegram API id |
| `API_HASH` | ✔ | — | Telegram API hash |
| `BOT_TOKEN` | ✔ | — | BotFather token |
| `SESSION_STRING` | ✔ | — | Pyrogram session string (user account) |
| `TEMP_DOWNLOAD_DIR` | ✖ | `./downloads` | Media staging directory |
| `QUEUE_MAX_SIZE` | ✖ | `100` | Pending jobs before the bot starts refusing |
| `DOWNLOAD_TIMEOUT` | ✖ | `300` | Seconds without progress before a download is aborted |
| `MAX_FILE_SIZE` | ✖ | `2147483648` | Max media size in bytes (2 GiB) |
| `DOWNLOAD_WORKERS` | ✖ | `6` | Parallel connections used to download one file |
| `DOWNLOAD_PART_BATCH` | ✖ | `16` | 1 MiB chunks per connection per segment (16 = 16 MiB) |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ChannelPrivate` on a private link | The **user** account must be a member of that channel. Join it first. |
| `CHAT_WRITE_FORBIDDEN` from the bot | You haven't pressed **Start** on the bot. |
| `AUTH_KEY_UNREGISTERED` | Session string is stale/revoked — regenerate it. |
| Slow downloads | Make sure `TgCrypto` installed successfully (see pip output). Media is fetched over several parallel connections (`DOWNLOAD_WORKERS`); if your ISP link to Telegram is the bottleneck (measure ~3 MiB/s on a single stream), more connections won't help — only a faster network will. |
| `File too large` | Raise `MAX_FILE_SIZE` or split your request. |

---

## Notes & limitations

- The bot processes one batch at a time by design — this keeps you clear of
  Telegram's flood limits.
- Protected *bot* content (e.g. messages the bot cannot read because of
  noforwards) is re-uploaded from downloaded bytes, so the restriction is
  gone; the original quality is preserved bit-for-bit for documents, and
  re-encoded only where Telegram itself requires it (e.g. stickers sent as
  documents keep original `.webp/.tgs/.webm` bytes).
- Web-page previews and polls are currently mirrored as text; service
  messages are skipped.
