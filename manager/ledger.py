# -*- coding: utf-8 -*-
"""발행 장부 — 중복 발행을 막는 '즉시 일관' 기록.

클라우드 상태 DB(actions/cache)는 저장 직후 다음 회차에 옛 버전이 복원될 수 있다
(→ 같은 글 재발행·같은 기사 재선택이 생김). 그래서 발행 사실만은 git 의 `state` 브랜치에
작은 JSON 으로 남기고, 발행 직전에 항상 원격 최신본을 확인한다.

기록: 글 지문(text), 원문 키(source:post_id / 기사 URL), 이미지 해시, 제목줄, 마지막 발행 시각.
git 원격이 없으면(로컬) data/ledger.json 파일만 쓴다.
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timedelta

from .store import KST, ROOT

BRANCH = "state"
FILE = "published.json"
LOCAL = os.path.join(ROOT, "data", "ledger.json")
KEEP_H = 72
GIT_DIR = ROOT
STATE_LOCAL = os.path.join(ROOT, "data", "state")


def text_fp(text):
    t = re.sub(r"https?://\S+|#\S+", "", text or "")
    t = re.sub(r"[^0-9A-Za-z가-힣]", "", t).lower()
    return hashlib.md5(t[:300].encode("utf-8")).hexdigest()[:16]


def ref_keys(refs):
    out = []
    for r in refs or []:
        out.append("%s:%s" % (r.get("source"), r.get("post_id")))
        if r.get("url"):
            out.append("url:" + re.sub(r"[?#].*$", "", r["url"]))
    return out


def _git(*args, cwd=None, check=True):
    p = subprocess.run(["git", *args], cwd=cwd or GIT_DIR, capture_output=True, text=True, encoding="utf-8")
    if check and p.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), (p.stderr or p.stdout)[:200]))
    return p


def remote_enabled():
    return os.environ.get("LEDGER_GIT") == "1"


def empty():
    return {"posts": [], "last_pub_at": ""}


def _prune(led):
    cut = (datetime.now(KST) - timedelta(hours=KEEP_H)).isoformat()
    led["posts"] = [p for p in led["posts"] if p.get("at", "") >= cut]
    return led


def load():
    if remote_enabled():
        _git("fetch", "-q", "origin", "+refs/heads/%s:refs/remotes/origin/%s" % (BRANCH, BRANCH), check=False)
        p = _git("show", "origin/%s:%s" % (BRANCH, FILE), check=False)
        if p.returncode == 0 and p.stdout.strip():
            return _prune(json.loads(p.stdout))
        return empty()
    if os.path.exists(LOCAL):
        with open(LOCAL, encoding="utf-8") as f:
            return _prune(json.load(f))
    return empty()


def _save_local(led):
    os.makedirs(os.path.dirname(LOCAL), exist_ok=True)
    with open(LOCAL, "w", encoding="utf-8") as f:
        json.dump(led, f, ensure_ascii=False, indent=1)


def _show(path):
    p = _git("show", "origin/%s:%s" % (BRANCH, path), check=False)
    return p.stdout if p.returncode == 0 else ""


def commit_files(update, message):
    """state 브랜치 최신본 위에서 update(read) -> {경로: 내용} 을 써서 커밋·푸시. 경합이면 재시도.
    read(path) 는 원격 최신 내용을 준다. 로컬 모드면 data/state/ 에 쓴다."""
    if not remote_enabled():
        base = STATE_LOCAL

        def read_local(path):
            fp = os.path.join(base, path)
            return open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
        files = update(read_local)
        for path, content in files.items():
            fp = os.path.join(base, path)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(content)
        return files
    for attempt in range(4):
        _git("fetch", "-q", "origin", "+refs/heads/%s:refs/remotes/origin/%s" % (BRANCH, BRANCH), check=False)
        files = update(_show)
        with tempfile.TemporaryDirectory() as tmp:
            wt = os.path.join(tmp, "wt")
            has = _git("ls-remote", "--exit-code", "--heads", "origin", BRANCH, check=False).returncode == 0
            if has:
                _git("worktree", "add", "-f", "--detach", wt, "origin/%s" % BRANCH)
            else:
                _git("worktree", "add", "-f", "--detach", wt)
                _git("checkout", "--orphan", "tmp-state", cwd=wt)
                _git("rm", "-rfq", ".", cwd=wt, check=False)
            for path, content in files.items():
                fp = os.path.join(wt, path)
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(content)
                _git("add", path, cwd=wt)
            _git("-c", "user.name=sepower-manager", "-c", "user.email=bot@users.noreply.github.com",
                 "commit", "-qm", message, cwd=wt)
            p = _git("push", "-q", "origin", "HEAD:refs/heads/%s" % BRANCH, cwd=wt, check=False)
            _git("worktree", "remove", "-f", wt, check=False)
            if p.returncode == 0:
                return files
        time.sleep(2 + attempt * 2)
    raise RuntimeError("state 브랜치 저장 실패(경합)")


def read_state(path):
    if remote_enabled():
        _git("fetch", "-q", "origin", "+refs/heads/%s:refs/remotes/origin/%s" % (BRANCH, BRANCH), check=False)
        return _show(path)
    fp = os.path.join(STATE_LOCAL, path)
    return open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""


def seen(led, draft):
    """이 초안이 이미 나간 것과 겹치면 이유 문자열, 아니면 None."""
    fp = text_fp(draft["text"])
    keys = set(ref_keys(draft.get("refs")))
    img = draft.get("photo_hash") or ""
    for p in led["posts"]:
        if p.get("fp") == fp:
            return "같은 글이 이미 나감(#%s)" % p.get("id")
        if keys & set(p.get("refs", [])):
            return "같은 원문이 이미 쓰임(#%s)" % p.get("id")
        if img and img == p.get("img"):
            return "같은 캡처가 이미 쓰임(#%s)" % p.get("id")
    return None


def record(draft, at):
    """발행 장부(72시간) + 월별 영구 보관(archive/YYYY-MM.jsonl, 보고서 재료)을 한 커밋으로."""
    text = draft["text"] or ""
    entry = {"id": draft["id"], "at": at, "kind": draft.get("kind", "insight"), "fp": text_fp(text),
             "refs": ref_keys(draft.get("refs")), "img": draft.get("photo_hash") or "",
             "head": " ".join(text.split("\n")[:2])[:160]}
    full = dict(entry, text=text, ptype=draft.get("ptype") or "",
                channels=len(json.loads(draft.get("channel_msgs") or "{}")))
    month = at[:7]
    out = {}

    def update(read):
        raw = read(FILE) if remote_enabled() else (open(LOCAL, encoding="utf-8").read() if os.path.exists(LOCAL) else "")
        led = _prune(json.loads(raw)) if raw.strip() else empty()
        led["posts"].append(entry)
        led["last_pub_at"] = max(led.get("last_pub_at", ""), at)
        out["led"] = led
        arch = read("archive/%s.jsonl" % month)
        files = {"archive/%s.jsonl" % month: arch + json.dumps(full, ensure_ascii=False) + "\n"}
        if remote_enabled():
            files[FILE] = json.dumps(led, ensure_ascii=False, indent=1)
        return files

    commit_files(update, "발행 기록 %s" % draft["id"])
    if not remote_enabled():
        _save_local(out["led"])
    return out["led"]


def load_archive(start, end):
    """[start, end) 사이(KST aware datetime)에 발행된 글 전체."""
    months, cur = [], start.replace(day=1)
    while cur < end:
        months.append(cur.strftime("%Y-%m"))
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    rows = []
    for m in months:
        for line in read_state("archive/%s.jsonl" % m).splitlines():
            if line.strip():
                r = json.loads(line)
                if start.isoformat() <= r["at"] < end.isoformat():
                    rows.append(r)
    return rows


def heads(led):
    return [p["head"] for p in led["posts"] if p.get("head")]


def used_refs(led):
    return {k for p in led["posts"] for k in p.get("refs", [])}


def used_images(led):
    return {p["img"] for p in led["posts"] if p.get("img")}
