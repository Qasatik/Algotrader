#!/usr/bin/env python3
"""GitHub Actions publisher for the AlgoTrader Telegram channel.

The local machine cannot reach api.telegram.org (the ISP drops packets to
Telegram IP ranges), but GitHub-hosted runners can. This script runs inside
the ``autopost`` workflow and publishes one post per scheduled run.

Post selection is stateless: the index is derived from the calendar date
(one post every two days, matching the ``day`` field in posts.json).
A per-index Actions cache in the workflow prevents duplicate sends.

Modes:
    --validate   call getMe + getUpdates, print diagnostics (no posting)
    --index      print the post index for today (-1 = series finished)
    --publish    send the selected post to TG_CHAT_ID

Environment:
    TG_BOT_TOKEN   bot token (repo secret)                [required]
    TG_CHAT_ID     @username or -100... channel id         [--publish]
    POST_INDEX     1-based override                        [optional]
    TG_START_DATE  ISO date of post #1, Europe/Moscow      [optional,
                   default 2026-08-30]
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
POSTS_FILE = ROOT / "docs" / "marketing" / "posts.json"
API = "https://api.telegram.org/bot{token}/{method}"
TZ = ZoneInfo("Europe/Moscow")
DEFAULT_START = "2026-08-30"
STEP_DAYS = 2  # posts are scheduled on days 1, 3, 5, ... 19


def _posts() -> list[dict]:
    with open(POSTS_FILE, encoding="utf-8") as f:
        return json.load(f)["posts"]


def _tg(method: str, token: str, **payload) -> dict:
    req = urllib.request.Request(
        API.format(token=token, method=method),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        return {"ok": False, "error_code": e.code, "description": body}


def today_index() -> int:
    """0-based post index for today; -1 when the series is over."""
    start = date.fromisoformat(os.environ.get("TG_START_DATE", DEFAULT_START))
    days = (datetime.now(TZ).date() - start).days
    if days < 0:
        return 0
    idx = days // STEP_DAYS
    posts = _posts()
    return idx if idx < len(posts) else -1


def validate(token: str) -> int:
    me = _tg("getMe", token)
    if not me.get("ok"):
        print(f"getMe FAILED: {me.get('error_code')} {me.get('description')}")
        return 1
    r = me["result"]
    print(f"getMe ok: id={r['id']} username=@{r.get('username')} name={r.get('first_name')}")

    upd = _tg("getUpdates", token, timeout=0)
    if not upd.get("ok"):
        print(f"getUpdates FAILED: {upd.get('error_code')} {upd.get('description')}")
        return 1
    chats: dict = {}
    for u in upd.get("result", []):
        for key in ("message", "edited_message", "channel_post",
                    "edited_channel_post", "callback_query"):
            obj = u.get(key)
            if not obj:
                continue
            chat = obj.get("chat", {}) if key != "callback_query" else obj.get("message", {}).get("chat", {})
            label = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
            chats[chat.get("id")] = f"{chat.get('type')}:{label}"
    if chats:
        print("Chats seen by this bot (candidate TG_CHAT_ID values):")
        for cid, label in sorted(chats.items(), key=lambda kv: str(kv[0])):
            print(f"  {cid}  {label}")
    else:
        print("No updates yet: write /start to the bot in Telegram, or add it "
              "as channel admin and post something, then re-run this job.")
    return 0


def publish(token: str) -> int:
    chat = os.environ.get("TG_CHAT_ID", "").strip()
    if not chat:
        print("TG_CHAT_ID is not set. Add the repo secret TG_CHAT_ID "
              "(@channel_username or -100... numeric id).")
        return 2
    posts = _posts()
    override = os.environ.get("POST_INDEX", "").strip()
    idx = (int(override) - 1) if override else today_index()
    if not 0 <= idx < len(posts):
        print(f"Post index {idx + 1} is out of range 1..{len(posts)}")
        return 1
    post = posts[idx]
    res = _tg("sendMessage", token,
              chat_id=chat,
              text=post["text"],
              parse_mode="HTML",
              disable_web_page_preview=True)
    if res.get("ok"):
        print(f"Posted day-{post['day']} message to {chat}")
        return 0
    print(f"sendMessage FAILED: {res.get('error_code')} {res.get('description')}")
    return 1


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    if not token:
        print("TG_BOT_TOKEN is not set (add the repo secret).")
        return 2
    if mode == "--validate":
        return validate(token)
    if mode == "--index":
        print(today_index())
        return 0
    if mode == "--publish":
        return publish(token)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
