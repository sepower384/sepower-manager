# -*- coding: utf-8 -*-
"""보고서용 시장 데이터 — 기간 수익률과 일별 시계열. API 키 없음(CoinGecko·Yahoo·alternative.me)."""
from datetime import datetime, timedelta, timezone

import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126 Safari/537.36"}

# (키, 표시 이름, 출처, 심볼, 단위)
ASSETS = [
    ("BTC", "비트코인", "cg", "bitcoin", "$"),
    ("ETH", "이더리움", "cg", "ethereum", "$"),
    ("SOL", "솔라나", "cg", "solana", "$"),
    ("SPX", "S&P 500", "yf", "^GSPC", ""),
    ("NDX", "나스닥", "yf", "^IXIC", ""),
    ("KOSPI", "코스피", "yf", "^KS11", ""),
    ("NVDA", "엔비디아", "yf", "NVDA", "$"),
    ("DXY", "달러인덱스", "yf", "DX-Y.NYB", ""),
    ("US10Y", "미 10년물", "yf", "^TNX", "%"),
    ("GOLD", "금", "yf", "GC=F", "$"),
    ("WTI", "WTI 유가", "yf", "CL=F", "$"),
]


def _cg(coin, start, end):
    days = min(365, max(2, (datetime.now(timezone.utc) - start).days + 2))   # 무료 API 는 365일까지
    params = {"vs_currency": "usd", "days": days}
    if days > 90:
        params["interval"] = "daily"
    r = requests.get("https://api.coingecko.com/api/v3/coins/%s/market_chart" % coin,
                     params=params, headers=H, timeout=20)
    r.raise_for_status()
    pts = [(datetime.fromtimestamp(t / 1000, timezone.utc), v) for t, v in r.json()["prices"]]
    return _daily(pts, start, end)


def _yf(sym, start, end):
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/" + sym,
                     params={"period1": int((start - timedelta(days=5)).timestamp()),
                             "period2": int(end.timestamp()), "interval": "1d"},
                     headers=H, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    closes = res["indicators"]["quote"][0]["close"]
    pts = [(datetime.fromtimestamp(t, timezone.utc), c) for t, c in zip(res["timestamp"], closes) if c is not None]
    return _daily(pts, start - timedelta(days=5), end)


def _daily(pts, start, end):
    """하루에 마지막 값 하나씩, [start, end) 안만."""
    by_day = {}
    for t, v in pts:
        if start <= t < end:
            by_day[t.date()] = v
    return [(d.isoformat(), by_day[d]) for d in sorted(by_day)]


def fear_greed(start, end):
    days = max(2, (datetime.now(timezone.utc) - start).days + 2)
    r = requests.get("https://api.alternative.me/fng/", params={"limit": days}, timeout=15)
    rows = []
    for d in r.json()["data"]:
        t = datetime.fromtimestamp(int(d["timestamp"]), timezone.utc)
        if start <= t < end:
            rows.append((t.date().isoformat(), int(d["value"]), d["value_classification"]))
    return sorted(rows)


def collect(start, end):
    """{키: {name, unit, series:[(날짜, 값)], first, last, change}} + fng."""
    out = {}
    for key, name, src, sym, unit in ASSETS:
        try:
            s = _cg(sym, start, end) if src == "cg" else _yf(sym, start, end)
        except Exception as e:
            print("[market] %s 실패: %s" % (key, str(e)[:80]))
            continue
        if len(s) < 2:
            continue
        # 주식은 시작 전 마지막 종가를 기준점으로 (주말 시작 보정)
        base = s[0][1]
        inside = [p for p in s if p[0] >= start.date().isoformat()] or s
        first = base if src == "yf" else inside[0][1]
        last = inside[-1][1]
        chg = (last - first) if key == "US10Y" else (last / first - 1) * 100
        out[key] = {"name": name, "unit": unit, "series": inside, "first": first, "last": last,
                    "change": chg, "change_kind": "bp" if key == "US10Y" else "%"}
    try:
        out["_fng"] = fear_greed(start, end)
    except Exception as e:
        print("[market] 공포탐욕 실패:", e)
        out["_fng"] = []
    return out


def fmt_value(a):
    v = a["last"]
    if a["unit"] == "%":
        return "%.2f%%" % v
    if v >= 1000:
        return "%s%s" % (a["unit"], format(round(v), ","))
    return "%s%.2f" % (a["unit"], v)


def fmt_change(a):
    c = a["change"]
    if a["change_kind"] == "bp":
        return "%+.0fbp" % (c * 100)
    return "%+.1f%%" % c


def table_text(m):
    """LLM 에 줄 요약표."""
    lines = []
    for k, a in m.items():
        if k.startswith("_"):
            continue
        lines.append("- %s: %s (기간 %s, 시작 %s)" % (a["name"], fmt_value(a), fmt_change(a),
                                                  fmt_value(dict(a, last=a["first"]))))
    f = m.get("_fng") or []
    if f:
        lines.append("- 공포탐욕지수: 시작 %d → 끝 %d(%s), 최저 %d, 최고 %d" % (
            f[0][1], f[-1][1], f[-1][2], min(x[1] for x in f), max(x[1] for x in f)))
    return "\n".join(lines)
