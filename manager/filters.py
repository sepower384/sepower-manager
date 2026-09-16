# -*- coding: utf-8 -*-
"""거르기·점수 — 광고/쉴링/인플루언서 품앗이 제거, 중복 지문, 주제 점수, 고래·청산 파싱."""
import hashlib
import math
import re
from datetime import datetime, timezone

URL_RE = re.compile(r"https?://\S+")
TG_LINK_RE = re.compile(r"(t\.me/|telegram\.me/|@[A-Za-z0-9_]{5,})")
TICKER_RE = re.compile(r"\$[A-Z]{2,10}\b")


def norm(text):
    t = URL_RE.sub(" ", text or "")
    t = re.sub(r"[^0-9A-Za-z가-힣]", "", t).lower()
    return t


def fingerprint(text):
    """앞 60자 정규화 해시 — 같은 속보가 여러 채널에 퍼져도 한 번만."""
    n = norm(text)[:60]
    return hashlib.md5(n.encode("utf-8")).hexdigest()[:16] if n else ""


def grams(text, n=3):
    t = norm(text)
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def similar(a, b, thr=0.45):
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return False
    return len(ga & gb) / min(len(ga), len(gb)) >= thr


def has_word(text, w):
    """영문(ASCII) 키워드는 단어 경계로, 한글은 부분일치로."""
    if w.isascii() and re.match(r"^[A-Za-z]", w):
        return re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(w), text) is not None
    return w in text


def block_reason(item, cfg):
    """거를 이유(문자열) 또는 None."""
    b = cfg["block"]
    text = item["text"] or ""
    if item.get("has_video") and len(text) < b["min_text_len"]:
        return "영상만"
    if len(norm(text)) < b["min_text_len"] and not item["photos"]:
        return "너무 짧음"
    strong = [w for w in b["strong"] if has_word(text, w)]
    if strong:
        return "광고:" + strong[0]
    weak = [w for w in b["weak"] if has_word(text, w)]
    if len(weak) >= 2:
        return "광고:" + "+".join(weak[:2])
    for w in b["shill_words"]:
        if w in text:
            return "쉴링:" + w
    # 인플루언서끼리 채널 소개/품앗이: 다른 텔레 채널 링크·멘션이 본문의 핵심인 글
    tg = TG_LINK_RE.findall(text)
    if len(tg) >= 2 or (tg and len(norm(text)) < 80):
        return "채널홍보/품앗이"
    # 티커 나열형 알파콜
    if len(set(TICKER_RE.findall(text))) >= 3 and "비트코인" not in text:
        return "티커 나열"
    return None


def topic_hits(text, cfg):
    hits = {}
    for topic, words in cfg["topics"].items():
        c = sum(1 for w in words if w.lower() in (text or "").lower())
        if c:
            hits[topic] = c
    return hits


def score(item, cfg, weight=1.0, now=None):
    """시의성(최근) × 주제 적합도 × 채널 가중치 × 조회수."""
    now = now or datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
        age_h = max(0.0, (now - dt).total_seconds() / 3600)
    except ValueError:
        age_h = 12
    fresh = math.exp(-age_h / 6)          # 6시간마다 1/e
    hits = topic_hits(item["text"], cfg)
    topical = 0.3 + min(2.0, sum(hits.values()) * 0.4)
    if item.get("source") == "news":
        views = 0.6                      # 뉴스는 조회수가 없어서 텔레 채널 평균치(≈조회 250)로 둔다
    else:
        views = math.log10(10 + (item.get("views") or 0)) / 4
    media = 1.15 if item["photos"] else 1.0
    return round(fresh * topical * weight * (0.6 + views) * media, 4)


# ───────────────────────────── 고래 입출금 (코인갤러리 포맷)
WHALE_RE = re.compile(r"대략\s*([\d,\.]+)\s*#?([A-Z]{2,6})\s*::\s*\(([\d,\.]+)\s*(조|억)")


ROUTE_RE = re.compile(r"^\W*(.+?)\s*에서\s*(.+?)\s*으?로")
UNKNOWN = "알 수 없는 지갑"


def parse_whale(text):
    m = WHALE_RE.search(text or "")
    r = ROUTE_RE.search((text or "").split("\n")[0])
    if not m or not r:
        return None
    amount, coin, krw, unit = m.groups()
    eok = float(krw.replace(",", "")) * (10000 if unit == "조" else 1)
    src, dst = (x.replace("#", "").strip() for x in r.groups())
    if src == dst and src != UNKNOWN:
        direction = "internal"          # 거래소 내부 지갑 정리 — 의미 없음
    elif src == UNKNOWN and dst != UNKNOWN:
        direction = "in"
    elif src != UNKNOWN and dst == UNKNOWN:
        direction = "out"
    elif src != UNKNOWN and dst != UNKNOWN:
        direction = "exchange"
    else:
        direction = "move"
    return {"coin": coin, "amount": amount, "eok": eok, "src": src, "dst": dst,
            "route": "%s → %s" % (src, dst), "direction": direction}


# ───────────────────────────── 대형 청산
LIQ_RE = re.compile(r"#(\w+?)(USDT|USD|PERP)?\s+Liquidated\s+(LONG|SHORT)\s+at price \$([\d,\.]+)\.\s*Total:\s*\$([\d,\.]+)\s+on\s+(\w+)", re.I)


def parse_liq(text):
    m = LIQ_RE.search(text or "")
    if not m:
        return None
    sym, _, side, price, total, ex = m.groups()
    return {"symbol": sym.upper(), "side": side.upper(), "price": price,
            "usd": float(total.replace(",", "")), "exchange": ex}
