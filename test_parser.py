"""Standalone sanity checks for parser.py (no pyrogram needed)."""
import parser as p

# --- basic public ---
r = p.parse_link("https://t.me/channel_username/1234")
assert r.chat_id == "@channel_username" and r.message_ids == (1234,) and r.topic_id is None, r

r = p.parse_link("t.me/foo_bar/7")
assert r.chat_id == "@foo_bar" and r.message_ids == (7,), r

r = p.parse_link("https://t.me/channel_username/100-110")
assert r.message_ids == tuple(range(100, 111)), r

# --- private ---
r = p.parse_link("https://t.me/c/1234567890/1234")
assert r.chat_id == -1001234567890 and r.topic_id is None, r

r = p.parse_link("https://t.me/c/1234567890/100-110")
assert r.chat_id == -1001234567890 and r.message_ids == tuple(range(100, 111)), r

# --- forum / topic links (the bug) ---
# public forum: 3 segments — topic 9534, message 9899
r = p.parse_link("https://t.me/cuetpgscqp09/9534/9899")
assert r.chat_id == "@cuetpgscqp09" and r.topic_id == 9534 and r.message_ids == (9899,), r

# private forum: t.me/c/<id>/<topic>/<msg>
r = p.parse_link("https://t.me/c/3893652272/243/2585")
assert r.chat_id == -1003893652272 and r.topic_id == 243 and r.message_ids == (2585,), r

# forum range: t.me/c/<id>/<topic>/<start>-<end>
r = p.parse_link("https://t.me/c/3893652272/243/2580-2585")
assert r.message_ids == tuple(range(2580, 2586)) and r.topic_id == 243, r

# extra trailing segment tolerated: t.me/<chat>/<topic>/<msg>/<msg>
r = p.parse_link("https://t.me/cuetpgscqp09/9534/9899/9899")
assert r.message_ids == (9899,), r

# --- query strings tolerated ---
r = p.parse_link("https://t.me/c/1234567890/12?single")
assert r.message_ids == (12,), r

# --- invalid inputs rejected ---
try:
    p.parse_link("https://t.me/c/1234567890/110-100")
    raise SystemExit("FAIL: inverted range accepted")
except ValueError:
    pass

for bad in ("https://t.me/channel_username", "hello world", "https://t.me/c/abc/12"):
    try:
        p.parse_link(bad)
        raise SystemExit(f"FAIL: accepted {bad!r}")
    except ValueError:
        pass

# --- extract_links in free text ---
found = p.extract_links(
    "check https://t.me/foo/1 and t.me/c/1234567890/22 plus "
    "https://t.me/c/3893652272/243/2585 and nothing else"
)
assert found == [
    "https://t.me/foo/1",
    "t.me/c/1234567890/22",
    "https://t.me/c/3893652272/243/2585",
], found

print("parser: ALL CHECKS PASSED")
