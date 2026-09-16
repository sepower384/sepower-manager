# -*- coding: utf-8 -*-
"""원천 뉴스 수집 — 구글뉴스 검색 RSS + 매체/공식 RSS. API 키 0개.

RSS 파싱·대표이미지 로직은 C:/dev/news-radar/radar/sources.py 에서 가져왔다(실측 검증분).
"""
import hashlib
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"}
MRSS = "{http://search.yahoo.com/mrss/}"
ATOM = "{http://www.w3.org/2005/Atom}"
_IMG_SRC = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)
_get = requests.get


def get(url, timeout=20):
    try:
        return _get(url, headers=HEADERS, timeout=timeout)
    except requests.exceptions.SSLError:
        import urllib3
        urllib3.disable_warnings()
        return _get(url, headers=HEADERS, timeout=timeout, verify=False)


def _t(el):
    return (el.text or "").strip() if el is not None else ""


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    s = re.sub(r"&[a-z#0-9]+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        d = None
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z"):
            try:
                d = datetime.strptime(s.strip(), fmt)
                break
            except ValueError:
                pass
        if d is None:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def rss_image(it, desc_raw=""):
    thumb = ""
    for el in it.iter():
        url = (el.get("url") or el.get("href") or "").strip()
        if not url.startswith("http"):
            continue
        if el.tag == MRSS + "content" and (el.get("medium") == "image" or "image" in (el.get("type") or "") or not el.get("type")):
            return url
        if el.tag == MRSS + "thumbnail" and not thumb:
            thumb = url
        if el.tag == "enclosure" and "image" in (el.get("type") or "") and not thumb:
            thumb = url
    if thumb:
        return thumb
    m = _IMG_SRC.search(desc_raw or "")
    return m.group(1) if m and m.group(1).startswith("http") else ""


def parse_rss(xml_bytes, feed_name):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out = []
    for it in root.findall(".//item") or root.findall(".//" + ATOM + "entry"):
        title = _t(it.find("title")) or _t(it.find(ATOM + "title"))
        link = _t(it.find("link"))
        if not link:
            le = it.find(ATOM + "link")
            link = le.get("href", "") if le is not None else ""
        desc = _t(it.find("description")) or _t(it.find(ATOM + "summary"))
        pub = _t(it.find("pubDate")) or _t(it.find(ATOM + "published")) or _t(it.find(ATOM + "updated"))
        if title:
            out.append({"title": strip_html(title), "url": link, "summary": strip_html(desc)[:500],
                        "published": parse_date(pub), "publisher": _t(it.find("source")) or feed_name,
                        "image": rss_image(it, desc)})
    return out


def google_news(query):
    ko = re.search(r"[가-힣]", query) is not None
    url = "https://news.google.com/rss/search?q=%s&%s" % (
        urllib.parse.quote(query + " when:1d"),
        "hl=ko&gl=KR&ceid=KR:ko" if ko else "hl=en-US&gl=US&ceid=US:en")
    r = get(url)
    if r.status_code != 200:
        return []
    items = parse_rss(r.content, "구글뉴스")
    for i in items:
        head, sep, tail = i["title"].rpartition(" - ")   # "제목 - 매체명"
        if sep and head and len(tail) < 40:
            i["title"], i["publisher"] = head, tail
        i["summary"] = ""                                 # 구글뉴스 요약은 제목 반복
    return items


_META = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*["']([^"']*)["']""")
_GENERIC = re.compile(r"default|logo|favicon|placeholder|/seo/", re.I)


def og_image(url, timeout=8):
    host = urllib.parse.urlparse(url or "").netloc.lower()
    if not host or host.endswith("news.google.com"):
        return ""
    try:
        r = get(url, timeout=timeout)
        page = r.text[:400000] if r.status_code == 200 else ""
    except Exception:
        return ""
    for tag in _META.findall(page):
        a = {k.lower(): v for k, v in _ATTR.findall(tag)}
        if (a.get("property") or a.get("name") or "").lower() in ("og:image", "twitter:image") and a.get("content"):
            img = urllib.parse.urljoin(r.url or url, a["content"].strip())
            if img.startswith("http") and not _GENERIC.search(img):
                return img
    return ""


def to_item(n):
    """뉴스 → 공통 아이템 스키마."""
    key = re.sub(r"\W", "", n["title"].lower())[:80] or n["url"]
    pid = int(hashlib.md5(key.encode("utf-8")).hexdigest()[:12], 16)
    text = n["title"] + ("\n" + n["summary"] if n["summary"] and n["summary"][:30] not in n["title"] else "")
    return {"source": "news", "post_id": pid, "url": n["url"],
            "date": (n["published"] or datetime.now(timezone.utc)).isoformat(),
            "text": text, "links": [n["url"]], "photos": [n["image"]] if n["image"] else [],
            "views": 0, "publisher": n["publisher"], "preview_title": n["title"],
            "has_video": False, "kind": "news"}


def collect(cfg, log=print, hours=8):
    ncfg = cfg["news"]
    cut = datetime.now(timezone.utc) - timedelta(hours=hours)
    out, seen = [], set()

    def add(batch, label):
        n = 0
        for x in batch:
            if x["published"] and x["published"] < cut:
                continue
            it = to_item(x)
            if it["post_id"] in seen:
                continue
            seen.add(it["post_id"])
            out.append(it)
            n += 1
        return n

    for q in ncfg["queries"]:
        try:
            add(google_news(q), q)
        except Exception as e:
            log("[뉴스 실패]", q, e)
        time.sleep(0.3)
    for name, url in ncfg["rss"].items():
        try:
            r = get(url)
            add(parse_rss(r.content, name) if r.status_code == 200 else [], name)
        except Exception as e:
            log("[RSS 실패]", name, e)
    return out
