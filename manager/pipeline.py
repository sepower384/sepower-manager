# -*- coding: utf-8 -*-
"""수집 → 거르기 → 초안 → (승인) → 발행."""
import json
import os
from datetime import datetime, timedelta, timezone

from . import capture, filters, news, store, telegram, tme, writer

CONFIG = os.path.join(store.ROOT, "config.json")
CAND_WINDOW_H = 8
PENDING_MAX = 4
EXPIRE_H = 6


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def log(*a):
    print(store.now_kst().strftime("%m-%d %H:%M:%S"), *a, flush=True)


def weights(cfg):
    w = {s["id"]: s.get("weight", 1.0) for s in cfg["sources"]}
    w["news"] = cfg.get("news", {}).get("weight", 1.0)
    return w


# ───────────────────────────────────────────── 수집
def classify(it, src, cfg):
    kind = src["kind"]
    it["kind"] = kind
    if kind == "whale":
        w = filters.parse_whale(it["text"])
        big = w and w["eok"] >= cfg["whale"]["min_krw_eok"] and w["direction"] != "internal"
        it["status"] = "whale" if big else "skip"
        return it
    if kind == "liquidation":
        q = filters.parse_liq(it["text"])
        it["status"] = "liq" if q and q["usd"] >= cfg["liquidation"]["min_usd"] else "skip"
        return it
    reason = filters.block_reason(it, cfg)
    if reason:
        it["status"], it["note"] = "blocked", reason
    return it


def collect(db, cfg, pages=1):
    total = 0
    batches = []
    for src in cfg["sources"]:
        try:
            batches.append((src, tme.fetch_recent(src["id"], pages)))
        except Exception as e:
            log("[수집 실패]", src["id"], e)
    if cfg.get("news"):
        batches.append(({"id": "news", "kind": "news"}, news.collect(cfg, log)))
    for src, got in batches:
        fresh = []
        for it in got:
            it = classify(it, src, cfg)
            if it.get("status", "new") == "new":
                fp = filters.fingerprint(it["text"])
                it["fp"] = fp
                if fp and store.fp_seen(db, fp):
                    it["status"], it["note"] = "dup", "지문중복"
            fresh.append(it)
        n = store.upsert_items(db, fresh)
        total += n
    log("수집 완료: 새 글 %d개" % total)
    return total


# ───────────────────────────────────────────── 후보
def candidates(db, cfg, limit=25, now=None):
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=CAND_WINDOW_H)).isoformat()
    w = weights(cfg)
    low = set(cfg.get("news", {}).get("low_publishers", []))
    rows = store.items_where(db, "status='new' AND kind IN ('insight','news') AND date>=?", (since,))
    gram = [filters.grams(r["text"].split("\n")[0]) for r in rows]
    for i, r in enumerate(rows):
        # 여러 매체가 같이 다루는 이슈 = 지금 뜨거운 이슈
        g = gram[i]
        cover = sum(1 for j, h in enumerate(gram)
                    if j != i and g and h and len(g & h) / min(len(g), len(h)) >= 0.45)
        r["score"] = filters.score(r, cfg, w.get(r["source"], 1.0), now) * (1 + 0.15 * min(cover, 5))
        if r.get("publisher") in low:
            r["score"] *= 0.4
    rows.sort(key=lambda r: -r["score"])
    recent = store.recent_texts(db, 30)
    picked, dropped = [], []
    for r in rows:
        if any(filters.similar(r["text"], p["text"]) for p in picked) or \
                any(filters.similar(r["text"], t) for t in recent):
            dropped.append(r)
            continue
        picked.append(r)
        if len(picked) >= limit:
            break
    store.set_item_status(db, [(r["source"], r["post_id"]) for r in dropped], "dup")
    return picked


# ───────────────────────────────────────────── 초안
def pending_count(db):
    return db.execute("SELECT COUNT(*) FROM drafts WHERE status IN ('pending','queued')").fetchone()[0]


def make_drafts(db, cfg, force=False):
    if not force and pending_count(db) >= PENDING_MAX:
        log("대기 초안 %d개 — 이번 회차 초안 생략" % pending_count(db))
        return []
    today = store.count_kind_today(db, "insight")
    if not force and today >= cfg["schedule"]["daily_post_cap"] * 2:
        log("오늘 초안 상한 도달")
        return []
    cands = candidates(db, cfg)
    if not cands:
        log("후보 없음")
        return []
    n = cfg["schedule"]["drafts_per_cycle"]
    log("후보 %d개 → LLM 편집" % len(cands))
    picks = writer.pick_and_write(cands, store.recent_texts(db), n)
    ids = []
    used = set()
    for p in picks:
        refs = [cands[i] for i in p["refs"]]
        did = store.add_draft(db, "insight", p["text"],
                              "", [{"source": r["source"], "post_id": r["post_id"], "url": r["url"]} for r in refs])
        lead = next((r for r in refs if r["photos"]), refs[0])
        store.update_draft(db, did, photo=capture.image_for(lead, did), reason=p["why"])
        used |= {(r["source"], r["post_id"]) for r in refs}
        ids.append(did)
    store.set_item_status(db, list(used), "used")
    store.set_item_status(db, [(c["source"], c["post_id"]) for c in cands
                               if (c["source"], c["post_id"]) not in used], "passed")
    log("초안 %d개 생성" % len(ids))
    return ids


def krw_eok(eok):
    jo, rest = divmod(int(eok), 10000)
    return ("%d조 %s억" % (jo, format(rest, ",")) if rest else "%d조" % jo) if jo else "%s억" % format(rest, ",")


def whale_text(w):
    arrow = {"in": "거래소 입금", "out": "거래소 출금", "exchange": "거래소 간 이동", "move": "대량 이동"}[w["direction"]]
    note = {"in": "거래소 입금은 매도 대기 물량일 수 있어 단기 변동성 체크 필요함",
            "out": "거래소 출금은 보통 장기 보관 신호로 읽힘",
            "exchange": "거래소 간 이동은 차익거래·유동성 재배치인 경우가 많음",
            "move": "개인 지갑 간 이동이라 방향성은 아직 모름. 이후 거래소 유입 여부 지켜볼 구간"}[w["direction"]]
    return ("🐋 고래 %s 포착\n\n%s %s (약 %s)\n%s\n\n%s\n\n#온체인데이터 #%s"
            % (arrow, w["amount"], w["coin"], krw_eok(w["eok"]), w["route"], note, w["coin"]))


def liq_text(q):
    side = "롱" if q["side"] == "LONG" else "숏"
    return ("💥 대규모 %s 청산 감지\n\n%s %s $%s 청산 (가격 $%s, %s)\n\n%s\n\n#청산 #%s"
            % (side, q["symbol"], side, format(int(q["usd"]), ","), q["price"], q["exchange"],
               "숏 청산이 이어지면 숏스퀴즈로 번지기도 함" if side == "숏"
               else "롱 청산이 몰리는 구간은 단기 바닥이 나오기도 함", q["symbol"]))


def make_alert_drafts(db, cfg):
    ids = []
    for kind, status, fmt, parse, cap in (
            ("whale", "whale", whale_text, filters.parse_whale, cfg["whale"]["daily_cap"]),
            ("liquidation", "liq", liq_text, filters.parse_liq, cfg["liquidation"]["daily_cap"])):
        since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        rows = store.items_where(db, "status=? AND date>=? ORDER BY date DESC", (status, since))
        for r in rows:
            if store.count_kind_today(db, kind) >= cap:
                break
            ids.append(store.add_draft(db, kind, fmt(parse(r["text"])), "",
                                       [{"source": r["source"], "post_id": r["post_id"], "url": r["url"]}]))
            store.set_item_status(db, [(r["source"], r["post_id"])], "used")
            break  # 회차당 종류별 1개
        store.set_item_status(db, [(r["source"], r["post_id"]) for r in
                                   store.items_where(db, "status=?", (status,))], "passed")
    return ids


def make_brief(db, cfg):
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    w = weights(cfg)
    rows = store.items_where(db, "kind IN ('insight','news') AND status IN ('new','passed','used') AND date>=?", (since,))
    for r in rows:
        r["score"] = filters.score(r, cfg, w.get(r["source"], 1.0), now)
    rows.sort(key=lambda r: -r["score"])
    top = []
    for r in rows:
        if not any(filters.similar(r["text"], t["text"]) for t in top):
            top.append(r)
        if len(top) >= 12:
            break
    if not top:
        return None
    k = store.now_kst()
    label = "%d/%d(%s)" % (k.month, k.day, "월화수목금토일"[k.weekday()])
    today = k.date()
    cal = [c for c in cfg.get("calendar", [])
           if 0 <= (datetime.fromisoformat(c["date"]).date() - today).days <= 7]
    text = writer.morning_brief(top, cal, label)
    return store.add_draft(db, "brief", text, "", [])


# ───────────────────────────────────────────── 승인·발행
BUTTONS = lambda did: [[("✅ 올리기", "pub:%d" % did), ("🔁 다시쓰기", "re:%d" % did), ("🗑 버리기", "rej:%d" % did)]]


def admin_preview(db, did, cfg):
    d = store.get_draft(db, did)
    head = "📝 초안 #%d [%s]%s\n━━━━━━━━━━\n" % (did, d["kind"], (" · " + d["reason"]) if d["reason"] else "")
    src = "\n━━━━━━━━━━\n원문: " + " ".join(r["url"] for r in d["refs"]) if d["refs"] else ""
    msg = telegram.send(telegram.admin_chat(), head + d["text"] + src, d["photo"], BUTTONS(did))
    store.update_draft(db, did, admin_msg_id=msg["message_id"])


def publish(db, did, cfg):
    d = store.get_draft(db, did)
    if not d or d["status"] == "published":
        return False
    target = telegram.target_chat()
    if not target:
        raise RuntimeError("TELEGRAM_TARGET_CHAT_ID 가 비어 있어 발행 불가")
    text = d["text"]
    promo = cfg.get("promo", {})
    if promo.get("enabled"):
        n = db.execute("SELECT COUNT(*) FROM drafts WHERE status='published'").fetchone()[0]
        if (n + 1) % promo["every_n_posts"] == 0:
            text += "\n\n" + promo["text"]
    msg = telegram.send(target, text, d["photo"])
    store.update_draft(db, did, status="published", channel_msg_id=msg["message_id"],
                       published_at=store.now_kst().isoformat())
    log("발행 #%d" % did)
    return True


def deliver(db, cfg, ids):
    """새 초안 처리 — approve 는 DM 으로, auto 는 큐에 넣고 간격 맞춰 발행."""
    mode = store.kv_get(db, "mode", cfg["mode"])
    for did in ids:
        if mode == "auto" and telegram.target_chat():
            store.update_draft(db, did, status="queued")
        else:
            admin_preview(db, did, cfg)


def flush_queue(db, cfg):
    s = cfg["schedule"]
    start = store.now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
    pubs = store.published_since(db, start)
    if len(pubs) >= s["daily_post_cap"]:
        return
    if pubs:
        last = datetime.fromisoformat(pubs[-1]["published_at"])
        if store.now_kst() - last < timedelta(minutes=s["min_gap_minutes"]):
            return
    r = db.execute("SELECT id FROM drafts WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
    if r:
        publish(db, r["id"], cfg)


def expire(db):
    cut = (store.now_kst() - timedelta(hours=EXPIRE_H)).isoformat()
    for r in db.execute("SELECT id, admin_msg_id FROM drafts WHERE status IN ('pending','queued') AND created<?", (cut,)).fetchall():
        store.update_draft(db, r["id"], status="expired")
        if r["admin_msg_id"]:
            telegram.edit_buttons(telegram.admin_chat(), r["admin_msg_id"])


def sources_text(db, d):
    out = []
    for ref in d["refs"]:
        it = store.items_where(db, "source=? AND post_id=?", (ref["source"], ref["post_id"]))
        if it:
            out.append("[%s] %s\n링크: %s" % (it[0]["source"], it[0]["text"][:900], ", ".join(it[0]["links"][:2])))
    return "\n\n".join(out) or "(원문 없음)"


def on_callback(db, cfg, cb):
    data = cb.get("data", "")
    act, _, did = data.partition(":")
    did = int(did or 0)
    d = store.get_draft(db, did)
    msg = cb.get("message", {})
    if not d:
        return telegram.answer(cb["id"], "초안 없음")
    if d["status"] not in ("pending", "queued"):
        telegram.answer(cb["id"], "이미 처리됨: " + d["status"])
        return telegram.edit_buttons(msg["chat"]["id"], msg["message_id"])
    if act == "pub":
        try:
            publish(db, did, cfg)
            telegram.answer(cb["id"], "채널에 올렸음 ✅")
            telegram.edit_buttons(msg["chat"]["id"], msg["message_id"], [[("✅ 발행됨", "noop:0")]])
        except Exception as e:
            telegram.answer(cb["id"], "발행 실패: %s" % str(e)[:150])
    elif act == "rej":
        store.update_draft(db, did, status="rejected")
        telegram.answer(cb["id"], "버렸음")
        telegram.edit_buttons(msg["chat"]["id"], msg["message_id"], [[("🗑 버림", "noop:0")]])
    elif act == "re":
        telegram.answer(cb["id"], "다시 쓰는 중…")
        redo(db, cfg, d, "")
        telegram.edit_buttons(msg["chat"]["id"], msg["message_id"], [[("🔁 새 초안으로 대체", "noop:0")]])
    else:
        telegram.answer(cb["id"])


def redo(db, cfg, d, instruction):
    if not writer.available():   # 클라우드 가벼운 회차 — LLM 회차로 미룸
        q = json.loads(store.kv_get(db, "redo_queue", "[]"))
        q.append([d["id"], instruction])
        store.kv_set(db, "redo_queue", json.dumps(q, ensure_ascii=False))
        telegram.send(telegram.admin_chat(), "🔁 #%d 다시쓰기 접수 — 잠시 뒤 새 초안이 옴" % d["id"])
        return
    text = writer.rewrite(d["text"], sources_text(db, d), instruction)
    store.update_draft(db, d["id"], status="rejected")
    nid = store.add_draft(db, d["kind"], text, d["photo"], d["refs"])
    store.update_draft(db, nid, reason="수정본(#%d)" % d["id"])
    admin_preview(db, nid, cfg)


def process_redo_queue(db, cfg):
    q = json.loads(store.kv_get(db, "redo_queue", "[]"))
    store.kv_set(db, "redo_queue", "[]")
    for did, instruction in q:
        d = store.get_draft(db, did)
        if d and d["status"] in ("pending", "queued"):
            redo(db, cfg, d, instruction)
    return len(q)


HELP = ("세력의 매니저 명령어\n"
        "/status 현황  /now 지금 한 바퀴  /brief 아침 체크포인트 지금\n"
        "/pause 멈춤  /resume 재개\n"
        "/auto 자동발행  /approve 승인모드\n"
        "초안 메시지에 '답장'으로 글을 보내면 → 그 글로 교체\n"
        "초안에 '답장'으로 '!지시' (예: !더 짧게) → 그 지시대로 다시 씀")


def on_message(db, cfg, m, run_cycle):
    text = (m.get("text") or "").strip()
    reply = m.get("reply_to_message")
    if reply and text:
        r = db.execute("SELECT id FROM drafts WHERE admin_msg_id=?", (reply["message_id"],)).fetchone()
        if r:
            d = store.get_draft(db, r["id"])
            if text.startswith("!"):
                return redo(db, cfg, d, text[1:].strip())
            store.update_draft(db, d["id"], status="rejected")
            nid = store.add_draft(db, d["kind"], text, d["photo"], d["refs"])
            store.update_draft(db, nid, reason="직접수정(#%d)" % d["id"])
            return admin_preview(db, nid, cfg)
    cmd = text.split()[0].split("@")[0] if text else ""
    chat = telegram.admin_chat()
    if cmd in ("/start", "/help"):
        telegram.send(chat, HELP)
    elif cmd == "/status":
        start = store.now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
        pend = pending_count(db)
        items = db.execute("SELECT COUNT(*) FROM items WHERE fetched_at>=?", (start.isoformat(),)).fetchone()[0]
        telegram.send(chat, "모드: %s%s\n오늘 발행 %d개 / 대기 %d개\n오늘 수집 %d개\n발행 채널: %s" % (
            store.kv_get(db, "mode", cfg["mode"]), " (일시정지)" if store.kv_get(db, "paused") == "1" else "",
            len(store.published_since(db, start)), pend, items, telegram.target_chat() or "미설정"))
    elif cmd == "/pause":
        store.kv_set(db, "paused", "1"); telegram.send(chat, "⏸ 멈췄음")
    elif cmd == "/resume":
        store.kv_set(db, "paused", "0"); telegram.send(chat, "▶️ 재개")
    elif cmd == "/auto":
        store.kv_set(db, "mode", "auto"); telegram.send(chat, "🤖 자동발행 모드 (하루 %d개, %d분 간격)" % (
            cfg["schedule"]["daily_post_cap"], cfg["schedule"]["min_gap_minutes"]))
    elif cmd == "/approve":
        store.kv_set(db, "mode", "approve"); telegram.send(chat, "🙋 승인 모드")
    elif cmd == "/now":
        telegram.send(chat, "한 바퀴 도는 중…")
        if writer.available():
            run_cycle(force=True)
        else:
            store.kv_set(db, "want_cycle", "1")
    elif cmd == "/brief":
        if not writer.available():
            store.kv_set(db, "want_brief", "1")
            return telegram.send(chat, "아침 체크포인트 쓰는 중…")
        did = make_brief(db, cfg)
        if did:
            deliver(db, cfg, [did])
        else:
            telegram.send(chat, "최근 24시간 수집글이 없음")
