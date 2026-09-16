# -*- coding: utf-8 -*-
"""이미지 준비 — 원문에 사진이 있으면 그걸 받고, 없으면 원문 게시물을 캡처한다."""
import os

import requests

from .store import ROOT

MEDIA_DIR = os.path.join(ROOT, "data", "media")


def download(url, name):
    os.makedirs(MEDIA_DIR, exist_ok=True)
    path = os.path.join(MEDIA_DIR, name)
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)
    return path


def screenshot_post(source, post_id, name):
    """t.me 임베드 위젯의 말풍선만 잘라 찍는다."""
    from playwright.sync_api import sync_playwright
    os.makedirs(MEDIA_DIR, exist_ok=True)
    path = os.path.join(MEDIA_DIR, name)
    url = "https://t.me/%s/%d?embed=1&mode=tme" % (source, post_id)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 560, "height": 900}, device_scale_factor=2)
        pg.goto(url, wait_until="networkidle", timeout=30000)
        el = pg.query_selector(".tgme_widget_message_bubble") or pg.query_selector("body")
        el.screenshot(path=path)
        b.close()
    return path


def screenshot_page(url, name):
    """기사 페이지 첫 화면(16:9) 캡처. 구글뉴스 링크는 원문으로 넘어갈 때까지 기다린다."""
    from playwright.sync_api import sync_playwright
    os.makedirs(MEDIA_DIR, exist_ok=True)
    path = os.path.join(MEDIA_DIR, name)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1200, "height": 675}, locale="ko-KR")
        pg.goto(url, wait_until="domcontentloaded", timeout=30000)
        if "news.google.com" in url:
            try:
                pg.wait_for_url(lambda u: "news.google.com" not in u, timeout=15000)
            except Exception:
                pass
        pg.wait_for_timeout(2500)
        pg.screenshot(path=path)
        b.close()
    return path


def image_for(item, draft_id):
    base = "d%d_%s_%d" % (draft_id, item["source"], item["post_id"])
    try:
        if item["photos"]:
            return download(item["photos"][0], base + ".jpg")
        if item["source"] == "news":
            from .news import og_image
            og = og_image(item["url"])
            if og:
                return download(og, base + ".jpg")
            return screenshot_page(item["url"], base + ".png")
        return screenshot_post(item["source"], item["post_id"], base + ".png")
    except Exception as e:  # 이미지는 없어도 글은 나간다
        print("[capture] 실패:", e)
        return ""
