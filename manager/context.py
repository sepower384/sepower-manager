# -*- coding: utf-8 -*-
"""깊은 글을 위한 재료 — 기사 본문, 시장 스냅샷. 실패하면 빈 값(글쓰기는 계속)."""
import re

from bs4 import BeautifulSoup

from . import news

BODY_MAX = 2500


def _text_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        t.decompose()
    root = soup.find("article") or soup
    paras = [re.sub(r"\s+", " ", p.get_text(" ")).strip() for p in root.find_all(["p", "li"])]
    paras = [p for p in paras if len(p) >= 40 and "©" not in p and "무단" not in p]
    return "\n".join(paras)[:BODY_MAX]


def _resolve_google(url):
    """구글뉴스 링크는 JS 리다이렉트 → 브라우저로 원문 HTML 확보."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(locale="ko-KR")
        pg.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            pg.wait_for_url(lambda u: "news.google.com" not in u, timeout=15000)
            pg.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        html, final = pg.content(), pg.url
        b.close()
    return html, final


def article_text(url):
    if not url or "t.me/" in url:
        return ""
    try:
        if "news.google.com" in url:
            html, _ = _resolve_google(url)
        else:
            r = news.get(url, timeout=12)
            if r.status_code != 200:
                return ""
            html = r.text
        return _text_from_html(html)
    except Exception as e:
        print("[context] 본문 실패:", url[:80], e)
        return ""


def market_snapshot():
    out = []
    try:
        r = news.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana"
                     "&vs_currencies=usd&include_24hr_change=true", timeout=10).json()
        for k, name in (("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")):
            if k in r:
                out.append("%s $%s (24h %+.1f%%)" % (name, format(round(r[k]["usd"]), ","), r[k]["usd_24h_change"]))
    except Exception as e:
        print("[context] 시세 실패:", e)
    try:
        r = news.get("https://api.alternative.me/fng/?limit=2", timeout=10).json()["data"]
        out.append("공포탐욕지수 %s(%s), 전일 %s" % (r[0]["value"], r[0]["value_classification"], r[1]["value"]))
    except Exception as e:
        print("[context] 공포탐욕 실패:", e)
    return " / ".join(out)
