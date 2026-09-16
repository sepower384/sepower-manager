# -*- coding: utf-8 -*-
"""공개 텔레그램 채널 읽기 — t.me/s/<아이디> 웹 미리보기를 파싱한다(로그인·API키 불필요)."""
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126 Safari/537.36"}
_get = requests.get  # 테스트에서 갈아끼움


def _text(el):
    if el is None:
        return ""
    for br in el.find_all("br"):
        br.replace_with("\n")
    return el.get_text().strip()


def _num(s):
    s = (s or "").strip().upper().replace(",", "")
    m = re.match(r"([\d.]+)([KM]?)", s)
    if not m:
        return 0
    return int(float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6}[m.group(2)])


def parse_page(html, channel):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for w in soup.select("div.tgme_widget_message[data-post]"):
        post = w["data-post"]  # "channel/123"
        pid = int(post.split("/")[-1])
        # 답장 인용 블록 안의 text 는 빼고 본문만
        body = None
        for t in w.select("div.tgme_widget_message_text"):
            if "js-message_reply_text" not in (t.get("class") or []):
                body = t
        links = []
        if body is not None:
            links = [a["href"] for a in body.find_all("a", href=True) if a["href"].startswith("http")]
        photos = []
        for a in w.select("a.tgme_widget_message_photo_wrap"):
            m = re.search(r"url\('([^']+)'\)", a.get("style", ""))
            if m:
                photos.append(m.group(1))
        tm = w.select_one("time[datetime]")
        date = tm["datetime"] if tm else datetime.now(timezone.utc).isoformat()
        views = _num(_text(w.select_one("span.tgme_widget_message_views")))
        owner = w.select_one(".tgme_widget_message_owner_name")
        preview = w.select_one("a.tgme_widget_message_link_preview")
        out.append({
            "source": channel,
            "post_id": pid,
            "url": "https://t.me/%s" % post,
            "date": date,
            "text": _text(body),
            "links": links,
            "photos": photos,
            "views": views,
            "publisher": _text(owner) or channel,
            "preview_title": _text(preview.select_one(".link_preview_title")) if preview else "",
            "has_video": bool(w.select_one(".tgme_widget_message_video_player")),
        })
    return out


def fetch(channel, before=None, timeout=15):
    url = "https://t.me/s/%s" % channel
    if before:
        url += "?before=%d" % before
    r = _get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return parse_page(r.text, channel)


def fetch_recent(channel, pages=1):
    """최신 글부터 pages 페이지(페이지당 ~20개)."""
    items, before = [], None
    for _ in range(pages):
        got = fetch(channel, before)
        if not got:
            break
        items.extend(got)
        before = min(i["post_id"] for i in got)
    return items
