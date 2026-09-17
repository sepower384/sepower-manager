# -*- coding: utf-8 -*-
"""주간·월간·분기·연간 보고서 — HTML(A4) → PDF, 강회장 DM 으로 전송.

재료
  - 채널에 실제로 나간 글: state 브랜치 archive/YYYY-MM.jsonl
  - 시장 데이터: market.collect (BTC·ETH·SOL·S&P·나스닥·코스피·엔비디아·DXY·10년물·금·유가·공포탐욕)
  - 하위 보고서 요약: 월간 ← 주간들, 분기·연간 ← 월간들 (reports/<kind>/<key>.json)
만든 요약 JSON 은 state 브랜치에 남기고, 완료 여부는 reports/done.json 으로 관리한다.
"""
import base64
import html
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import filters, ledger, market, store, telegram, writer
from .store import KST, ROOT

OUT_DIR = os.path.join(ROOT, "data", "reports")
COVER_DIR = os.path.join(ROOT, "assets", "covers")

KINDS = {
    "weekly": {"name": "주간", "en": "WEEKLY", "issues": 5, "accent": "#F0B90B"},
    "monthly": {"name": "월간", "en": "MONTHLY", "issues": 6, "accent": "#38BDF8"},
    "quarterly": {"name": "분기", "en": "QUARTERLY", "issues": 7, "accent": "#A78BFA"},
    "yearly": {"name": "연간", "en": "ANNUAL", "issues": 8, "accent": "#F472B6"},
}
# 언제 만드나 (KST): (조건, 시각)
DUE = {
    "weekly": lambda d: d.weekday() == 0, "monthly": lambda d: d.day == 1,
    "quarterly": lambda d: d.day == 1 and d.month in (1, 4, 7, 10), "yearly": lambda d: d.day == 1 and d.month == 1,
}
DUE_HOUR = {"weekly": 8, "monthly": 9, "quarterly": 10, "yearly": 11}
CATCH_UP_DAYS = 3


# ───────────────────────────────────────────── 기간
def period(kind, now):
    """now(KST) 기준 '직전' 완결 기간 [start, end), 키, 표시 이름."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "weekly":
        end = today - timedelta(days=today.weekday())
        start = end - timedelta(days=7)
        y, w, _ = start.isocalendar()
        return start, end, "%d-W%02d" % (y, w), "%d년 %d주차 (%s ~ %s)" % (
            y, w, start.strftime("%m.%d"), (end - timedelta(days=1)).strftime("%m.%d"))
    if kind == "monthly":
        end = today.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
        return start, end, start.strftime("%Y-%m"), "%d년 %d월" % (start.year, start.month)
    if kind == "quarterly":
        qm = ((today.month - 1) // 3) * 3 + 1
        end = today.replace(month=qm, day=1)
        s = end - timedelta(days=1)
        start = s.replace(month=((s.month - 1) // 3) * 3 + 1, day=1)
        q = (start.month - 1) // 3 + 1
        return start, end, "%d-Q%d" % (start.year, q), "%d년 %d분기" % (start.year, q)
    end = today.replace(month=1, day=1)
    start = end.replace(year=end.year - 1)
    return start, end, str(start.year), "%d년" % start.year


def to_date(start, end, now):
    """아직 안 끝난 기간(샘플용): 이번 기간 시작 ~ 지금."""
    return start, now


def done_map():
    raw = ledger.read_state("reports/done.json")
    return json.loads(raw) if raw.strip() else {}


def due_reports(now):
    """지금 만들어야 하는 (kind, start, end, key, label) 목록."""
    done = done_map()
    out = []
    for kind in KINDS:
        for back in range(CATCH_UP_DAYS + 1):
            day = now - timedelta(days=back)
            if not DUE[kind](day):
                continue
            if back == 0 and now.hour < DUE_HOUR[kind]:
                continue
            start, end, key, label = period(kind, day.replace(hour=23))
            if "%s:%s" % (kind, key) not in done:
                out.append((kind, start, end, key, label))
            break
    return out


# ───────────────────────────────────────────── 재료
def child_summaries(kind, start, end):
    sub = {"monthly": "weekly", "quarterly": "monthly", "yearly": "monthly"}.get(kind)
    if not sub:
        return []
    keys = {}
    if sub == "weekly":             # 기간에 걸친 모든 주(월요일 시작)
        cur = start - timedelta(days=start.weekday())
        while cur < end:
            y, w, _ = cur.isocalendar()
            keys["%d-W%02d" % (y, w)] = "%d주차(%s~)" % (w, cur.strftime("%m.%d"))
            cur += timedelta(days=7)
    else:                           # 기간 안의 모든 달
        cur = start.replace(day=1)
        while cur < end:
            keys[cur.strftime("%Y-%m")] = "%d월" % cur.month
            cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    out = []
    for k, label in keys.items():
        raw = ledger.read_state("reports/%s/%s.json" % (sub, k))
        if raw.strip():
            s = json.loads(raw)
            out.append({"key": k, "label": label, "title": s.get("title", ""), "summary": s.get("summary", []),
                        "issues": [i.get("title", "") for i in s.get("issues", [])]})
    return out


def post_stats(posts, cfg):
    ins = [p for p in posts if p.get("kind", "insight") == "insight"]
    return {
        "total": len(posts),
        "insight": len(ins),
        "alerts": sum(1 for p in posts if p.get("kind") in ("whale", "liquidation")),
        "briefs": sum(1 for p in posts if p.get("kind") == "brief"),
        "crypto_share": round(100 * sum(1 for p in ins if filters.is_crypto(p.get("text", ""), cfg)) / len(ins)) if ins else 0,
        "types": Counter(p.get("ptype") or "?" for p in ins).most_common(),
        "days": len({p["at"][:10] for p in posts}),
    }


def fallback_news(db, start, end, limit=80):
    """발행 기록이 거의 없을 때(시작 직후) 수집된 뉴스 제목으로 보충."""
    utc = timezone.utc
    rows = store.items_where(db, "kind IN ('news','insight') AND status!='blocked' AND date>=? AND date<? "
                                 "ORDER BY date DESC LIMIT ?",
                             (start.astimezone(utc).isoformat(), end.astimezone(utc).isoformat(), limit * 4))
    seen, out = [], []
    for r in rows:
        head = r["text"].split("\n")[0][:140]
        if any(filters.similar(head, s) for s in seen):
            continue
        seen.append(head)
        out.append("- [%s] %s" % (r.get("publisher") or r["source"], head))
        if len(out) >= limit:
            break
    return out


# ───────────────────────────────────────────── LLM 요약
def build_prompt(kind, label, posts, mkt, children, news_lines, cfg):
    k = KINDS[kind]
    lines = []
    for p in posts[-160:]:
        if p.get("kind") in ("whale", "liquidation"):
            continue
        lines.append("### %s\n%s" % (p["at"][:16].replace("T", " "), (p.get("text") or p.get("head", ""))[:450]))
    child = "\n".join("- %s: %s / %s / 이슈: %s" % (c["label"], c["title"], " ".join(c["summary"]),
                                                   ", ".join(c["issues"])) for c in children) or "(없음)"
    flow_rule = ('"flow": 하위 기간(%s)별 한 줄 흐름 [{"label":"기간","text":"한 줄"}],'
                 % ("주차" if kind == "monthly" else "월")) if kind != "weekly" else '"flow": [],'
    return (writer.rules() +
            "\n\n너는 이제 채널 글이 아니라 강회장에게 드리는 **%s 보고서**를 쓴다. 대상 기간: %s.\n"
            "말투는 개조식 음슴체(~임/~함/~중). 인사이트 깊이 규칙을 그대로 적용하고, 사실·숫자는 아래 재료에 있는 것만 쓴다.\n"
            "크립토 비중을 충분히(절반 이상) 두고, 거시·반도체/AI 와의 연결을 보여줘라.\n\n"
            "[시장 데이터 — 기간 수익률]\n%s\n\n"
            "[하위 기간 보고서 요약]\n%s\n\n"
            "[이 기간 채널에 나간 글 %d개]\n%s\n\n"
            "[같은 기간 수집 뉴스 제목(보충)]\n%s\n\n"
            "[출력] JSON 객체 하나만 (코드블록 없이):\n"
            '{"title": "기간을 관통하는 제목(22자 이내)", "subtitle": "부제 한 줄(40자 이내)",\n'
            ' "summary": ["핵심 한 줄", "핵심 한 줄", "핵심 한 줄"],\n'
            ' "issues": [{"tag": "크립토|거시|AI·반도체|규제|국내", "title": "이슈 제목", "what": "무슨 일(2문장)", '
            '"why": "왜 중요한지 — 2차효과·연결·과거비교(2~3문장)", "next": "다음에 볼 것(1문장)"}]  ← 정확히 %d개, 중요도 순,\n'
            ' "sections": {"crypto": "크립토 총평 4~6문장", "macro": "거시·금리·달러 총평 3~5문장", "ai": "반도체·AI·미국주식 총평 3~5문장"},\n'
            ' %s\n'
            ' "watch": [{"when": "날짜나 시기", "what": "볼 일정/지표", "why": "왜"}]  ← 다음 기간 체크포인트 5개,\n'
            ' "view": "세력 관점 — 이 기간이 사이클에서 어떤 의미인지, 다음 기간 시나리오 2갈래 포함 5~7문장. 매수·매도 권유 금지",\n'
            ' "keywords": ["키워드 8개"]}\n'
            % (k["name"], label, market.table_text(mkt), child, len(lines), "\n\n".join(lines) or "(없음)",
               "\n".join(news_lines) or "(없음)", k["issues"], flow_rule))


def summarize(kind, label, posts, mkt, children, news_lines, cfg):
    raw = writer.run_claude(build_prompt(kind, label, posts, mkt, children, news_lines, cfg), timeout=600)
    s = raw[raw.find("{"): raw.rfind("}") + 1]
    data = json.loads(s)
    data.setdefault("flow", [])
    return data


# ───────────────────────────────────────────── 차트 (인라인 SVG)
UP, DOWN, INK, MUTED, LINE = "#E5484D", "#3E63DD", "#0F172A", "#64748B", "#E2E8F0"


def _scale(vals, lo, hi):
    mn, mx = min(vals), max(vals)
    if mx == mn:
        mx = mn + 1
    return [hi - (v - mn) / (mx - mn) * (hi - lo) for v in vals], mn, mx


def sparkline(series, w=120, h=34, color=INK):
    vals = [v for _, v in series]
    if len(vals) < 2:
        return ""
    ys, _, _ = _scale(vals, 3, h - 3)
    xs = [i * (w - 4) / (len(vals) - 1) + 2 for i in range(len(vals))]
    pts = " ".join("%.1f,%.1f" % p for p in zip(xs, ys))
    return ('<svg viewBox="0 0 %d %d" width="%d" height="%d"><polyline points="%s" fill="none" '
            'stroke="%s" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/></svg>'
            % (w, h, w, h, pts, color))


def line_chart(series, w=680, h=215, color="#F0B90B", unit="$"):
    vals = [v for _, v in series]
    if len(vals) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 64, 16, 28, 46
    ys, mn, mx = _scale(vals, pad_t, h - pad_b)
    xs = [pad_l + i * (w - pad_l - pad_r) / (len(vals) - 1) for i in range(len(vals))]
    pts = " ".join("%.1f,%.1f" % p for p in zip(xs, ys))
    area = "%s %.1f,%d %.1f,%d" % (pts, xs[-1], h - pad_b, xs[0], h - pad_b)
    grid = ""
    for i in range(5):
        y = pad_t + i * (h - pad_t - pad_b) / 4
        v = mx - i * (mx - mn) / 4
        grid += ('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s"/>'
                 '<text x="%d" y="%.1f" text-anchor="end" font-size="11" fill="%s">%s%s</text>'
                 % (pad_l, w - pad_r, y, y, LINE, pad_l - 8, y + 4, MUTED, unit, format(round(v), ",")))
    labels = ""
    step = max(1, len(series) // 6)
    for i in range(0, len(series), step):
        labels += '<text x="%.1f" y="%d" text-anchor="middle" font-size="11" fill="%s">%s</text>' % (
            xs[i], h - 8, MUTED, series[i][0][5:].replace("-", "."))
    hi_i, lo_i = vals.index(mx), vals.index(mn)
    marks = ""
    for i, tag, col in ((hi_i, "고점", UP), (lo_i, "저점", DOWN)):
        marks += ('<circle cx="%.1f" cy="%.1f" r="4" fill="%s"/><text x="%.1f" y="%.1f" font-size="11" '
                  'font-weight="700" fill="%s" text-anchor="middle">%s %s%s</text>'
                  % (xs[i], ys[i], col, xs[i], ys[i] + (-10 if tag == "고점" else 18), col, tag, unit,
                     format(round(vals[i]), ",")))
    gid = "g" + color.strip("#")
    return ('<svg viewBox="0 0 {w} {h}" width="100%"><defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="{c}" stop-opacity=".35"/><stop offset="1" stop-color="{c}" stop-opacity="0"/>'
            '</linearGradient></defs>{grid}<polygon points="{area}" fill="url(#{gid})"/>'
            '<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="2.6" stroke-linejoin="round"/>'
            '{labels}{marks}</svg>').format(w=w, h=h, c=color, gid=gid, grid=grid, area=area, pts=pts, labels=labels, marks=marks)


def perf_bars(mkt, w=680):
    rows = [(a["name"], a["change"], market.fmt_change(a)) for k, a in mkt.items()
            if not k.startswith("_") and a["change_kind"] == "%"]
    rows.sort(key=lambda r: -r[1])
    if not rows:
        return ""
    m = max(abs(r[1]) for r in rows) or 1
    bh, gap = 17, 6
    h = len(rows) * (bh + gap)
    mid = 360
    half = w - mid - 70
    out = ""
    for i, (name, c, label) in enumerate(rows):
        y = i * (bh + gap)
        bw = abs(c) / m * half
        x = mid if c >= 0 else mid - bw
        col = UP if c >= 0 else DOWN
        out += ('<text x="%d" y="%d" font-size="13" fill="%s" text-anchor="end">%s</text>'
                '<rect x="%.1f" y="%d" width="%.1f" height="%d" rx="4" fill="%s"/>'
                '<text x="%.1f" y="%d" font-size="12.5" font-weight="700" fill="%s" text-anchor="%s">%s</text>'
                % (mid - half - 12, y + 16, INK, html.escape(name), x, y, max(bw, 2), bh, col,
                   (x + bw + 6) if c >= 0 else (x - 6), y + 16, col, "start" if c >= 0 else "end", label))
    out += '<line x1="%d" x2="%d" y1="-4" y2="%d" stroke="%s" stroke-width="1.5"/>' % (mid, mid, h, MUTED)
    return '<svg viewBox="0 -6 %d %d" width="100%%">%s</svg>' % (w, h + 10, out)


def fng_gauge(fng):
    if not fng:
        return ""
    v = fng[-1][1]
    pct = v
    col = "#DC2626" if v < 25 else "#F97316" if v < 45 else "#EAB308" if v < 55 else "#84CC16" if v < 75 else "#16A34A"
    return ('<div class="fng"><div class="fng-bar"><div class="fng-dot" style="left:%d%%;background:%s"></div></div>'
            '<div class="fng-lbl"><span>극단적 공포</span><span>중립</span><span>극단적 탐욕</span></div></div>' % (pct, col))


# ───────────────────────────────────────────── HTML
CSS = """
@page { size: A4; margin: 16mm 16mm 17mm;
  @bottom-left { content: var(--foot); font-family: 'Pretendard', sans-serif; font-size: 7.5pt; color: #94A3B8 }
  @bottom-right { content: counter(page); font-family: 'Pretendard', sans-serif; font-size: 8pt; color: #94A3B8 } }
@page :first { margin: 0; @bottom-left { content: none } @bottom-right { content: none } }
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact }
body { margin: 0; font-family: 'Pretendard', 'Noto Sans CJK KR', 'Malgun Gothic', sans-serif; color: #0F172A;
       font-size: 10.5pt; line-height: 1.62; word-break: keep-all }
.page { position: relative; break-before: page; background: #fff }
.kpi, .card, .issue, .sec, .watch, .view, .stat, tr, .flow div, .summary { break-inside: avoid }
h2, h3 { break-after: avoid }
.foot { display: none }
.eyebrow { font-size: 8.5pt; letter-spacing: .18em; font-weight: 700; color: var(--accent) }
h2 { font-size: 19pt; line-height: 1.25; margin: 1mm 0 4mm; letter-spacing: -.02em }
h3 { font-size: 12.5pt; margin: 0 0 2mm; letter-spacing: -.01em }
.muted { color: #64748B }

/* 표지 */
.cover { width: 210mm; height: 297mm; overflow: hidden; break-before: auto; color: #fff; background: #0B1220 }
.cover .art { position: absolute; inset: 0; background-size: cover; background-position: center }
.cover .shade { position: absolute; inset: 0;
  background: linear-gradient(180deg, rgba(11,18,32,.25) 0%, rgba(11,18,32,.55) 45%, rgba(11,18,32,.97) 78%) }
.cover .in { position: absolute; left: 18mm; right: 18mm; bottom: 26mm }
.cover .brand { position: absolute; top: 16mm; left: 18mm; right: 18mm; display: flex; justify-content: space-between;
  font-size: 9pt; letter-spacing: .2em; font-weight: 700; opacity: .92 }
.cover .kind { display: inline-block; font-size: 10pt; font-weight: 800; letter-spacing: .24em; color: #0B1220;
  background: var(--accent); padding: 1.6mm 4mm; border-radius: 2mm }
.cover h1 { font-size: 36pt; line-height: 1.16; margin: 6mm 0 4mm; letter-spacing: -.03em; font-weight: 800 }
.cover .sub { font-size: 13pt; opacity: .88; margin-bottom: 9mm }
.cover .meta { display: flex; gap: 10mm; font-size: 9.5pt; opacity: .85; border-top: 1px solid rgba(255,255,255,.25);
  padding-top: 4mm }
.cover .meta b { display: block; font-size: 8pt; letter-spacing: .15em; opacity: .7; font-weight: 600 }

/* 요약 */
.summary { background: #0B1220; color: #fff; border-radius: 4mm; padding: 6mm 7mm; margin-bottom: 6mm }
.summary ol { margin: 0; padding-left: 5mm }
.summary li { margin: 1.2mm 0; font-size: 11pt; font-weight: 500 }
.summary li::marker { color: var(--accent); font-weight: 800 }
.kpis { display: grid; grid-template-columns: repeat(3, 1fr); gap: 3mm }
.kpi { border: 1px solid #E2E8F0; border-radius: 3mm; padding: 3mm 3.5mm 2mm; display: flex; flex-direction: column }
.kpi .n { font-size: 8.5pt; color: #64748B; font-weight: 600 }
.kpi .v { font-size: 13.5pt; font-weight: 800; letter-spacing: -.02em; margin-top: .5mm }
.kpi .c { font-size: 10pt; font-weight: 800 }
.kpi svg { margin-top: 1mm }
.up { color: #E5484D } .down { color: #3E63DD }
.fng { margin-top: 2mm }
.fng-bar { position: relative; height: 3mm; border-radius: 2mm;
  background: linear-gradient(90deg, #DC2626, #F97316, #EAB308, #84CC16, #16A34A) }
.fng-dot { position: absolute; top: -1.2mm; width: 5.4mm; height: 5.4mm; border-radius: 50%; border: 1mm solid #fff;
  box-shadow: 0 0 0 .3mm #0F172A; transform: translateX(-50%) }
.fng-lbl { display: flex; justify-content: space-between; font-size: 7.5pt; color: #64748B; margin-top: 1.5mm }
.card { border: 1px solid #E2E8F0; border-radius: 3mm; padding: 4.5mm 5mm; margin-bottom: 4mm }
.chart-card { padding: 4mm 4mm 2mm }

/* 이슈 */
.issue { display: grid; grid-template-columns: 11mm 1fr; gap: 3mm; padding: 2.6mm 0; border-bottom: 1px solid #E2E8F0 }
.issue:last-child { border-bottom: 0 }
.issue .no { font-size: 20pt; font-weight: 800; color: var(--accent); line-height: 1 }
.tag { display: inline-block; font-size: 7.5pt; font-weight: 700; padding: .4mm 2mm; border-radius: 5mm;
  background: #F1F5F9; color: #334155; margin-right: 1.5mm; vertical-align: 1px }
.tag.crypto { background: #FEF3C7; color: #92400E } .tag.macro { background: #DBEAFE; color: #1E40AF }
.tag.ai { background: #EDE9FE; color: #5B21B6 } .tag.reg { background: #FCE7F3; color: #9D174D }
.issue h4 { margin: 0 0 .6mm; font-size: 11pt; letter-spacing: -.01em }
.issue p { margin: .4mm 0; font-size: 9pt; line-height: 1.52 }
.issue p b { color: #0F172A; font-weight: 700; margin-right: 1mm }
.issue .why { background: #F8FAFC; border-left: .8mm solid var(--accent); padding: 1.4mm 2.5mm; border-radius: 0 1.5mm 1.5mm 0 }

/* 분야 */
.sec { display: grid; grid-template-columns: 26mm 1fr; gap: 4mm; padding: 2.8mm 0; font-size: 9.6pt; line-height: 1.55; border-bottom: 1px solid #E2E8F0 }
.sec .h { font-size: 13pt; font-weight: 800 }
.sec .h small { display: block; font-size: 8pt; color: #94A3B8; letter-spacing: .15em; font-weight: 600 }
.chips span { display: inline-block; border: 1px solid #CBD5E1; border-radius: 5mm; padding: .6mm 3mm;
  margin: 0 1.5mm 1.5mm 0; font-size: 9pt; font-weight: 600 }
.flow { display: grid; gap: 2mm }
.flow div { display: grid; grid-template-columns: 26mm 1fr; font-size: 9.6pt }
.flow b { color: var(--accent2) }

/* 체크포인트·관점 */
.watch { display: grid; grid-template-columns: 28mm 1fr; border-bottom: 1px dashed #CBD5E1; padding: 1.8mm 0; font-size: 9.6pt }
.watch .when { font-weight: 800; font-size: 10pt }
.watch .what { font-weight: 700 }
.watch .why { font-size: 9.3pt; color: #475569 }
.view { background: #0B1220; color: #E2E8F0; border-radius: 4mm; padding: 6mm 7mm; margin-top: 4mm; position: relative;
  font-size: 10pt; line-height: 1.68 }
.view:before { content: "“"; position: absolute; top: -3mm; left: 5mm; font-size: 44pt; color: var(--accent);
  font-family: Georgia, serif }
.view .who { margin-top: 3mm; font-size: 8.5pt; letter-spacing: .15em; color: var(--accent); font-weight: 700 }

/* 부록 */
table { width: 100%; border-collapse: collapse; font-size: 8.8pt }
th { text-align: left; font-size: 7.8pt; color: #64748B; letter-spacing: .08em; border-bottom: 1.5px solid #0F172A;
  padding: 1.6mm 1.5mm }
td { border-bottom: 1px solid #EEF2F7; padding: 1.5mm; vertical-align: top }
td.t { white-space: nowrap; color: #64748B; width: 22mm }
td.k { width: 12mm; font-weight: 700; color: var(--accent2) }
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 3mm; margin-bottom: 5mm }
.stat { background: #F8FAFC; border-radius: 3mm; padding: 3mm 4mm }
.stat b { display: block; font-size: 17pt; letter-spacing: -.02em }
.stat span { font-size: 8.5pt; color: #64748B }
"""

TAG_CLASS = {"크립토": "crypto", "거시": "macro", "AI·반도체": "ai", "AI": "ai", "반도체": "ai", "규제": "reg"}


def _e(s):
    return html.escape(str(s or ""))


def _chg_cls(a):
    return "up" if a["change"] >= 0 else "down"


def cover_data_uri(kind):
    for ext in ("jpg", "png", "webp"):
        p = os.path.join(COVER_DIR, "%s.%s" % (kind, ext))
        if os.path.exists(p):
            with open(p, "rb") as f:
                return "data:image/%s;base64,%s" % ("jpeg" if ext == "jpg" else ext, base64.b64encode(f.read()).decode())
    return ""


def render_html(kind, key, label, s, mkt, posts, stats, now):
    k = KINDS[kind]
    accent = k["accent"]
    foot = lambda n: ('<div class="foot"><span>세력 마켓 사이클 연구소 · %s 보고서 %s</span><span>%d</span></div>'
                      % (k["name"], _e(key), n))
    pages = []
    art = cover_data_uri(kind)
    pages.append(
        '<section class="page cover"><div class="art" style="%s"></div><div class="shade"></div>'
        '<div class="brand"><span>SEPOWER MARKET CYCLE LAB</span><span>%s</span></div>'
        '<div class="in"><span class="kind">%s REPORT</span><h1>%s</h1><div class="sub">%s</div>'
        '<div class="meta"><div><b>PERIOD</b>%s</div><div><b>ISSUE</b>%s</div><div><b>PUBLISHED</b>%s</div>'
        '<div><b>POSTS</b>%d건</div></div></div></section>'
        % (("background-image:url(%s)" % art) if art else
           "background:radial-gradient(120%% 80%% at 80%% 10%%, %s55 0%%, transparent 55%%),"
           "radial-gradient(90%% 70%% at 0%% 40%%, #1E3A8A66 0%%, transparent 60%%),#0B1220" % accent,
           _e(key), k["en"], _e(s["title"]), _e(s.get("subtitle", "")), _e(label), _e(key),
           now.strftime("%Y.%m.%d"), stats["total"]))

    # 2. 한눈에 보기
    kpi_keys = ["BTC", "ETH", "SOL", "SPX", "NDX", "KOSPI", "NVDA", "DXY", "US10Y", "GOLD", "WTI"]
    cards = ""
    for kk in kpi_keys:
        a = mkt.get(kk)
        if not a:
            continue
        col = "#E5484D" if a["change"] >= 0 else "#3E63DD"
        cards += ('<div class="kpi"><span class="n">%s</span><span class="v">%s</span>'
                  '<span class="c %s">%s</span>%s</div>' % (_e(a["name"]), market.fmt_value(a), _chg_cls(a),
                                                          market.fmt_change(a), sparkline(a["series"], 150, 30, col)))
    f = mkt.get("_fng") or []
    if f:
        cards += ('<div class="kpi"><span class="n">공포탐욕지수</span><span class="v">%d <small style="font-size:9pt">%s</small></span>'
                  '<span class="c muted">시작 %d → 끝 %d</span>%s</div>' % (f[-1][1], _e(f[-1][2]), f[0][1], f[-1][1], fng_gauge(f)))
    pages.append(
        '<section class="page"><div class="eyebrow">AT A GLANCE</div><h2>%s 한눈에 보기</h2>'
        '<div class="summary"><ol>%s</ol></div><div class="kpis">%s</div>%s</section>'
        % (k["name"], "".join("<li>%s</li>" % _e(x) for x in s.get("summary", [])), cards, foot(2)))

    # 3. 차트
    btc = mkt.get("BTC")
    if btc:
        head = (_chg_cls(btc), market.fmt_change(btc), market.fmt_value(dict(btc, last=btc["first"])),
                market.fmt_value(btc), line_chart(btc["series"], color=accent))
    else:
        head = ("", "", "", "", "데이터 없음")
    eth = mkt.get("ETH")
    eth_card = ('<div class="card chart-card"><h3>이더리움 <span class="%s">%s</span></h3>%s</div>'
                % (_chg_cls(eth), market.fmt_change(eth), line_chart(eth["series"], h=150, color="#6366F1"))) if eth else ""
    pages.append(
        ('<section class="page"><div class="eyebrow">MARKET</div><h2>가격 흐름과 자산별 성과</h2>'
         '<div class="card chart-card"><h3>비트코인 <span class="%s">%s</span> <span class="muted" style="font-size:9pt;font-weight:500">%s → %s</span></h3>%s</div>'
         % head) + eth_card +
        '<div class="card chart-card"><h3>자산별 기간 수익률</h3><div class="muted" style="font-size:8.5pt;margin-bottom:2mm">'
        '빨강 상승 · 파랑 하락 (미 10년물은 한눈에 보기에서 bp로 표시)</div>%s</div>%s</section>' % (perf_bars(mkt), foot(3)))

    # 4. 핵심 이슈
    issues = ""
    for i, it in enumerate(s.get("issues", []), 1):
        tag = it.get("tag", "")
        issues += ('<div class="issue"><div class="no">%02d</div><div><h4><span class="tag %s">%s</span>%s</h4>'
                   '<p><b>무슨 일</b>%s</p><p class="why"><b>왜 중요</b>%s</p><p><b>다음</b>%s</p></div></div>'
                   % (i, TAG_CLASS.get(tag, ""), _e(tag), _e(it.get("title")), _e(it.get("what")),
                      _e(it.get("why")), _e(it.get("next"))))
    pages.append('<section class="page"><div class="eyebrow">KEY ISSUES</div><h2>핵심 이슈 TOP %d</h2>%s%s</section>'
                 % (len(s.get("issues", [])), issues, foot(4)))

    # 5. 분야별 + 흐름
    sec = s.get("sections", {})
    flow = ""
    if s.get("flow"):
        flow = ('<div class="card" style="margin-top:5mm"><h3>기간 흐름</h3><div class="flow">%s</div></div>'
                % "".join("<div><b>%s</b><span>%s</span></div>" % (_e(x.get("label")), _e(x.get("text"))) for x in s["flow"]))
    chips = '<div class="chips" style="margin-top:4mm">%s</div>' % "".join(
        "<span>#%s</span>" % _e(x) for x in s.get("keywords", []))
    watch = "".join('<div class="watch"><div class="when">%s</div><div><div class="what">%s</div><div class="why">%s</div></div></div>'
                    % (_e(w.get("when")), _e(w.get("what")), _e(w.get("why"))) for w in s.get("watch", []))
    nxt = {"weekly": "다음 주", "monthly": "다음 달", "quarterly": "다음 분기", "yearly": "내년"}[kind]
    sectors = ('<div class="sec"><div class="h">크립토<small>CRYPTO</small></div><div>%s</div></div>'
               '<div class="sec"><div class="h">거시·금리<small>MACRO</small></div><div>%s</div></div>'
               '<div class="sec"><div class="h">반도체·AI<small>AI · SEMIS</small></div><div>%s</div></div>'
               % (_e(sec.get("crypto")), _e(sec.get("macro")), _e(sec.get("ai"))))
    view = '<div class="view">%s<div class="who">— 세력 관점 · SEPOWER VIEW</div></div>' % _e(s.get("view"))
    if flow:   # 월간 이상: 분야별+흐름 / 체크포인트+관점 두 쪽
        pages.append('<section class="page"><div class="eyebrow">BY SECTOR</div><h2>분야별 정리</h2>%s%s%s%s</section>'
                     % (sectors, flow, chips, foot(5)))
        pages.append('<section class="page"><div class="eyebrow">WHAT\'S NEXT</div><h2>%s 체크포인트</h2>%s%s%s</section>'
                     % (nxt, watch, view, foot(6)))
    else:      # 주간: 한 쪽에 모아 빈 공간 없이
        pages.append('<section class="page"><div class="eyebrow">BY SECTOR · WHAT\'S NEXT</div><h2>분야별 정리</h2>%s%s'
                     '<h3 style="margin-top:6mm">%s 체크포인트</h3>%s%s%s</section>'
                     % (sectors, chips, nxt, watch, view, foot(5)))

    # 7. 부록
    rows = ""
    for p in [p for p in posts if p.get("kind", "insight") != "liquidation"][-45:]:
        rows += '<tr><td class="t">%s</td><td class="k">%s</td><td>%s</td></tr>' % (
            p["at"][5:16].replace("T", " ").replace("-", "."),
            _e({"whale": "고래", "brief": "브리핑"}.get(p.get("kind"), p.get("ptype") or "")),
            _e((p.get("head") or "")[:90]))
    types = " · ".join("%s %d" % (t, n) for t, n in stats["types"][:8]) or "-"
    pages.append(
        '<section class="page"><div class="eyebrow">APPENDIX</div><h2>채널 운영 기록</h2>'
        '<div class="stats"><div class="stat"><b>%d</b><span>발행 글</span></div>'
        '<div class="stat"><b>%d</b><span>인사이트 글</span></div>'
        '<div class="stat"><b>%d%%</b><span>크립토 비중</span></div>'
        '<div class="stat"><b>%d</b><span>알림(고래·청산)</span></div></div>'
        '<div class="muted" style="font-size:8.5pt;margin-bottom:3mm">글 유형 분포: %s</div>'
        '<table><tr><th>시각</th><th>유형</th><th>제목</th></tr>%s</table>%s</section>'
        % (stats["total"], stats["insight"], stats["crypto_share"], stats["alerts"], _e(types),
           rows or '<tr><td colspan="3" class="muted">이 기간 발행 기록 없음(운영 시작 전)</td></tr>', foot(len(pages) + 1)))

    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">'
            '<style>:root{--accent:%s;--accent2:%s}%s</style></head><body>%s</body></html>'
            % (accent, "#B45309" if kind == "weekly" else "#0369A1" if kind == "monthly" else "#6D28D9"
               if kind == "quarterly" else "#BE185D",
               CSS.replace("var(--foot)", '"세력 마켓 사이클 연구소 · %s 보고서 %s"' % (k["name"], key)),
               "".join(pages)))


def to_pdf(html_text, path):
    from playwright.sync_api import sync_playwright
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.set_content(html_text, wait_until="networkidle", timeout=60000)
        pg.evaluate("() => document.fonts.ready")
        pg.pdf(path=path, format="A4", print_background=True, margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        b.close()
    return path


# ───────────────────────────────────────────── 실행
def build(kind, start, end, key, label, db, cfg, now=None, sample=False):
    now = now or store.now_kst()
    posts = ledger.load_archive(start, end)
    mkt = market.collect(start.astimezone(), end.astimezone())
    children = child_summaries(kind, start, end)
    news_lines = fallback_news(db, start, end) if len([p for p in posts if p.get("kind") == "insight"]) < 15 else []
    s = summarize(kind, label, posts, mkt, children, news_lines, cfg)
    stats = post_stats(posts, cfg)
    doc = render_html(kind, key, label, s, mkt, posts, stats, now)
    name = "세력_%s보고서_%s%s" % (KINDS[kind]["name"], key, "_샘플" if sample else "")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, name + ".html"), "w", encoding="utf-8") as f:
        f.write(doc)
    pdf = to_pdf(doc, os.path.join(OUT_DIR, name + ".pdf"))
    return pdf, s, stats


def deliver(kind, key, label, pdf, s, stats):
    cap = "📑 %s 보고서 · %s\n\n%s\n\n%s" % (
        KINDS[kind]["name"], label, s.get("title", ""), "\n".join("· " + x for x in s.get("summary", [])))
    telegram.send_document(telegram.admin_chat(), pdf, cap[:1000])


def save(kind, key, s, now):
    def update(read):
        raw = read("reports/done.json")
        done = json.loads(raw) if raw.strip() else {}
        done["%s:%s" % (kind, key)] = now.isoformat()
        return {"reports/%s/%s.json" % (kind, key): json.dumps(s, ensure_ascii=False, indent=1),
                "reports/done.json": json.dumps(done, ensure_ascii=False, indent=1)}
    ledger.commit_files(update, "%s 보고서 %s" % (kind, key))


def run_due(db, cfg, now=None, log=print):
    now = now or store.now_kst()
    made = []
    for kind, start, end, key, label in due_reports(now):
        log("보고서 생성: %s %s" % (kind, key))
        try:
            pdf, s, stats = build(kind, start, end, key, label, db, cfg, now)
            deliver(kind, key, label, pdf, s, stats)
            save(kind, key, s, now)
            made.append((kind, key))
        except Exception as e:
            log("보고서 실패 %s %s: %s" % (kind, key, e))
            telegram.send(telegram.admin_chat(), "⚠️ %s 보고서(%s) 생성 실패: %s" % (KINDS[kind]["name"], key, str(e)[:200]))
    return made


def sample(kind, db, cfg, now=None):
    """진행 중인 기간으로 미리보기(저장·완료처리 안 함)."""
    now = now or store.now_kst()
    start, end, key, label = period(kind, now + {"weekly": timedelta(days=7), "monthly": timedelta(days=32),
                                                 "quarterly": timedelta(days=95), "yearly": timedelta(days=366)}[kind])
    if kind == "weekly":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
        label = "최근 7일 (%s ~ %s)" % (start.strftime("%m.%d"), now.strftime("%m.%d"))
    return build(kind, start, now, key, label + " · 샘플", db, cfg, now, sample=True) + (key, label)
