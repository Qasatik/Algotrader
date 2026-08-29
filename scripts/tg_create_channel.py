#!/usr/bin/env python3
"""Create the marketing Telegram channel and promote the bot, fully automated.

Bots cannot create channels (Bot API limitation), so this script logs into
the OWNER's account via MTProto (Telethon) from a GitHub Actions runner,
creates the channel, invites the bot, grants admin rights, sets a public
@username and prints the Bot API chat id.

The Telegram login code is delivered through the bot itself: Telegram sends
the code to the owner's app, the owner forwards it to the bot in a private
chat, this script polls getUpdates to pick it up and immediately deletes
the message. Same for the 2FA password ("2fa: <password>"). The bot also
chats with the owner: it announces each code request and reports errors,
so the owner always knows which code is expected.

Environment:
    TG_BOT_TOKEN     bot token (repo secret)              [required]
    TG_PHONE         owner account, e.g. +79991234567     [required]
    TG_CHANNEL_TITLE channel title                        [optional]

Output markers (parsed from logs):
    TG_CHAT_ID=-100...   Bot API id of the created channel
    CHANNEL_LINK=https://t.me/...  public link (if username was free)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import urllib.request

# Telegram Desktop public API pair (shipped in TDConfig; fine for one-shot tooling).
API_ID = 611335
API_HASH = "d524b414d21f4d37f08684c1df41ac9c"
BOT_USERNAME = "Alg0tr4debot"
DEFAULT_TITLE = "AlgoTrader — фандинг на автопилоте"
ABOUT = ("Дельта-нейтральный сбор фандинга на Bybit. Честные цифры, "
         "открытый код, вход только при положительном EV. "
         f"Бот: @{BOT_USERNAME}")
USERNAME_CANDIDATES = ["algotrader_funding", "algotrader_channel",
                       "algotrader_bybit", "algotrader_fund"]
CODE_WAIT_S = 600
POLL_S = 5
MAX_ATTEMPTS = 3

API = "https://api.telegram.org/bot{token}/{method}"


def bot_api(method: str, token: str, **payload) -> dict:
    req = urllib.request.Request(
        API.format(token=token, method=method),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def notify(token: str, chat, text: str) -> None:
    """Best-effort message from the bot to the owner's private chat."""
    if not chat:
        return
    try:
        bot_api("sendMessage", token, chat_id=chat, text=text)
    except Exception:
        pass


async def wait_for_message(token: str, patterns: dict, baseline: int,
                           timeout: int, exclude: set):
    """Poll getUpdates until a NEW message matches a pattern; delete it.

    ``exclude`` holds values already tried (e.g. stale codes) — matches are
    deleted but skipped, so the loop keeps waiting for a fresh value.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            updates = bot_api("getUpdates", token,
                              offset=baseline + 1, timeout=0).get("result", [])
        except Exception:
            updates = []
        for u in updates:
            baseline = max(baseline, u["update_id"])
            msg = u.get("message") or {}
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            for key, pat in patterns.items():
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    value = m.group(1)
                    chat, mid = msg.get("chat", {}).get("id"), msg.get("message_id")
                    if chat and mid:
                        try:
                            bot_api("deleteMessage", token,
                                    chat_id=chat, message_id=mid)
                        except Exception:
                            pass
                    if value not in exclude:
                        return key, value, baseline
        await asyncio.sleep(POLL_S)
    return None, None, baseline


async def main() -> int:
    from telethon import TelegramClient, functions, types
    from telethon.errors import (FloodWaitError, SessionPasswordNeededError,
                                 PhoneCodeExpiredError, PhoneCodeInvalidError)
    from telethon.sessions import StringSession

    token = os.environ["TG_BOT_TOKEN"].strip()
    phone = os.environ["TG_PHONE"].strip()
    title = (os.environ.get("TG_CHANNEL_TITLE") or DEFAULT_TITLE).strip()

    # Baseline so we only read messages that arrive from now on; also find
    # the owner's private chat with the bot for proactive notifications.
    baseline = 0
    owner_chat = None
    for u in bot_api("getUpdates", token, timeout=0).get("result", []):
        baseline = max(baseline, u["update_id"])
        msg = u.get("message") or {}
        if msg.get("chat", {}).get("type") == "private":
            owner_chat = msg["chat"]["id"]

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    tried_codes: set = set()
    signed_in = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            sent = await client.send_code_request(phone)
        except FloodWaitError as e:
            print(f"Telegram flood-wait: retry in {e.seconds}s")
            notify(token, owner_chat,
                   f"Telegram просит подождать {e.seconds} сек перед новым кодом.")
            return 1
        print(f"[attempt {attempt}/{MAX_ATTEMPTS}] login code requested for "
              f"{phone[:5]}*** — forward the FRESH code to @{BOT_USERNAME}")
        notify(token, owner_chat,
               f"Код {attempt}/{MAX_ATTEMPTS}: Telegram отправил новый код. "
               "Перешли сюда СВЕЖИЙ код (старые не работают), у тебя 10 минут.")

        key, code, baseline = await wait_for_message(
            token, {"code": r"\b(\d{4,8})\b"}, baseline, CODE_WAIT_S,
            exclude=tried_codes)
        if key != "code":
            print("No code received in time.")
            notify(token, owner_chat,
                   "Код не получен за 10 минут. Запусти воркфлоу ещё раз.")
            return 1
        tried_codes.add(code)

        try:
            await client.sign_in(phone=phone, code=code,
                                 phone_code_hash=sent.phone_code_hash)
            signed_in = True
            break
        except SessionPasswordNeededError:
            print("2FA enabled — waiting for '2fa: <password>'.")
            notify(token, owner_chat,
                   "Код принят. Включена 2FA — отправь сюда: 2fa: <твой пароль> "
                   "(сообщение сразу удалю).")
            key, pw, baseline = await wait_for_message(
                token, {"pw": r"2fa\s*[:\-]?\s*(\S+)"}, baseline, 300,
                exclude=set())
            if key != "pw":
                print("No 2FA password received in time.")
                return 1
            await client.sign_in(password=pw)
            signed_in = True
            break
        except (PhoneCodeExpiredError, PhoneCodeInvalidError) as e:
            print(f"attempt {attempt}: {type(e).__name__}")
            notify(token, owner_chat,
                   "Этот код устарел. Запрашиваю новый — перешли самый "
                   "СВЕЖИЙ код из Telegram.")
    if not signed_in:
        print("All attempts failed.")
        notify(token, owner_chat,
               "Не получилось войти: коды устаревали. Запусти воркфлоу ещё раз.")
        return 1

    me = await client.get_me()
    print(f"Logged in as {me.first_name} (id {me.id})")
    notify(token, owner_chat, "Вход выполнен. Создаю канал...")

    res = await client(functions.channels.CreateChannelRequest(
        title=title, about=ABOUT, megagroup=False))
    channel = next(c for c in res.chats if isinstance(c, types.Channel))
    print(f"Channel created: {title!r} (raw id {channel.id})")

    bot = await client.get_entity(BOT_USERNAME)
    await client(functions.channels.InviteToChannelRequest(
        channel=channel, users=[bot]))
    rights = types.ChannelAdminRights(
        post_messages=True, edit_messages=True, delete_messages=True,
        invite_users=True, pin_messages=True)
    await client(functions.channels.EditAdminRequest(
        channel=channel, user_id=bot, admin_rights=rights, rank="autopost"))
    print(f"@{BOT_USERNAME} is now a channel admin")

    link = None
    for u in USERNAME_CANDIDATES:
        try:
            await client(functions.channels.UpdateUsernameRequest(
                channel=channel, username=u))
            link = f"https://t.me/{u}"
            print(f"Public link: {link}")
            break
        except Exception as e:
            print(f"username {u}: {type(e).__name__}")

    print(f"TG_CHAT_ID=-100{channel.id}")
    if link:
        print(f"CHANNEL_LINK={link}")
    notify(token, owner_chat,
           f"Готово. Канал создан, я админ. {link or ''}")

    await client.log_out()
    print("Owner session logged out. Done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as e:  # noqa: BLE001 — surface everything to the log
        print(f"FAILED: {type(e).__name__}: {e}")
        raise SystemExit(1)
