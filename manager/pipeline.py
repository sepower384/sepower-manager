# -*- coding: utf-8 -*-
"""수집 → 거르기 → 초안 → (승인) → 발행."""
import json
import os
import random
import re
from datetime import datetime, timedelta, timezone

from . import capture, context, filters, ledger, news, store, telegram, tme, writer

CONFIG = os.path.join(store.ROOT, "config.json")
CAND_WINDOW_H = 8
PENDING_MAX = 4
EXPIRE_H = 6
QUEUE_EXPIRE_H = 3


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
def is_urgent(item, cfg, now=None):
    """속보성 + 90분 이내 + 채널 주제에 맞음."""
    u = cfg.get("urgent", {})
    words = u.get("words", [])
    text = item["text"] or ""
    if not any(w in text[:120] for w in words):
        return False
    try:
        dt = datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
    except ValueError:
        return False
    age = ((now or datetime.now(timezone.utc)) - dt).total_seconds() / 60
    return age <= u.get("fresh_minutes", 90) and bool(filters.topic_hits(text, cfg))


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
        r["crypto"] = filters.is_crypto(r["text"], cfg)
        if r["crypto"]:
            r["score"] *= cfg.get("crypto", {}).get("weight", 1.0)
    rows.sort(key=lambda r: -r["score"])
    led = ledger.load()                       # 실제로 나간 것(즉시 일관) + 로컬 DB 기록
    recent = store.recent_topics(db) + ledger.heads(led)
    used = ledger.used_refs(led)
    picked, dropped = [], []
    for r in rows:
        head = r["text"].split("\n")[0]
        keys = set(ledger.ref_keys([{"source": r["source"], "post_id": r["post_id"], "url": r["url"]}]))
        r["urgent"] = is_urgent(r, cfg, now)
        if keys & used or \
                any(filters.similar(head, p["text"].split("\n")[0]) for p in picked) or \
                any(filters.similar(head, t) for t in recent):
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
    urgent = [c for c in cands if c.get("urgent")]
    urgent_ok = urgent and store.count_urgent_today(db) < cfg.get("urgent", {}).get("daily_cap", 4)
    queued = db.execute("SELECT COUNT(*) FROM drafts WHERE status='queued'").fetchone()[0]
    auto = store.kv_get(db, "mode", cfg["mode"]) == "auto"
    if not force and auto and queued >= 1 and not urgent_ok:
        log("대기열에 %d개 있음 — 새 초안 안 씀(1~2시간 간격이라 미리 쌓으면 시의성 떨어짐)" % queued)
        return []
    hint = ""
    if urgent_ok:
        hint = ("긴급 속보 후보: %s — 이 중 채널에 알릴 가치가 있으면 반드시 먼저 고르고 짧은 유형(A)으로 빠르게 써라."
                % ", ".join("#%d" % cands.index(c) for c in urgent[:3]))
        n = max(n, 1)
    elif need_crypto(db, cfg) and any(c.get("crypto") for c in cands):
        # 최근 글에 크립토가 모자라면 크립토 후보를 앞으로 + 반드시 크립토로
        cands.sort(key=lambda c: not c.get("crypto"))
        hint = ("최근 글에 크립토 이야기가 부족함. 이번엔 반드시 [크립토] 표시된 후보에서 골라라 "
                "(코인시장·BTC/ETH·알트·거래소·스테이블코인·온체인·규제 중 인사이트 있는 것).")
        log("크립토 비중 부족 → 크립토 우선")
    log("후보 %d개(긴급 %d) → LLM 편집" % (len(cands), len(urgent)))
    snap = {}

    def enrich(refs):
        if "v" not in snap:
            snap["v"] = context.market_snapshot()
        bodies = {}
        for i in refs[:3]:
            c = cands[i]
            if c["source"] == "news" and c["links"]:
                bodies[i] = context.article_text(c["links"][0])
        return bodies, snap["v"]

    led = ledger.load()
    topics = store.recent_topics(db) + ledger.heads(led)
    picks = writer.pick_and_write(cands, topics, n, hint=hint, recent_types=store.recent_types(db),
                                  enrich=enrich, recent_posts=store.recent_texts(db, 3))
    ids = []
    used = set()
    hashes = store.recent_hashes(db) | ledger.used_images(led)
    for p in dedupe_picks(picks, cands, topics):
        refs = [cands[i] for i in p["refs"]]
        did = store.add_draft(db, "insight", p["text"],
                              "", [{"source": r["source"], "post_id": r["post_id"], "url": r["url"]} for r in refs])
        lead = next((r for r in refs if r["source"] == "news"), refs[0])   # 기사 캡처 우선
        path, h = capture.image_for(lead, did, hashes)
        hashes.add(h)
        store.update_draft(db, did, photo=path, photo_hash=h, reason=p["why"], ptype=p.get("type", ""),
                           urgent=1 if any(r.get("urgent") for r in refs) else 0)
        used |= {(r["source"], r["post_id"]) for r in refs}
        ids.append(did)
    store.set_item_status(db, list(used), "used")
    store.set_item_status(db, [(c["source"], c["post_id"]) for c in cands
                               if (c["source"], c["post_id"]) not in used], "passed")
    log("초안 %d개 생성" % len(ids))
    return ids


def need_crypto(db, cfg):
    """최근 window 개 글 중 크립토 비중이 min_share 미만이면 True."""
    c = cfg.get("crypto", {})
    window = c.get("window", 4)
    posts = [p["head"] for p in ledger.load()["posts"] if p.get("head")][-window:]
    if len(posts) < window:
        posts = (store.recent_texts(db, window) + posts)[:window]
    if not posts:
        return True
    share = sum(1 for t in posts if filters.is_crypto(t, cfg)) / len(posts)
    return share < c.get("min_share", 0.5)


def dedupe_picks(picks, cands, topics):
    """같은 기사·같은 사건이 한 회차 안에서, 또는 최근 48시간 글과 겹치면 버린다."""
    kept, seen_refs, heads = [], set(), []
    for p in picks:
        refs = set(p["refs"])
        lead = " ".join(p["text"].split("\n")[:2])
        src_heads = [cands[i]["text"].split("\n")[0] for i in p["refs"]]
        dup = (refs & seen_refs
               or any(filters.similar(lead, h, 0.5) for h in heads)
               or any(filters.similar(s, h) for s in src_heads for h in heads)
               or any(filters.similar(lead, t, 0.5) for t in topics))
        if dup:
            log("중복이라 버림:", lead[:60])
            continue
        kept.append(p)
        seen_refs |= refs
        heads += [lead] + src_heads
    return kept


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
            did = store.add_draft(db, kind, fmt(parse(r["text"])), "",
                                  [{"source": r["source"], "post_id": r["post_id"], "url": r["url"]}])
            ensure_photo(db, did)
            ids.append(did)
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
    text = writer.morning_brief(top, cal, label, context.market_snapshot())
    did = store.add_draft(db, "brief", text, "", [])
    ensure_photo(db, did)
    return did


# ───────────────────────────────────────────── 승인·발행
BUTTONS = lambda did: [[("✅ 올리기", "pub:%d" % did), ("🔁 다시쓰기", "re:%d" % did), ("🗑 버리기", "rej:%d" % did)]]


def admin_preview(db, did, cfg):
    d = store.get_draft(db, did)
    head = "📝 초안 #%d [%s]%s\n━━━━━━━━━━\n" % (did, d["kind"], (" · " + d["reason"]) if d["reason"] else "")
    src = "\n━━━━━━━━━━\n원문: " + " ".join(r["url"] for r in d["refs"]) if d["refs"] else ""
    msg = telegram.send(telegram.admin_chat(), head + d["text"] + src, d["photo"], BUTTONS(did))
    store.update_draft(db, did, admin_msg_id=msg["message_id"])


CARD_ACCENT = {"whale": "#1d9bf0", "liquidation": "#f4212e", "brief": "#00ba7c"}


def ensure_photo(db, did):
    """이미지 파일이 없으면(클라우드 회차가 바뀌었거나 캡처 실패) 다시 만든다. 실패하면 RuntimeError."""
    d = store.get_draft(db, did)
    if d["photo"] and os.path.exists(d["photo"]):
        return d["photo"]
    used = store.recent_hashes(db)
    path = h = None
    if d["kind"] == "insight" and d["refs"]:
        ref = d["refs"][0]
        its = store.items_where(db, "source=? AND post_id=?", (ref["source"], ref["post_id"]))
        if its:
            path, h = capture.image_for(its[0], did, used)
    if not path:
        lines = [re.sub(r"[^\w\s·().,%$~→/+\-]", "", l).strip() for l in d["text"].split("\n") if l.strip()]
        title = lines[0] if lines else "세력"
        sub = next((l for l in lines[1:] if not l.startswith("#")), "")
        path = capture.card(title, {"whale": "온체인 데이터", "liquidation": "청산 데이터",
                                    "brief": "아침 체크포인트"}.get(d["kind"], "세력"),
                            sub, "d%d_card.png" % did, CARD_ACCENT.get(d["kind"], "#f0b90b"))
        h = capture.file_hash(path)
    store.update_draft(db, did, photo=path, photo_hash=h)
    return path


def publish(db, did, cfg, now=None):
    d = store.get_draft(db, did)
    if not d or d["status"] == "published":
        return False
    target = telegram.target_chat()
    if not target:
        raise RuntimeError("TELEGRAM_TARGET_CHAT_ID 가 비어 있어 발행 불가")
    try:
        ensure_photo(db, did)            # 자료 이미지 없는 글은 내보내지 않는다
    except Exception as e:
        raise RuntimeError("이미지 준비 실패라 발행 보류: %s" % str(e)[:120])
    d = store.get_draft(db, did)
    if True:                             # 같은 글·원문·캡처가 이미 나갔으면 절대 다시 안 냄(알림·브리핑 포함)
        why = ledger.seen(ledger.load(), d)
        if why:
            store.update_draft(db, did, status="dup", reason=why)
            log("발행 취소 #%d: %s" % (did, why))
            return False
    text = d["text"]
    promo = cfg.get("promo", {})
    if promo.get("enabled"):
        n = db.execute("SELECT COUNT(*) FROM drafts WHERE status='published'").fetchone()[0]
        if (n + 1) % promo["every_n_posts"] == 0:
            text += "\n\n" + promo["text"]
    sent, errors = {}, []
    for chat in telegram.target_chats():        # 모든 발행 채널에 같은 글
        try:
            sent[chat] = telegram.send(chat, text, d["photo"])["message_id"]
        except Exception as e:
            errors.append("%s: %s" % (chat, str(e)[:80]))
    if not sent:
        raise RuntimeError("모든 채널 발행 실패 — " + "; ".join(errors))
    at = (now or store.now_kst()).isoformat()
    store.update_draft(db, did, status="published", channel_msg_id=next(iter(sent.values())),
                       channel_msgs=json.dumps(sent), published_at=at)
    try:
        ledger.record(store.get_draft(db, did), at)
    except Exception as e:
        log("⚠️ 발행 장부 기록 실패:", e)
        telegram.send(telegram.admin_chat(), "⚠️ #%d 발행 장부 기록 실패 — 자동발행 잠시 멈춤(/resume 으로 재개)\n%s" % (did, e))
        store.kv_set(db, "paused", "1")     # 장부 없이 계속 내보내면 중복 위험
    log("발행 #%d → %d개 채널%s" % (did, len(sent), (" (실패: %s)" % "; ".join(errors)) if errors else ""))
    if errors:
        telegram.send(telegram.admin_chat(), "⚠️ #%d 일부 채널 발행 실패\n%s" % (did, "\n".join(errors)))
    return True


def deliver(db, cfg, ids):
    """새 초안 처리 — approve 는 DM 으로, auto 는 큐에 넣고 간격 맞춰 발행."""
    mode = store.kv_get(db, "mode", cfg["mode"])
    for did in ids:
        if mode == "auto" and telegram.target_chat():
            store.update_draft(db, did, status="queued")
        else:
            admin_preview(db, did, cfg)


def in_active_hours(cfg, now=None):
    a, b = cfg["schedule"]["active_hours"]
    return a <= (now or store.now_kst()).hour < b


def next_gap_minutes(last_pub_at, cfg):
    """마지막 발행 시각으로 정해지는 무작위 간격 — 상태가 옛 버전이어도 모든 회차가 같은 값을 얻는다."""
    lo, hi = cfg["schedule"]["gap_minutes"]
    seed = int(ledger.hashlib.md5(last_pub_at.encode()).hexdigest()[:8], 16)
    return lo + random.Random(seed).random() * (hi - lo)


def flush_queue(db, cfg, now=None):
    """자동발행 — 보통 글은 gap_minutes(1~2시간) 무작위, 긴급 속보는 urgent.gap_minutes 뒤면 바로."""
    s = cfg["schedule"]
    now = now or store.now_kst()
    if not in_active_hours(cfg, now) or store.kv_get(db, "paused") == "1":
        return False
    led = ledger.load()
    today = [p for p in led["posts"] if p.get("at", "") >= now.replace(hour=0, minute=0, second=0).isoformat()]
    if len(today) >= s["daily_post_cap"]:
        return False
    last = led.get("last_pub_at") or ""
    since = (now - datetime.fromisoformat(last)).total_seconds() / 60 if last else 10 ** 6
    for r in db.execute("SELECT id, urgent FROM drafts WHERE status='queued' ORDER BY urgent DESC, id").fetchall():
        need = cfg.get("urgent", {}).get("gap_minutes", 15) if r["urgent"] else next_gap_minutes(last, cfg)
        if since < need:
            return False
        try:
            ok = publish(db, r["id"], cfg, now)
        except Exception as e:     # 이미지 못 만드는 가벼운 회차 등 → 다음 회차에 다시
            log("발행 보류 #%d:" % r["id"], e)
            return False
        if ok:
            notify_published(db, r["id"])
            return True
        # 중복이라 취소됐으면 다음 대기 글을 본다
    return False


def notify_published(db, did):
    """자동발행된 글을 DM 으로 알려주고, 마음에 안 들면 바로 지울 수 있게."""
    d = store.get_draft(db, did)
    try:
        msg = telegram.send(telegram.admin_chat(), "✅ 채널 게시됨 #%d\n━━━━━━━━━━\n%s" % (did, d["text"]),
                            buttons=[[("🗑 채널에서 삭제", "del:%d" % did)]])
        store.update_draft(db, did, admin_msg_id=msg["message_id"])
    except Exception as e:
        log("발행 알림 실패", e)


def expire(db):
    """승인 대기는 6시간, 자동발행 대기열은 3시간 지나면 시의성이 떨어져 폐기."""
    now = store.now_kst()
    cut = (now - timedelta(hours=EXPIRE_H)).isoformat()
    qcut = (now - timedelta(hours=QUEUE_EXPIRE_H)).isoformat()
    for r in db.execute("SELECT id, admin_msg_id FROM drafts WHERE (status='pending' AND created<?) "
                        "OR (status='queued' AND created<?)", (cut, qcut)).fetchall():
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


def get_targets(db):
    """발행 채널 목록. 처음엔 Secret(쉼표 구분) + 예전 단일 설정(target_chat)으로 시작, 이후 DM 버튼으로 추가·삭제."""
    raw = store.kv_get(db, "targets")
    if raw is None:
        base = [c.strip() for c in telegram.env("TELEGRAM_TARGET_CHAT_ID").split(",") if c.strip()]
        legacy = store.kv_get(db, "target_chat")
        if legacy and legacy not in base:
            base.append(legacy)
        store.kv_set(db, "targets", json.dumps(base))
        return base
    return json.loads(raw)


def set_targets(db, targets):
    store.kv_set(db, "targets", json.dumps(list(dict.fromkeys(targets))))
    apply_target(db)


def apply_target(db):
    os.environ["MANAGER_TARGETS"] = ",".join(get_targets(db))


def target_titles(db):
    titles = json.loads(store.kv_get(db, "target_titles", "{}"))
    missing = [t for t in get_targets(db) if t not in titles]
    for t in missing:
        try:
            titles[t] = telegram.call("getChat", {"chat_id": t}).get("title", t)
        except Exception:
            titles[t] = t
    if missing:
        store.kv_set(db, "target_titles", json.dumps(titles, ensure_ascii=False))
    return titles


def on_chat_member(db, cfg, u):
    """봇이 채널 관리자로 추가되면 강회장에게 '여기에도 발행할까?' 버튼을 보낸다."""
    chat = u["chat"]
    status = u["new_chat_member"]["status"]
    if chat.get("type") not in ("channel", "supergroup"):
        return
    cid = str(chat["id"])
    titles = target_titles(db)
    titles[cid] = chat.get("title", cid)
    store.kv_set(db, "target_titles", json.dumps(titles, ensure_ascii=False))
    if status == "administrator":
        if cid in get_targets(db):
            return
        telegram.send(telegram.admin_chat(),
                      "📌 '%s' 에 관리자로 추가됐음\n이 채널에도 글을 올릴까? (기존 채널은 그대로 유지)" % titles[cid],
                      buttons=[[("✅ 여기에도 발행", "tgt:%s" % cid)]])
    elif cid in get_targets(db) and status in ("left", "kicked", "member"):
        set_targets(db, [t for t in get_targets(db) if t != cid])
        telegram.send(telegram.admin_chat(), "⚠️ '%s' 에서 관리자 권한이 빠져서 발행 목록에서 뺐음" % titles[cid])


def targets_message(db):
    titles = target_titles(db)
    ts = get_targets(db)
    lines = ["📡 발행 채널 %d곳" % len(ts)] + ["· %s (%s)" % (titles.get(t, "?"), t) for t in ts]
    buttons = [[("🚫 %s 빼기" % titles.get(t, t)[:20], "untgt:%s" % t)] for t in ts]
    return "\n".join(lines), buttons


def on_callback(db, cfg, cb):
    data = cb.get("data", "")
    act, _, did = data.partition(":")
    msg = cb.get("message", {})
    if act == "tgt":
        set_targets(db, get_targets(db) + [did])
        telegram.answer(cb["id"], "발행 채널 추가됨")
        telegram.edit_buttons(msg["chat"]["id"], msg["message_id"], [[("✅ 발행 채널에 추가됨", "noop:0")]])
        text, _ = targets_message(db)
        return telegram.send(telegram.admin_chat(), text + "\n\n같은 글이 모든 채널에 올라감. /targets 로 관리")
    if act == "untgt":
        set_targets(db, [t for t in get_targets(db) if t != did])
        telegram.answer(cb["id"], "뺐음")
        text, buttons = targets_message(db)
        return telegram.send(telegram.admin_chat(), text, buttons=buttons)
    if act == "noop":
        return telegram.answer(cb["id"])
    did = int(did or 0)
    d = store.get_draft(db, did)
    msg = cb.get("message", {})
    if not d:
        return telegram.answer(cb["id"], "초안 없음")
    if act == "del":
        if d["status"] != "published":
            return telegram.answer(cb["id"], "발행된 글이 아님")
        try:
            msgs = json.loads(d.get("channel_msgs") or "{}") or {telegram.target_chat(): d["channel_msg_id"]}
            for chat, mid in msgs.items():
                telegram.call("deleteMessage", {"chat_id": chat, "message_id": mid})
            store.update_draft(db, did, status="deleted")
            telegram.answer(cb["id"], "%d개 채널에서 지웠음" % len(msgs))
            telegram.edit_buttons(msg["chat"]["id"], msg["message_id"], [[("🗑 삭제됨", "noop:0")]])
        except Exception as e:
            telegram.answer(cb["id"], "삭제 실패: %s" % str(e)[:150])
        return
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
        "/targets 발행 채널 목록·빼기 (봇을 채널 관리자로 넣으면 추가 버튼이 옴)\n"
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
        titles = target_titles(db)
        telegram.send(chat, "모드: %s%s\n오늘 발행 %d개 / 대기 %d개\n오늘 수집 %d개\n발행 채널: %s" % (
            store.kv_get(db, "mode", cfg["mode"]), " (일시정지)" if store.kv_get(db, "paused") == "1" else "",
            len(store.published_since(db, start)), pend, items,
            ", ".join(titles.get(t, t) for t in get_targets(db)) or "미설정"))
    elif cmd == "/targets":
        text, buttons = targets_message(db)
        telegram.send(chat, text, buttons=buttons)
    elif cmd == "/pause":
        store.kv_set(db, "paused", "1"); telegram.send(chat, "⏸ 멈췄음")
    elif cmd == "/resume":
        store.kv_set(db, "paused", "0"); telegram.send(chat, "▶️ 재개")
    elif cmd == "/auto":
        store.kv_set(db, "mode", "auto"); telegram.send(chat, "🤖 자동발행 모드 (하루 최대 %d개, %d~%d분 무작위 간격)" % (
            cfg["schedule"]["daily_post_cap"], *cfg["schedule"]["gap_minutes"]))
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
