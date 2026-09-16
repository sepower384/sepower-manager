# -*- coding: utf-8 -*-
"""텔레그램 Bot API — requests 만 쓴다.

.env:
  TELEGRAM_BOT_TOKEN_MANAGER  세력의 매니저 봇 토큰
  TELEGRAM_ADMIN_CHAT_ID      초안 승인 받을 곳(강회장 DM)
  TELEGRAM_TARGET_CHAT_ID     실제 발행할 채널(@아이디 또는 -100…). 비우면 발행 안 함
"""
import json
import os

import requests

API = "https://api.telegram.org/bot%s/%s"
MAX_CAPTION = 1024
_post = requests.post


def env(k):
    return (os.environ.get(k) or "").strip()


def token():
    return env("TELEGRAM_BOT_TOKEN_MANAGER")


def admin_chat():
    return env("TELEGRAM_ADMIN_CHAT_ID")


def target_chat():
    return env("TELEGRAM_TARGET_CHAT_ID")


def call(method, data=None, files=None, timeout=30):
    r = _post(API % (token(), method), data=data, files=files, timeout=timeout)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError("%s 실패: %s" % (method, j.get("description")))
    return j["result"]


def keyboard(rows):
    return json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]})


def send(chat, text, photo="", buttons=None, reply_to=None):
    """사진 있으면 사진+캡션, 캡션이 길면 사진 먼저 보내고 글은 따로. 마지막 메시지 반환."""
    extra = {}
    if buttons:
        extra["reply_markup"] = keyboard(buttons)
    if reply_to:
        extra["reply_to_message_id"] = reply_to
    if photo and os.path.exists(photo):
        with open(photo, "rb") as f:
            if len(text) <= MAX_CAPTION:
                return call("sendPhoto", {"chat_id": chat, "caption": text, **extra},
                            files={"photo": f}, timeout=60)
            call("sendPhoto", {"chat_id": chat}, files={"photo": f}, timeout=60)
    return call("sendMessage", {"chat_id": chat, "text": text,
                                "disable_web_page_preview": "true" if photo else "false", **extra})


def edit_buttons(chat, msg_id, buttons=None):
    try:
        call("editMessageReplyMarkup", {"chat_id": chat, "message_id": msg_id,
                                        "reply_markup": keyboard(buttons or [])})
    except RuntimeError:
        pass


def answer(cb_id, text=""):
    try:
        call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})
    except RuntimeError:
        pass


def updates(offset, timeout=50):
    return call("getUpdates", {"offset": offset, "timeout": timeout,
                               "allowed_updates": json.dumps(["message", "callback_query", "my_chat_member"])},
                timeout=timeout + 10)
