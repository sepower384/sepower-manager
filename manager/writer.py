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


def build_pick_prompt(cands, recent, n):
    rec = "\n".join("- " + r.split("\n")[0][:80] for r in recent) or "- (없음)"
    return (rules() +
            "\n\n[최근에 이미 쓴 글 첫 줄 — 겹치면 제외]\n" + rec +
            "\n\n[후보]\n" + fmt_candidates(cands) +
            "\n\n[할 일] 위 후보에서 채널에 올릴 가치가 큰 것을 최대 %d개 골라 글을 써라. "
            "같은 사건을 다룬 후보 여러 개는 하나로 합쳐 써도 됨(refs에 모두 기입). "
            "유형(A~F)은 소식에 맞게 고르고 같은 유형만 반복하지 말 것.\n"
            "출력은 JSON 배열만: "
            '[{"refs":[후보번호,...],"type":"A~F","text":"완성된 글","why":"고른 이유 한 줄"}]' % n)


def pick_and_write(cands, recent, n=2, hint=""):
    if not cands:
        return []
    prompt = build_pick_prompt(cands, recent, n)
    if hint:
        prompt += "\n\n[추가 지시] " + hint
    out = extract_json(run_claude(prompt))
    res = []
    for d in out[:n]:
        refs = [int(x) for x in d.get("refs", []) if str(x).isdigit() and int(x) < len(cands)]
        text = (d.get("text") or "").strip()
        if refs and text:
            res.append({"refs": refs, "text": text, "why": d.get("why", "")})
    return res


def rewrite(text, sources_text, instruction):
    prompt = (rules() + "\n\n[원문]\n" + sources_text + "\n\n[기존 초안]\n" + text +
              "\n\n[수정 지시] " + (instruction or "다른 유형·다른 각도로 다시 써라") +
              "\n\n완성된 글 본문만 출력(설명·따옴표·코드블록 없이).")
    return run_claude(prompt).strip()


def morning_brief(top_items, calendar, today_label):
    cal = "\n".join("- %s %s %s" % (c["date"], c.get("time", ""), c["event"]) for c in calendar) or "- (등록 일정 없음)"
    prompt = (rules() + "\n\n[지난 24시간 주요 뉴스]\n" + fmt_candidates(top_items) +
              "\n\n[등록된 일정]\n" + cal +
              "\n\n[할 일] '☀️ %s 아침 체크포인트' 글을 써라. 형식:\n"
              "1줄 제목 → '📍지난밤 핵심' · 불릿 3~5개(각 1줄) → "
              "'📍이번 주 일정' (등록 일정 중 7일 이내 + 뉴스에 나온 일정만, 없으면 생략) → 한 줄 관점 → 해시태그.\n"
              "링크 없이 600자 이내. 본문만 출력." % today_label)
    return run_claude(prompt).strip()
