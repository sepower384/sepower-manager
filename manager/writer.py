# -*- coding: utf-8 -*-
"""글쓰기 — `claude -p` 로 편집·작성 (로컬은 로그인, 클라우드는 CLAUDE_CODE_OAUTH_TOKEN = 구독, API 과금 없음).

한 사이클에 LLM 1회: 후보 목록을 주고 "올릴 만한 것 고르기 + 글쓰기"를 한 번에 시킨다.
문체는 STYLE.md 를 따른다(참고 채널 글은 넣지 않는다).
"""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime

from .store import KST, ROOT

STYLE_PATH = os.path.join(ROOT, "STYLE.md")
CLAUDE_TIMEOUT = 300

ROLE = """너는 텔레그램 채널 '세력'의 운영 매니저 "세력의 매니저"다. 운영자 강회장(유튜브 @r_sepower, BTC 사이클·미국시장·반도체/AI 사이클 인사이트) 대신 채널 글을 쓴다.
아래 문체 가이드를 반드시 따른다.

[추가 규칙]
- 후보는 뉴스·공식발표·데이터 원문이다. 원문에 있는 사실·숫자만 쓴다(날짜·수치·고유명사 그대로). 시간은 한국시간으로.
- 영어 원문은 자연스러운 한국어로.
- 링크는 후보에 적힌 링크 중 1개만 맨 끝에(해시태그 다음 줄). 링크가 news.google.com 이면 생략.
- 광고·에어드랍·특정 코인 홍보성 기사, 알맹이 없는 기사, 최근에 이미 쓴 내용과 겹치는 건 고르지 않는다. 좋은 게 없으면 빈 배열.
"""


def rules():
    with open(STYLE_PATH, encoding="utf-8") as f:
        return ROLE + "\n\n" + f.read()


def available():
    return os.environ.get("MANAGER_NO_LLM") != "1"


def run_claude(prompt, timeout=CLAUDE_TIMEOUT):
    env = dict(os.environ, PYTHONUTF8="1")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    exe = shutil.which("claude") or "claude"
    p = subprocess.run([exe, "-p"], input=prompt, capture_output=True, text=True,
                       encoding="utf-8", timeout=timeout, env=env, creationflags=flags)
    if p.returncode != 0:
        raise RuntimeError("claude -p 실패: %s" % (p.stderr or p.stdout)[:300])
    return p.stdout


def extract_json(s):
    m = re.search(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", s, re.S)
    raw = m.group(1) if m else s[s.find("["): s.rfind("]") + 1] if "[" in s else s
    return json.loads(raw)


def kst(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST).strftime("%m-%d %H:%M KST")
    except ValueError:
        return iso[:16]


def fmt_candidates(cands):
    lines = []
    for i, c in enumerate(cands):
        txt = (c["text"] or c.get("preview_title") or "")[:700]
        lines.append("### #%d  출처=%s  시각=%s  사진=%s\n%s\n링크: %s" % (
            i, c.get("publisher") or c["source"], kst(c["date"]),
            "있음" if c["photos"] else "없음", txt, ", ".join(c["links"][:2]) or "-"))
    return "\n\n".join(lines)


TYPES = {
    "A": "속보 한 줄형", "B": "차트/데이터 코멘트형", "C": "쟁점 정리형(질문→답)",
    "D": "실시간 요약형(⚡📍)", "E": "해설형(짧은 문단)", "F": "일정 체크형",
    "G": "그때 vs 지금 비교형", "H": "역발상형", "I": "연결고리형(A→B→C)",
    "J": "시나리오/체크포인트형", "K": "질문 던지기형",
}
SHORT_TYPES = {"A", "B", "K"}


def allowed_types(recent_types, keep_out=4):
    """최근 keep_out 개 글에 쓴 유형은 이번에 못 씀 (다양하게 섞기)."""
    banned = set(t for t in list(recent_types)[:keep_out] if t)
    ok = [t for t in TYPES if t not in banned and t != "F"]   # F(일정)는 아침 체크포인트 전용
    return ok or [t for t in TYPES if t != "F"]


def build_pick_prompt(cands, recent, n, recent_types=()):
    rec = "\n".join("- " + r.split("\n")[0][:120] for r in list(recent)[:60]) or "- (없음)"
    ok = allowed_types(recent_types)
    want_short = sum(1 for t in list(recent_types)[:3] if t in SHORT_TYPES) == 0
    return (rules() +
            "\n\n[최근 48시간에 이미 다룬 글·기사 — 같은 사건·같은 자료는 매체·언어가 달라도 절대 고르지 말 것]\n" + rec +
            "\n\n[후보]\n" + fmt_candidates(cands) +
            "\n\n[할 일 — 1단계: 고르기] 위 후보에서 채널에 올릴 가치가 큰 소식을 최대 %d개 골라라. "
            "같은 사건을 다룬 후보 여러 개는 하나로 묶어라(refs에 모두 기입). "
            "고른 것끼리 서로 다른 사건이어야 하고, 같은 후보 번호를 두 번 쓰지 말 것.\n"
            "- '같은 사건'은 넓게 본다: 같은 FOMC 의 금리 결정·기자회견·점도표·시장 반응은 전부 한 사건, "
            "같은 법안의 표결·반응·전망도 한 사건. 이런 건 하나의 글로 묶거나 하나만 고른다.\n"
            "- 최근 48시간 목록에 있는 사건의 후속 소식은 '새로운 사실'(새 숫자·새 결정)이 있을 때만 고른다.\n"
            "- 인사이트를 깊게 뽑을 수 있는 소식을 우선한다(단순 가격 등락·단신보다 구조적 변화).\n"
            "- 이번에 쓸 수 있는 유형: %s. 고른 것끼리도 유형이 서로 달라야 함.\n"
            "- %s\n"
            "- angle 에는 '뉴스 요약'이 아니라 '어떤 인사이트로 풀지'(2차효과/과거비교/연결/시나리오/숨은포인트/괴리 중 무엇을 어떻게)를 적어라.\n"
            "출력은 JSON 배열만: "
            '[{"refs":[후보번호,...],"type":"유형문자","angle":"인사이트 각도 한두 줄","why":"고른 이유 한 줄"}]'
            % (n, ", ".join("%s(%s)" % (t, TYPES[t]) for t in ok),
               "최근 짧은 글이 없었으니 1개는 짧은 유형(A/B/K)으로" if want_short
               else "최근 짧은 글이 있었으니 깊은 유형 위주로"))


def build_write_prompt(cands, pick, bodies, snapshot, recent):
    src = []
    for i in pick["refs"]:
        c = cands[i]
        body = bodies.get(i, "")
        src.append("### 출처=%s  시각=%s\n%s\n링크: %s%s" % (
            c.get("publisher") or c["source"], kst(c["date"]), (c["text"] or "")[:1200],
            ", ".join(c["links"][:2]) or "-", ("\n[기사 본문]\n" + body) if body else ""))
    t = pick["type"]
    size = "80~200자로 짧고 날카롭게" if t in SHORT_TYPES else "300~600자로 깊게"
    rec = "\n\n".join(r[:300] for r in recent[:3]) or "(없음)"
    return (rules() +
            "\n\n[현재 시장 스냅샷]\n" + (snapshot or "(없음)") +
            "\n\n[최근 채널 글 — 말투 이어가되 내용·표현 반복 금지]\n" + rec +
            "\n\n[원문]\n" + "\n\n".join(src) +
            "\n\n[할 일 — 2단계: 쓰기] 유형 %s(%s)로, %s 써라.\n"
            "인사이트 각도: %s\n"
            "- '인사이트 깊이' 항목을 반드시 반영(짧은 유형도 마지막 한 줄은 '그래서 왜 중요한지').\n"
            "- 원문·본문·스냅샷에 없는 숫자는 쓰지 말 것. 과거 비교는 확실히 아는 사실만.\n"
            "- 완성된 글 본문만 출력(설명·따옴표·코드블록 없이)." % (t, TYPES.get(t, ""), size, pick.get("angle", "")))


def pick_and_write(cands, recent, n=2, hint="", recent_types=(), enrich=None, recent_posts=None):
    """1단계 고르기 → (본문·시세 보강) → 2단계 쓰기.
    recent: 최근 48시간에 다룬 사건 목록(중복 금지), recent_posts: 말투 참고용 최근 글.
    enrich(refs) -> (bodies, snapshot)"""
    if not cands:
        return []
    posts = recent if recent_posts is None else recent_posts
    prompt = build_pick_prompt(cands, recent, n, recent_types)
    if hint:
        prompt += "\n\n[추가 지시] " + hint
    picks = []
    for d in extract_json(run_claude(prompt))[:n]:
        refs = [int(x) for x in d.get("refs", []) if str(x).isdigit() and int(x) < len(cands)]
        if refs:
            t = str(d.get("type", "E")).strip()[:1].upper()
            picks.append({"refs": refs, "type": t if t in TYPES else "E",
                          "angle": d.get("angle", ""), "why": d.get("why", "")})
    res = []
    for p in picks:
        bodies, snapshot = enrich(p["refs"]) if enrich else ({}, "")
        text = run_claude(build_write_prompt(cands, p, bodies, snapshot, posts)).strip()
        text = re.sub(r"^```\w*\n|\n```$", "", text).strip()
        if text:
            res.append({"refs": p["refs"], "text": text, "type": p["type"],
                        "why": "[%s] %s" % (p["type"], p["why"])})
    return res


def rewrite(text, sources_text, instruction):
    prompt = (rules() + "\n\n[원문]\n" + sources_text + "\n\n[기존 초안]\n" + text +
              "\n\n[수정 지시] " + (instruction or "다른 유형·다른 인사이트 각도로 더 깊게 다시 써라") +
              "\n\n완성된 글 본문만 출력(설명·따옴표·코드블록 없이).")
    return run_claude(prompt).strip()


def morning_brief(top_items, calendar, today_label, snapshot=""):
    cal = "\n".join("- %s %s %s" % (c["date"], c.get("time", ""), c["event"]) for c in calendar) or "- (등록 일정 없음)"
    prompt = (rules() + "\n\n[현재 시장 스냅샷]\n" + (snapshot or "(없음)") +
              "\n\n[지난 24시간 주요 뉴스]\n" + fmt_candidates(top_items) +
              "\n\n[등록된 일정]\n" + cal +
              "\n\n[할 일] '☀️ %s 아침 체크포인트' 글을 써라. 형식:\n"
              "1줄 제목 → '📍지난밤 핵심' · 불릿 3~5개(각 1줄, 사실+한마디 해석) → "
              "'📍이번 주 일정' (등록 일정 중 7일 이내 + 뉴스에 나온 일정만, 없으면 생략) → "
              "'📍오늘의 관점' 2~3줄(지난밤 소식들이 서로 어떻게 이어지는지, 오늘 무엇을 보면 되는지) → 해시태그.\n"
              "링크 없이 800자 이내. 본문만 출력." % today_label)
    return run_claude(prompt).strip()
