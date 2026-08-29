#!/usr/bin/env python3
"""Scheduled Telegram channel auto-poster for AlgoTrader marketing.

Reads the post pack from ``docs/marketing/posts.json`` and publishes ONE
post per invocation to the channel configured in ``TG_AUTOPOST_CHAT``.
Progress is tracked in ``data/autopost_state.json`` (start date + index),
so the timer can run daily and posts come out one by one, in order.

Environment:
    TG_AUTOPOST_TOKEN — bot token that is an ADMIN of the channel
                        (default: SAAS_BOT_TOKEN, then TELEGRAM_BOT_TOKEN)
    TG_AUTOPOST_CHAT  — channel: @username or -100... numeric id
    TG_AUTOPOST_START — optional ISO date to (re)start the sequence
                        (YYYY-MM-DD); omit to continue from state

Usage (manual test):
    env TG_AUTOPOST_CHAT=@mychannel python3 scripts/tg_autopost.py --dry-run

systemd: deploy/tg-autopost.timer runs this daily (see deploy/README).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSTS_FILE = ROOT / "docs" / "marketing" / "posts.json"
STATE_FILE = ROOT / "data" / "autopost_state.json"
API = "https://api.telegram.org/bot{token}/{method}"


def _posts() -> list[dict]:
    with open(POSTS_FILE, encoding="utf-8") as f:
        return json.load(f)["posts"]


def _state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"start": None, "next_index": 0}


def _save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def _tg(method: str, token: str, **payload) -> dict:
    req = urllib.request.Request(
        API.format(token=token, method=method),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    dry = "--dry-run" in sys.argv
    token = (os.environ.get("TG_AUTOPOST_TOKEN")
             or os.environ.get("SAAS_BOT_TOKEN")
             or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
    chat = os.environ.get("TG_AUTOPOST_CHAT", "").strip()

    posts = _posts()
    st = _state()

    # (Re)start sequence if an explicit start date is given.
    if os.environ.get("TG_AUTOPOST_START"):
        st["start"] = os.environ["TG_AUTOPOST_START"]
        st["next_index"] = 0

    if st["start"] is None:
        st["start"] = date.today().isoformat()
        _save_state(st)
        print(f"state initialised: start={st['start']}")

    start = date.fromisoformat(st["start"])
    today = date.today()
    day_num = (today - start).days + 1  # day 1 = start day

    # Find the next post whose scheduled day has arrived.
    idx = st["next_index"]
    while idx < len(posts) and posts[idx]["day"] < day_num:
        print(f"skipping post #{idx} (day {posts[idx]['day']} < today "
              f"day {day_num}) — already past")
        idx += 1
    if idx >= len(posts):
        print("post pack finished — nothing to publish")
        _save_state({**st, "next_index": idx})
        return 0
    post = posts[idx]
    if post["day"] > day_num:
        print(f"next post scheduled for day {post['day']} "
              f"(today is day {day_num}) — waiting")
        return 0

    if dry:
        print(f"DRY RUN — would publish post #{idx} (day {post['day']}):")
        print(post["text"][:400] + ("..." if len(post["text"]) > 400 else ""))
        return 0

    if not token or not chat:
        print("TG_AUTOPOST_TOKEN/SAAS_BOT_TOKEN and TG_AUTOPOST_CHAT "
              "must be set", file=sys.stderr)
        return 1

    res = _tg("sendMessage", token,
              chat_id=chat, text=post["text"],
              parse_mode="HTML", disable_web_page_preview=True)
    if not res.get("ok"):
        print(f"telegram error: {res}", file=sys.stderr)
        return 1
    print(f"published post #{idx} (day {post['day']}) to {chat} at "
          f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    _save_state({**st, "next_index": idx + 1})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
