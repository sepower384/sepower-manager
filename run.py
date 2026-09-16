# -*- coding: utf-8 -*-
"""세력의 매니저 실행기.

  pythonw run.py serve      상시 구동(텔레그램 버튼·명령 대기 + 30분마다 한 바퀴)
  python  run.py preview    수집→초안까지만, 전송 없이 data/preview.html 로 확인
  python  run.py collect    수집만
  python  run.py once       한 바퀴(수집→초안→전송) 1회
  python  run.py cloud updates|llm|cycle   GitHub Actions 용 (PC 꺼져도 돎)
"""
import html
import os
import socket
import sys
import time
import traceback
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from manager import pipeline, store, telegram  # noqa: E402

LOG = os.path.join(ROOT, "data", "manager.log")


def load_env():
    p = os.path.join(ROOT, ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def setup_log():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    if sys.stdout is None or "pythonw" in sys.executable.lower():
        f = open(LOG, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f


def in_active_hours(cfg):
    a, b = cfg["schedule"]["active_hours"]
    return a <= store.now_kst().hour < b


def cycle(db, cfg, force=False, send=True):
    if not force and (store.kv_get(db, "paused") == "1" or not in_active_hours(cfg)):
        return []
    pipeline.collect(db, cfg)
    ids = pipeline.make_alert_drafts(db, cfg) + pipeline.make_drafts(db, cfg, force)
    k = store.now_kst()
    hh, mm = map(int, cfg["schedule"]["morning_brief_at"].split(":"))
    if (k.hour, k.minute) >= (hh, mm) and store.kv_get(db, "brief_date") != k.date().isoformat():
        store.kv_set(db, "brief_date", k.date().isoformat())
        b = pipeline.make_brief(db, cfg)
        if b:
            ids.append(b)
    if send:
        pipeline.deliver(db, cfg, ids)
    return ids


def serve():
    setup_log()
    try:  # 중복 실행 방지
        lock = socket.socket()
        lock.bind(("127.0.0.1", 48761))
    except OSError:
        print("이미 실행 중"); return
    if not (telegram.token() and telegram.admin_chat()):
        print(".env 에 TELEGRAM_BOT_TOKEN_MANAGER / TELEGRAM_ADMIN_CHAT_ID 필요"); return
    db = store.connect()
    pipeline.log("세력의 매니저 시작")
    next_run = 0.0
    while True:
        cfg = pipeline.load_config()
        try:
            if time.time() >= next_run:
                next_run = time.time() + cfg["schedule"]["cycle_minutes"] * 60
                cycle(db, cfg)
            housekeeping(db, cfg)
            handle_updates(db, cfg, timeout=50)
        except Exception:
            pipeline.log("오류\n" + traceback.format_exc())
            time.sleep(30)


def handle_updates(db, cfg, timeout=0):
    """쌓인 버튼·명령 처리. 처리한 개수 반환."""
    admin = str(telegram.admin_chat())
    offset = int(store.kv_get(db, "offset", "0"))
    ups = telegram.updates(offset, timeout=timeout)
    for u in ups:
        store.kv_set(db, "offset", u["update_id"] + 1)
        try:
            if "callback_query" in u:
                cb = u["callback_query"]
                if str(cb["from"]["id"]) == admin:
                    pipeline.on_callback(db, cfg, cb)
            elif "message" in u:
                m = u["message"]
                if str(m["chat"]["id"]) == admin:
                    pipeline.on_message(db, cfg, m, lambda force=False: cycle(db, cfg, force))
        except Exception:
            pipeline.log("업데이트 처리 오류\n" + traceback.format_exc())
    return len(ups)


def housekeeping(db, cfg):
    pipeline.expire(db)
    store.prune(db)
    if store.kv_get(db, "mode", cfg["mode"]) == "auto":
        pipeline.flush_queue(db, cfg)


def llm_tasks(db, cfg):
    """가벼운 회차가 미뤄둔 LLM 일: 다시쓰기, /now, /brief."""
    pipeline.process_redo_queue(db, cfg)
    if store.kv_get(db, "want_cycle") == "1":
        store.kv_set(db, "want_cycle", "0")
        cycle(db, cfg, force=True)
    if store.kv_get(db, "want_brief") == "1":
        store.kv_set(db, "want_brief", "0")
        b = pipeline.make_brief(db, cfg)
        if b:
            pipeline.deliver(db, cfg, [b])


def needs_llm(db):
    return (store.kv_get(db, "redo_queue", "[]") != "[]" or store.kv_get(db, "want_cycle") == "1"
            or store.kv_get(db, "want_brief") == "1")


def cloud(mode):
    """GitHub Actions 용 1회 실행.
    updates : LLM 없이 버튼·명령 처리(5분마다). LLM 일이 생기면 GITHUB_OUTPUT 에 need_llm=true
    llm     : updates 가 미룬 LLM 일 처리
    cycle   : 버튼 처리 + 수집·초안·발행 (30분마다)
    """
    db = store.connect()
    cfg = pipeline.load_config()
    if mode == "updates":
        os.environ["MANAGER_NO_LLM"] = "1"
        n = handle_updates(db, cfg)
        housekeeping(db, cfg)
        need = needs_llm(db)
        pipeline.log("업데이트 %d개 처리, LLM 필요=%s" % (n, need))
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write("need_llm=%s\n" % ("true" if need else "false"))
    elif mode == "llm":
        llm_tasks(db, cfg)
    elif mode == "cycle":
        handle_updates(db, cfg)
        llm_tasks(db, cfg)
        cycle(db, cfg)
        housekeeping(db, cfg)


def preview():
    db = store.connect(os.path.join(ROOT, "data", "preview.db"))
    cfg = pipeline.load_config()
    ids = cycle(db, cfg, force=True, send=False)
    if "--brief" in sys.argv:
        b = pipeline.make_brief(db, cfg)
        ids += [b] if b else []
    rows = [store.get_draft(db, i) for i in ids]
    blocks = []
    for d in rows:
        img = ('<img src="%s">' % ("file:///" + d["photo"].replace("\\", "/"))) if d["photo"] else ""
        refs = " ".join('<a href="%s">%s</a>' % (r["url"], r["url"]) for r in d["refs"])
        blocks.append('<div class="c"><div class="h">#%d · %s · %s</div>%s<pre>%s</pre><div class="r">%s</div></div>'
                      % (d["id"], d["kind"], html.escape(d["reason"]), img, html.escape(d["text"]), refs))
    blocked = db.execute("SELECT note, COUNT(*) n FROM items WHERE status='blocked' GROUP BY note ORDER BY n DESC").fetchall()
    stat = ", ".join("%s %d" % (r["note"], r["n"]) for r in blocked)
    page = ("<meta charset=utf-8><title>세력의 매니저 미리보기</title><style>body{font-family:sans-serif;background:#e6ebee;max-width:560px;margin:20px auto;padding:0 16px}"
            ".c{background:#fff;border-radius:12px;padding:12px;margin:14px 0}.h{color:#888;font-size:12px}img{max-width:100%%;border-radius:8px;margin-top:6px}"
            "pre{white-space:pre-wrap;font-family:inherit;font-size:15px;line-height:1.5}.r{font-size:11px;color:#3a7}</style>"
            "<h3>미리보기 %s</h3><p style='font-size:12px;color:#666'>거른 글: %s</p>%s"
            % (datetime.now().strftime("%m-%d %H:%M"), html.escape(stat), "".join(blocks) or "<p>초안 없음</p>"))
    out = os.path.join(ROOT, "data", "preview.html")
    open(out, "w", encoding="utf-8").write(page)
    print("초안 %d개 → %s" % (len(rows), out))
    for d in rows:
        print("\n=== #%d [%s] %s\n%s" % (d["id"], d["kind"], d["reason"], d["text"]))


def main():
    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "preview":
        preview()
    elif cmd == "collect":
        pipeline.collect(store.connect(), pipeline.load_config())
    elif cmd == "cloud":
        cloud(sys.argv[2])
    elif cmd == "once":
        cycle(store.connect(), pipeline.load_config(), force=True)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
