# -*- coding: utf-8 -*-
"""자료 이미지 — 모든 글에 반드시 1장.

순서: 원문 기사 화면 캡처(제목 중심) → 원문 사진/og:image → 제목 카드(항상 성공).
텔레그램 원문(코인니스 등)은 게시물 말풍선 캡처가 곧 자료 캡처다.
최근 48시간에 쓴 것과 같은 이미지(해시)는 건너뛴다.
"""
import hashlib
import html
import os
from datetime import datetime

import requests

from .store import KST, ROOT

MEDIA_DIR = os.path.join(ROOT, "data", "media")
MIN_BYTES = 40000          # 이보다 작으면 빈 화면·로고일 가능성 (정상 캡처는 60KB~350KB)
AD_HOSTS = ("doubleclick", "googlesyndication", "adservice", "taboola", "outbrain", "criteo",
            "adnxs", "amazon-adsystem", "dable", "mobon", "adfit", "realclick", "popads")

HIDE_OVERLAYS_JS = """
() => {
  for (const el of document.querySelectorAll('body *')) {
    const s = getComputedStyle(el);
    if ((s.position === 'fixed' || s.position === 'sticky') && el.offsetHeight > 0) el.remove();
  }
  for (const sel of ['[id*=cookie]','[class*=cookie]','[class*=consent]','[id*=consent]',
                     '[class*=popup]','[class*=modal]','[class*=subscribe]','iframe']) {
    document.querySelectorAll(sel).forEach(e => e.remove());
  }
  document.documentElement.style.overflow = 'auto';
  document.body.style.overflow = 'auto';
}
"""

FIND_HEADLINES_JS = """
() => {
  const sel = 'h1, h2, h3, [class*=title], [class*=Title], [class*=headline], [class*=subject], [id*=title]';
  const seen = new Set();
  const out = [];
  for (const h of document.querySelectorAll(sel)) {
    const t = (h.innerText || '').trim();
    if (t.length < 8 || t.length > 200 || seen.has(t)) continue;
    const r = h.getBoundingClientRect();
    const fs = parseFloat(getComputedStyle(h).fontSize);
    if (r.width < 200 || r.height < 18 || fs < 18) continue;
    seen.add(t);
    // 제목이 들어있는 본문 칸 너비 (사이드바·광고 제외)
    let box = h.parentElement, w = r.width;
    while (box && box.getBoundingClientRect().width < 560) box = box.parentElement;
    if (box) w = Math.min(box.getBoundingClientRect().width, 1100);
    out.push({x: Math.max(0, r.left - 40), y: r.top + window.scrollY, w: Math.max(640, w + 80), t, fs});
  }
  out.sort((a, b) => b.fs - a.fs);
  return out.slice(0, 20);
}
"""


def pick_headline(cands, want):
    """기사 제목과 가장 비슷한 요소. 기대 제목이 없으면 가장 큰 글씨."""
    if not cands:
        return None
    if not want:
        return cands[0]
    scored = [(match_score(c["t"], want), c) for c in cands]
    best = max(scored, key=lambda s: s[0])
    return best[1] if best[0] >= 0.3 else None


def _path(name):
    os.makedirs(MEDIA_DIR, exist_ok=True)
    return os.path.join(MEDIA_DIR, name)


def file_hash(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def download(url, name):
    path = _path(name)
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    if len(r.content) < 8000:
        raise ValueError("이미지가 너무 작음")
    with open(path, "wb") as f:
        f.write(r.content)
    return path


def _browser(p):
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1200, "height": 900}, locale="ko-KR", device_scale_factor=1)
    for h in AD_HOSTS:                  # 광고 주소만 골라 막는다(모든 요청 검사는 느림)
        ctx.route("**/*%s*/**" % h, lambda route: route.abort())
    ctx.route("**/*.{mp4,webm,woff,woff2}", lambda route: route.abort())
    return b, ctx


def screenshot_post(source, post_id, name):
    """텔레그램 게시물 말풍선만 잘라 찍는다."""
    from playwright.sync_api import sync_playwright
    path = _path(name)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 560, "height": 900}, device_scale_factor=2)
        pg.goto("https://t.me/%s/%d?embed=1&mode=tme" % (source, post_id), wait_until="networkidle", timeout=30000)
        el = pg.query_selector(".tgme_widget_message_bubble") or pg.query_selector("body")
        el.screenshot(path=path)
        b.close()
    return path


def looks_blank(path):
    """거의 한 색(흰 화면·로딩·빈 박스)이면 True."""
    try:
        from PIL import Image
    except ImportError:
        return False
    im = Image.open(path).convert("L").resize((64, 36))
    px = list(im.getdata())
    common = max(set(px), key=px.count)
    same = sum(1 for v in px if abs(v - common) < 12) / len(px)
    return same > 0.9


def match_score(page_title, want):
    from .filters import grams
    a, b = grams(page_title), grams(want)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def screenshot_article(url, name, want_title=""):
    """기사 화면 캡처 — 팝업·고정 배너 치우고 제목부터 16:9 로 자른다.
    제목을 못 찾거나 기사 제목과 다르거나(동의창·메인화면) 빈 화면이면 실패로 본다."""
    from playwright.sync_api import sync_playwright
    path = _path(name)
    with sync_playwright() as p:
        b, ctx = _browser(p)
        pg = ctx.new_page()
        pg.set_default_timeout(15000)       # 느린 사이트 하나가 회차를 붙잡지 않게
        pg.goto(url, wait_until="domcontentloaded", timeout=25000)
        if "news.google.com" in url:
            try:
                pg.wait_for_url(lambda u: "news.google.com" not in u, timeout=12000)
            except Exception:
                pass
        try:
            pg.wait_for_load_state("load", timeout=8000)
        except Exception:
            pass
        pg.wait_for_timeout(1500)
        pg.evaluate(HIDE_OVERLAYS_JS)
        pg.wait_for_timeout(500)
        found = pg.evaluate(FIND_HEADLINES_JS)
        head = pick_headline(found, want_title)
        if not head:
            b.close()
            raise ValueError("기사 제목을 못 찾음(후보 %d개: %s)" % (len(found), found[0]["t"][:30] if found else "-"))
        x = head["x"]
        w = min(1200 - x, head["w"])
        pg.evaluate("() => window.scrollTo(0, 0)")
        pg.screenshot(path=path, full_page=True,       # 페이지 절대좌표로 자름(스크롤 어긋남 방지)
                      clip={"x": x, "y": max(0, head["y"] - 40), "width": w, "height": round(w * 9 / 16)})
        final = pg.url
        b.close()
    if "news.google.com" in final:
        raise ValueError("원문으로 못 넘어감")
    if os.path.getsize(path) < MIN_BYTES or looks_blank(path):
        raise ValueError("빈 화면 같음")
    return path


CARD_HTML = """<html><head><meta charset="utf-8"><style>
body{margin:0;width:1200px;height:675px;background:#0f1419;font-family:'Pretendard','Noto Sans CJK KR','Malgun Gothic',sans-serif;
display:flex;flex-direction:column;justify-content:center;padding:0 90px;box-sizing:border-box;color:#e7e9ea}
.src{font-size:26px;color:#8b98a5;margin-bottom:28px;letter-spacing:.5px}
.t{font-size:54px;font-weight:800;line-height:1.3;word-break:keep-all}
.sub{font-size:28px;color:#aab8c2;margin-top:34px;line-height:1.5;word-break:keep-all}
.bar{width:90px;height:8px;background:%(accent)s;border-radius:4px;margin-bottom:34px}
</style></head><body><div class="bar"></div><div class="src">%(src)s</div>
<div class="t">%(title)s</div><div class="sub">%(sub)s</div></body></html>"""


def card(title, publisher, sub, name, accent="#f0b90b"):
    """제목 카드 — 캡처가 전부 실패해도 이미지 없는 글은 안 나가게."""
    from playwright.sync_api import sync_playwright
    path = _path(name)
    page = CARD_HTML % {"accent": accent, "title": html.escape(title[:90]),
                        "sub": html.escape(sub[:140]),
                        "src": html.escape("%s · %s" % (publisher, datetime.now(KST).strftime("%Y.%m.%d %H:%M")))}
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1200, "height": 675})
        pg.set_content(page)
        pg.wait_for_timeout(300)
        pg.screenshot(path=path)
        b.close()
    return path


def _headline(item):
    lines = [l.strip() for l in (item.get("preview_title") or item["text"] or "").split("\n") if l.strip()]
    t = (lines[0] if lines else "").strip("[]")
    return t, (lines[1] if len(lines) > 1 else "")


def image_for(item, draft_id, used_hashes=()):
    """(경로, 해시). 원문 캡처 → 원문 사진 → 제목 카드."""
    base = "d%d_%s_%d" % (draft_id, item["source"], item["post_id"] % 10 ** 9)
    tries = []
    if item["source"] == "news":
        url = (item.get("links") or [item["url"]])[0]
        tries.append(lambda: screenshot_article(url, base + "_cap.png", item.get("preview_title") or _headline(item)[0]))
        if item["photos"]:
            tries.append(lambda: download(item["photos"][0], base + "_img.jpg"))
        from .news import og_image

        def og():
            u = og_image(url)
            if not u:
                raise ValueError("og 없음")
            return download(u, base + "_og.jpg")
        tries.append(og)
    else:
        tries.append(lambda: screenshot_post(item["source"], item["post_id"], base + "_tg.png"))
        if item["photos"]:
            tries.append(lambda: download(item["photos"][0], base + "_img.jpg"))
    for t in tries:
        try:
            path = t()
            h = file_hash(path)
            if h in used_hashes:
                print("[capture] 최근에 쓴 이미지라 건너뜀")
                continue
            return path, h
        except Exception as e:
            print("[capture] 실패:", str(e)[:120])
    title, sub = _headline(item)
    path = card(title, item.get("publisher") or item["source"], sub, base + "_card.png")
    return path, file_hash(path)
