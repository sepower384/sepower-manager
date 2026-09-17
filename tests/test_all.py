# -*- coding: utf-8 -*-
"""python -m unittest discover tests  (네트워크·LLM 없이 돈다)"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from manager import capture, filters, ledger, news, pipeline, store, telegram, tme, writer  # noqa: E402
import run  # noqa: E402

CFG = pipeline.load_config()
NOW = datetime.now(timezone.utc)


def item(text, src="coinnesskr", pid=1, photos=(), hours_ago=1, kind="news"):
    return {"source": src, "post_id": pid, "url": "https://t.me/%s/%d" % (src, pid),
            "date": (NOW - timedelta(hours=hours_ago)).isoformat(), "text": text,
            "links": [], "photos": list(photos), "views": 500, "publisher": "코인니스",
            "preview_title": "", "kind": kind, "has_video": False}


class FakeTG:
    def __init__(self):
        self.calls = []
        self.mid = 100

    def __call__(self, url, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, dict(data or {})))
        self.mid += 1
        body = {"ok": True, "result": {"message_id": self.mid}}
        return type("R", (), {"json": lambda s: body})()

    def methods(self):
        return [c[0] for c in self.calls]


class FilterTest(unittest.TestCase):
    def test_headline_link_passes(self):
        it = item("美 국채금리 '마의 5%' 뚫었다…금융위기 이전 수준으로\nhttps://naver.me/x")
        self.assertIsNone(filters.block_reason(it, CFG))

    def test_macro_event_not_ad(self):
        it = item("이제 남은 빅 이벤트는 FOMC\n(17일 03시)\n페드워치 25bp 인상 확실시. Susan Collins 반대")
        self.assertIsNone(filters.block_reason(it, CFG))

    def test_ads_blocked(self):
        self.assertIn("광고", filters.block_reason(item("트레이딩 대회 START! 지금부터 총상금 30,000 USDG"), CFG))
        self.assertIn("광고", filters.block_reason(item("AMA 리워드 레전드 빈집 발생 180분에게 지급 예정입니다"), CFG))

    def test_channel_promo_blocked(self):
        self.assertEqual(filters.block_reason(item("좋은 채널 소개합니다 @goodchannel1 @another_ch 구독"), CFG),
                         "채널홍보/품앗이")

    def test_similar(self):
        a = "[CFTC 위원장 “클래리티 법안 좌초에도 암호화폐 시장 규칙 추진”] 마이크 셀리그"
        b = "CFTC 위원장 클래리티 법안 좌초에도 암호화폐 시장 규칙 추진 https://x.com/1"
        self.assertTrue(filters.similar(a, b))
        self.assertFalse(filters.similar(a, "엔비디아 실적 발표 앞두고 반도체 강세"))

    def test_score_prefers_fresh_topical(self):
        fresh = filters.score(item("비트코인 ETF 유입, FOMC 앞두고 기관 매집"), CFG)
        old = filters.score(item("비트코인 ETF 유입, FOMC 앞두고 기관 매집", hours_ago=20), CFG)
        off = filters.score(item("오늘 점심 맛집 다녀왔음 아주 좋았음"), CFG)
        self.assertGreater(fresh, old)
        self.assertGreater(fresh, off)

    def test_whale(self):
        w = filters.parse_whale("🟢 알 수 없는 지갑에서 #binance 으로 🟢\n대략 1,543 #BTC :: (1,562억) 입금 확인")
        self.assertEqual((w["direction"], w["eok"], w["dst"]), ("in", 1562.0, "binance"))
        w = filters.parse_whale("🔴 #coinbase 에서 #coinbase 으로 🔴\n대략 9,000 #BTC :: (1조) 이동")
        self.assertEqual(w["direction"], "internal")
        self.assertEqual(pipeline.krw_eok(12500), "1조 2,500억")

    def test_liq(self):
        q = filters.parse_liq("🔴 #BTCUSDT Liquidated SHORT at price $76,010. Total: $2,300,000.00 on Binance")
        self.assertEqual((q["symbol"], q["side"], q["usd"]), ("BTC", "SHORT", 2300000.0))


class TmeTest(unittest.TestCase):
    HTML = """<div class="tgme_widget_message js-widget_message" data-post="ch/42">
      <a class="tgme_widget_message_reply"><div class="tgme_widget_message_text js-message_reply_text">인용문</div></a>
      <a class="tgme_widget_message_photo_wrap" style="width:1px;background-image:url('https://cdn/p.jpg')"></a>
      <div class="tgme_widget_message_text js-message_text">본문<br/>둘째줄 <a href="https://naver.me/a">링크</a></div>
      <span class="tgme_widget_message_views">1.2K</span><time datetime="2026-09-16T11:39:05+00:00"></time></div>"""

    def test_parse(self):
        [p] = tme.parse_page(self.HTML, "ch")
        self.assertEqual(p["post_id"], 42)
        self.assertEqual(p["text"], "본문\n둘째줄 링크")
        self.assertEqual(p["photos"], ["https://cdn/p.jpg"])
        self.assertEqual(p["links"], ["https://naver.me/a"])
        self.assertEqual(p["views"], 1200)


class NewsTest(unittest.TestCase):
    RSS = b"""<?xml version="1.0"?><rss xmlns:media="http://search.yahoo.com/mrss/"><channel>
      <item><title>Bitcoin ETFs see inflows - CoinDesk</title><link>https://ex.com/a</link>
        <pubDate>Wed, 16 Sep 2026 12:00:00 GMT</pubDate><description>&lt;p&gt;Funds took in $500M&lt;/p&gt;</description>
        <media:content url="https://ex.com/a.jpg" medium="image"/></item>
    </channel></rss>"""

    def test_parse_and_item(self):
        [n] = news.parse_rss(self.RSS, "CoinDesk")
        self.assertEqual(n["image"], "https://ex.com/a.jpg")
        it = news.to_item(n)
        self.assertEqual((it["source"], it["kind"], it["photos"]), ("news", "news", ["https://ex.com/a.jpg"]))
        self.assertIn("Funds took in $500M", it["text"])
        self.assertEqual(it["post_id"], news.to_item(dict(n, url="https://other")) ["post_id"])  # 같은 제목=같은 글

    def test_config_has_no_influencer_sources(self):
        ids = {s["id"] for s in CFG["sources"]}
        self.assertFalse(ids & set(CFG["style_refs"]))


class WriterTest(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(writer.extract_json('설명\n```json\n[{"refs":[0],"text":"a"}]\n```'), [{"refs": [0], "text": "a"}])
        self.assertEqual(writer.extract_json('[{"refs":[1],"text":"b"}]'), [{"refs": [1], "text": "b"}])

    def test_prompt_uses_style_guide_not_channel_posts(self):
        p = writer.build_pick_prompt([item("비트코인 ETF 순유입")], [], 2)
        self.assertIn("음슴체", p)
        self.assertIn("형식 예시일 뿐", p)
        self.assertIn("출처=코인니스", p)
        self.assertNotIn("talkevergreen", p)

    def test_two_stage_pick_then_write_with_body(self):
        prompts = []

        def fake(p, timeout=0):
            prompts.append(p)
            if "1단계" in p:
                return '[{"refs":[0,99],"type":"h","angle":"금리 인하 이유가 핵심","why":"이유"},{"refs":[]}]'
            return "```\n다들 호재라는데\n\n본문임\n\n#경제\n```"
        orig = writer.run_claude
        writer.run_claude = fake
        try:
            out = writer.pick_and_write([item("후보")], [], 2,
                                        enrich=lambda refs: ({0: "기사 본문 전체 내용"}, "BTC $76,000 (24h -1.0%)"))
        finally:
            writer.run_claude = orig
        self.assertEqual(out, [{"refs": [0], "text": "다들 호재라는데\n\n본문임\n\n#경제",
                                "type": "H", "why": "[H] 이유"}])
        self.assertEqual(len(prompts), 2)                       # 고르기 1 + 쓰기 1
        self.assertIn("기사 본문 전체 내용", prompts[1])
        self.assertIn("BTC $76,000", prompts[1])
        self.assertIn("금리 인하 이유가 핵심", prompts[1])
        self.assertIn("300~600자", prompts[1])                  # H 는 깊은 유형

    def test_recent_types_are_banned(self):
        ok = writer.allowed_types(["C", "E", "C", "H", "A"])
        self.assertFalse({"C", "E", "H"} & set(ok))
        self.assertIn("A", ok)                                  # 5번째 이전은 다시 허용
        self.assertNotIn("F", ok)                               # 일정형은 아침 브리핑 전용
        p = writer.build_pick_prompt([item("x")], [], 2, ["C", "E"])
        self.assertNotIn("C(쟁점", p)
        self.assertIn("짧은 유형(A/B/K)", p)                     # 최근 짧은 글 없음 → 하나는 짧게

    def test_recent_types_from_db(self):
        db = store.connect(":memory:")
        for t in ("C", "H"):
            d = store.add_draft(db, "insight", "x", "", [])
            store.update_draft(db, d, ptype=t, status="published")
        self.assertEqual(store.recent_types(db), ["H", "C"])


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.db = store.connect(":memory:")
        self.tg = FakeTG()
        telegram._post = self.tg
        os.environ.update(TELEGRAM_BOT_TOKEN_MANAGER="t", TELEGRAM_ADMIN_CHAT_ID="1",
                          TELEGRAM_TARGET_CHAT_ID="@sepower")
        os.environ.pop("MANAGER_TARGETS", None)
        store.kv_set(self.db, "mode", "approve")
        self.did = store.add_draft(self.db, "insight", "⚖️ 헤드라인\n본문임\n→ 관점\n출처: 코인니스", "",
                                   [{"source": "coinnesskr", "post_id": 1, "url": "u"}])
        # 브라우저 없이: 카드 = 임시 파일, 원문 캡처 = 실패(→ 카드로 떨어짐)
        self.tmp = tempfile.mkdtemp()
        os.environ.pop("LEDGER_GIT", None)
        ledger.LOCAL = os.path.join(self.tmp, "ledger.json")      # 테스트마다 빈 장부
        self.cards = []

        def fake_card(title, publisher, sub, name, accent="#000"):
            p = os.path.join(self.tmp, name)
            with open(p, "wb") as f:
                f.write(("card:" + title + name).encode("utf-8"))
            self.cards.append(title)
            return p
        self._orig = (capture.card, capture.screenshot_post, capture.screenshot_article)
        capture.card = fake_card
        capture.screenshot_post = capture.screenshot_article = lambda *a: (_ for _ in ()).throw(ValueError("no browser"))

    def tearDown(self):
        capture.card, capture.screenshot_post, capture.screenshot_article = self._orig

    def cb(self, data, mid):
        return {"id": "c", "data": data, "from": {"id": 1},
                "message": {"message_id": mid, "chat": {"id": 1}}}

    def test_approve_publishes_once(self):
        pipeline.deliver(self.db, CFG, [self.did])
        d = store.get_draft(self.db, self.did)
        self.assertEqual(self.tg.calls[0][1]["chat_id"], "1")
        self.assertIn("pub:%d" % self.did, self.tg.calls[0][1]["reply_markup"])
        pipeline.on_callback(self.db, CFG, self.cb("pub:%d" % self.did, d["admin_msg_id"]))
        d = store.get_draft(self.db, self.did)
        self.assertEqual(d["status"], "published")
        sent = [c for c in self.tg.calls if c[1].get("chat_id") == "@sepower"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "sendPhoto")
        self.assertNotIn("초안", sent[0][1]["caption"])    # 관리용 머리글은 채널에 안 나감
        pipeline.on_callback(self.db, CFG, self.cb("pub:%d" % self.did, d["admin_msg_id"]))  # 두 번 눌러도
        self.assertEqual(len([c for c in self.tg.calls if c[1].get("chat_id") == "@sepower"]), 1)

    def test_reject(self):
        pipeline.on_callback(self.db, CFG, self.cb("rej:%d" % self.did, 5))
        self.assertEqual(store.get_draft(self.db, self.did)["status"], "rejected")
        self.assertFalse([c for c in self.tg.calls if c[1].get("chat_id") == "@sepower"])

    def test_reply_edit_replaces_draft(self):
        pipeline.deliver(self.db, CFG, [self.did])
        mid = store.get_draft(self.db, self.did)["admin_msg_id"]
        pipeline.on_message(self.db, CFG, {"text": "내가 고친 글", "chat": {"id": 1},
                                           "reply_to_message": {"message_id": mid}}, None)
        self.assertEqual(store.get_draft(self.db, self.did)["status"], "rejected")
        new = self.db.execute("SELECT * FROM drafts ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((new["text"], new["status"]), ("내가 고친 글", "pending"))

    def test_no_target_does_not_publish(self):
        os.environ["TELEGRAM_TARGET_CHAT_ID"] = ""
        pipeline.on_callback(self.db, CFG, self.cb("pub:%d" % self.did, 5))
        self.assertEqual(store.get_draft(self.db, self.did)["status"], "pending")

    def test_auto_mode_random_gap_and_night(self):
        store.kv_set(self.db, "mode", "auto")
        d2 = store.add_draft(self.db, "insight", "두번째 글은 전혀 다른 내용임", "", [])
        pipeline.deliver(self.db, CFG, [self.did, d2])
        t = store.now_kst().replace(hour=10, minute=0)
        self.assertTrue(pipeline.flush_queue(self.db, CFG, t))
        gap = pipeline.next_gap_minutes(t.isoformat(), CFG)
        self.assertTrue(60 <= gap <= 120)                                  # 1~2시간 무작위
        self.assertEqual(gap, pipeline.next_gap_minutes(t.isoformat(), CFG))  # 어느 회차가 계산해도 같음
        self.assertFalse(pipeline.flush_queue(self.db, CFG, t + timedelta(minutes=59)))
        st = [store.get_draft(self.db, i)["status"] for i in (self.did, d2)]
        self.assertEqual(st, ["published", "queued"])
        night_off = json.loads(json.dumps(CFG))
        night_off["schedule"]["active_hours"] = [7, 24]      # 시간대 제한을 켜면 새벽엔 안 나감
        self.assertFalse(pipeline.flush_queue(self.db, night_off, t.replace(hour=3) + timedelta(days=1)))
        self.assertTrue(pipeline.flush_queue(self.db, CFG, t + timedelta(minutes=121)))
        # 자동발행 알림에 삭제 버튼 → 누르면 채널에서 지움
        admin_mid = store.get_draft(self.db, self.did)["admin_msg_id"]
        self.assertTrue(admin_mid)
        pipeline.on_callback(self.db, CFG, self.cb("del:%d" % self.did, admin_mid))
        self.assertEqual(store.get_draft(self.db, self.did)["status"], "deleted")
        self.assertIn("deleteMessage", self.tg.methods())

    def test_urgent_goes_after_15_minutes(self):
        store.kv_set(self.db, "mode", "auto")
        store.update_draft(self.db, self.did, status="queued")
        t = store.now_kst().replace(hour=10, minute=0)
        self.assertTrue(pipeline.flush_queue(self.db, CFG, t))
        u = store.add_draft(self.db, "insight", "속보) 전혀 새로운 긴급 뉴스", "", [])
        store.update_draft(self.db, u, status="queued", urgent=1)
        self.assertFalse(pipeline.flush_queue(self.db, CFG, t + timedelta(minutes=10)))
        self.assertTrue(pipeline.flush_queue(self.db, CFG, t + timedelta(minutes=16)))
        self.assertEqual(store.get_draft(self.db, u)["status"], "published")

    def test_is_urgent(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(pipeline.is_urgent(item("[속보] 연준 긴급 금리 인하"), CFG, now))
        self.assertFalse(pipeline.is_urgent(item("[속보] 연예인 결혼 발표"), CFG, now))      # 주제 밖
        self.assertFalse(pipeline.is_urgent(item("[속보] 연준 금리 인하", hours_ago=3), CFG, now))  # 오래됨
        self.assertFalse(pipeline.is_urgent(item("연준 금리 인하 전망"), CFG, now))

    def test_stale_state_cannot_republish(self):
        """상태 DB 가 옛 버전으로 복원돼도(같은 초안이 다시 queued) 장부가 막는다."""
        store.kv_set(self.db, "mode", "auto")
        store.update_draft(self.db, self.did, status="queued")
        t = store.now_kst().replace(hour=10, minute=0)
        self.assertTrue(pipeline.flush_queue(self.db, CFG, t))
        stale = store.connect(":memory:")                  # 옛 상태: 발행 기록 없음
        d = store.add_draft(stale, "insight", "⚖️ 헤드라인\n본문임\n→ 관점\n출처: 코인니스", "",
                            [{"source": "coinnesskr", "post_id": 1, "url": "u"}])
        store.update_draft(stale, d, status="queued")
        store.kv_set(stale, "mode", "auto")
        self.assertFalse(pipeline.flush_queue(stale, CFG, t + timedelta(hours=3)))
        self.assertEqual(store.get_draft(stale, d)["status"], "dup")
        self.assertEqual(len([c for c in self.tg.calls if c[0] == "sendPhoto" and c[1]["chat_id"] == "@sepower"]), 1)

    def test_same_source_different_text_blocked(self):
        store.update_draft(self.db, self.did, status="queued")
        pipeline.publish(self.db, self.did, CFG)
        d2 = store.add_draft(self.db, "insight", "완전히 다르게 쓴 글", "",
                             [{"source": "coinnesskr", "post_id": 1, "url": "u"}])     # 같은 원문
        self.assertFalse(pipeline.publish(self.db, d2, CFG))
        self.assertIn("같은 원문", store.get_draft(self.db, d2)["reason"])

    def test_auto_without_target_falls_back_to_dm(self):
        store.kv_set(self.db, "mode", "auto")
        os.environ["TELEGRAM_TARGET_CHAT_ID"] = ""
        pipeline.deliver(self.db, CFG, [self.did])
        self.assertEqual(store.get_draft(self.db, self.did)["status"], "pending")
        self.assertIn("pub:%d" % self.did, self.tg.calls[0][1]["reply_markup"])

    def test_redo_queued_without_llm_then_processed(self):
        pipeline.deliver(self.db, CFG, [self.did])
        mid = store.get_draft(self.db, self.did)["admin_msg_id"]
        os.environ["MANAGER_NO_LLM"] = "1"
        try:
            pipeline.on_callback(self.db, CFG, self.cb("re:%d" % self.did, mid))
        finally:
            del os.environ["MANAGER_NO_LLM"]
        self.assertTrue(run.needs_llm(self.db))
        self.assertEqual(store.get_draft(self.db, self.did)["status"], "pending")
        orig = writer.run_claude
        writer.run_claude = lambda p, timeout=0: "새로 쓴 글"
        try:
            run.llm_tasks(self.db, CFG)
        finally:
            writer.run_claude = orig
        self.assertFalse(run.needs_llm(self.db))
        self.assertEqual(store.get_draft(self.db, self.did)["status"], "rejected")
        new = self.db.execute("SELECT * FROM drafts ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((new["text"], new["status"]), ("새로 쓴 글", "pending"))

    def test_handle_updates_ignores_strangers(self):
        ups = [{"update_id": 7, "message": {"text": "/pause", "chat": {"id": 999}}},
               {"update_id": 8, "message": {"text": "/pause", "chat": {"id": 1}}}]
        orig = telegram.updates
        try:
            store.kv_set(self.db, "paused", "0")
            telegram.updates = lambda offset, timeout=0: ups[:1]   # 남의 메시지만
            run.handle_updates(self.db, CFG)
            self.assertEqual(store.kv_get(self.db, "paused"), "0")
            telegram.updates = lambda offset, timeout=0: ups
            run.handle_updates(self.db, CFG)
        finally:
            telegram.updates = orig
        self.assertEqual(store.kv_get(self.db, "paused"), "1")
        self.assertEqual(store.kv_get(self.db, "offset"), "9")

    def test_added_to_channel_sets_target_by_button(self):
        up = {"update_id": 1, "my_chat_member": {
            "from": {"id": 1}, "chat": {"id": -1001234, "type": "channel", "title": "세력 채널"},
            "new_chat_member": {"status": "administrator"}}}
        stranger = {"update_id": 2, "my_chat_member": dict(up["my_chat_member"], **{"from": {"id": 5}})}
        orig = telegram.updates
        telegram.updates = lambda offset, timeout=0: [up, stranger]
        try:
            run.handle_updates(self.db, CFG)
        finally:
            telegram.updates = orig
        asks = [c for c in self.tg.calls if "tgt:-1001234" in c[1].get("reply_markup", "")]
        self.assertEqual(len(asks), 1)                      # 남이 추가한 건 무시
        pipeline.on_callback(self.db, CFG, self.cb("tgt:-1001234", 9))
        # 교체가 아니라 추가 — 기존 채널(@sepower)도 유지
        self.assertEqual(telegram.target_chats(), ["@sepower", "-1001234"])
        os.environ.pop("MANAGER_TARGETS")
        pipeline.apply_target(self.db)                      # 다음 회차에도 유지
        self.assertEqual(telegram.target_chats(), ["@sepower", "-1001234"])
        # 발행은 두 채널 모두, 삭제도 두 채널 모두
        pipeline.publish(self.db, self.did, CFG)
        sent = [c[1]["chat_id"] for c in self.tg.calls if c[0] == "sendPhoto"]
        self.assertEqual(sent, ["@sepower", "-1001234"])
        pipeline.on_callback(self.db, CFG, self.cb("del:%d" % self.did, 9))
        dels = [c[1]["chat_id"] for c in self.tg.calls if c[0] == "deleteMessage"]
        self.assertEqual(dels, ["@sepower", "-1001234"])
        # 빼기 버튼
        pipeline.on_callback(self.db, CFG, self.cb("untgt:@sepower", 9))
        self.assertEqual(telegram.target_chats(), ["-1001234"])

    def test_legacy_single_target_is_kept_with_secret(self):
        store.kv_set(self.db, "target_chat", "-100999")     # 예전 버전이 교체해버린 값
        pipeline.apply_target(self.db)
        self.assertEqual(telegram.target_chats(), ["@sepower", "-100999"])

    def test_every_post_has_image(self):
        pipeline.publish(self.db, self.did, CFG)
        sent = [c for c in self.tg.calls if c[1].get("chat_id") == "@sepower"]
        self.assertEqual(sent[0][0], "sendPhoto")               # 글만 나가는 일 없음
        self.assertEqual(self.cards, ["헤드라인"])                # 원문 캡처 실패 → 제목 카드(이모지 제거)
        self.assertTrue(store.get_draft(self.db, self.did)["photo_hash"])

    def test_no_image_means_hold_in_queue(self):
        store.kv_set(self.db, "mode", "auto")
        store.update_draft(self.db, self.did, status="queued")
        capture.card = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("playwright 없음"))
        t = store.now_kst().replace(hour=10)
        self.assertFalse(pipeline.flush_queue(self.db, CFG, t))
        self.assertEqual(store.get_draft(self.db, self.did)["status"], "queued")
        self.assertFalse([c for c in self.tg.calls if c[1].get("chat_id") == "@sepower"])

    def test_same_image_not_reused(self):
        p = os.path.join(self.tmp, "same.jpg")
        open(p, "wb").write(b"x" * 9000)
        h = capture.file_hash(p)
        orig = capture.download
        capture.download = lambda url, name: p
        try:
            it = item("같은 사진 기사", photos=["http://img"], kind="news")
            _, h1 = capture.image_for(it, 1, set())
            path2, h2 = capture.image_for(it, 2, {h})
        finally:
            capture.download = orig
        self.assertEqual(h1, h)
        self.assertNotEqual(h2, h)                              # 이미 쓴 사진이면 카드로
        self.assertIn("card", path2)

    def test_dedupe_picks(self):
        cands = [item("[CFTC 위원장 클래리티 법안 좌초에도 시장 규칙 추진] 셀리그", pid=1),
                 item("CFTC 위원장, 클래리티 법안 좌초에도 암호화폐 시장 규칙 추진", pid=2),
                 item("엔비디아 데이터센터 매출 사상 최대", pid=3),
                 item("스트래티지 비트코인 추가 매수", pid=4)]
        picks = [{"refs": [0], "text": "규제 공은 CFTC로\n셀리그 위원장이 규칙 추진"},
                 {"refs": [1], "text": "CFTC가 움직인다\n다른 표현"},          # 같은 사건(원문 제목 유사)
                 {"refs": [0, 2], "text": "엔비디아 얘기\n..."},               # 같은 후보 재사용
                 {"refs": [2], "text": "AI capex 사이클\n엔비디아 매출"},
                 {"refs": [3], "text": "스트래티지 또 매수\n세일러"}]        # 최근 48시간에 다룸
        kept = pipeline.dedupe_picks(picks, cands, ["스트래티지 또 매수 세일러 이번주"])
        self.assertEqual([k["refs"] for k in kept], [[0], [2]])

    def test_recent_topics_include_source_titles(self):
        store.upsert_items(self.db, [item("원문 기사 제목입니다 충분히 길게", pid=1)])
        store.update_draft(self.db, self.did, status="published")
        topics = store.recent_topics(self.db)
        self.assertIn("원문 기사 제목입니다 충분히 길게", topics)
        self.assertTrue(any("헤드라인" in t for t in topics))

    def test_crypto_detection_and_share(self):
        self.assertTrue(filters.is_crypto("솔라나 ETF 자금 유입", CFG))
        self.assertTrue(filters.is_crypto("Tether mints $1B USDT", CFG))
        self.assertFalse(filters.is_crypto("엔비디아 데이터센터 매출 사상 최대", CFG))
        self.assertFalse(filters.is_crypto("Tokyo stocks rally", CFG))           # token 부분일치 아님
        self.assertTrue(pipeline.need_crypto(self.db, CFG))                     # 글이 없으면 크립토부터
        for t in ("엔비디아 실적 해설", "국채금리 5% 돌파", "FOMC 금리 인상", "애플 서버 개발"):
            d = store.add_draft(self.db, "insight", t, "", [])
            store.update_draft(self.db, d, status="published")
        self.assertTrue(pipeline.need_crypto(self.db, CFG))
        for t in ("비트코인 고래 매집", "이더리움 ETF 순유입", "업비트 신규 상장"):
            d = store.add_draft(self.db, "insight", t, "", [])
            store.update_draft(self.db, d, status="published")
        self.assertFalse(pipeline.need_crypto(self.db, CFG))                    # 최근 4개 중 3개 크립토
        for i in range(4):                                                      # 고래 알림은 안 셈
            d = store.add_draft(self.db, "whale", "🐋 고래 이동 BTC %d" % i, "", [])
            store.update_draft(self.db, d, status="published")
        self.assertFalse(pipeline.need_crypto(self.db, CFG))
        for t in ("유가 급등", "나스닥 급락", "고용지표 쇼크"):
            d = store.add_draft(self.db, "insight", t, "", [])
            store.update_draft(self.db, d, status="published")
        self.assertTrue(pipeline.need_crypto(self.db, CFG))                     # 고래 알림이 많아도 부족 판정

    def test_crypto_candidates_boosted_and_tagged(self):
        store.upsert_items(self.db, [item("엔비디아 AI 반도체 데이터센터 전력 급증", pid=31),
                                     item("비트코인 현물 ETF 대규모 순유입, 기관 매집", pid=32)])
        c = pipeline.candidates(self.db, CFG)
        self.assertTrue(c[0]["crypto"])
        self.assertIn("[크립토]", writer.fmt_candidates(c))

    def test_pause_command(self):
        pipeline.on_message(self.db, CFG, {"text": "/pause", "chat": {"id": 1}}, None)
        self.assertEqual(store.kv_get(self.db, "paused"), "1")

    def test_candidates_skip_recent_duplicates(self):
        store.upsert_items(self.db, [
            item("[CFTC 위원장 클래리티 법안 좌초에도 암호화폐 시장 규칙 추진] 셀리그 위원장", pid=11),
            item("CFTC 위원장 클래리티 법안 좌초에도 암호화폐 시장 규칙 추진 — 셀리그", src="talkevergreen", pid=12, kind="insight"),
            item("엔비디아 실적 앞두고 반도체 AI 사이클 점검, 데이터센터 전력 수요 급증", pid=13),
        ])
        c = pipeline.candidates(self.db, CFG)
        self.assertEqual(len(c), 2)                              # 같은 사건 두 기사 → 하나만
        # 발행된 글의 원문과 같은 사건이면 후보에서 빠짐
        store.upsert_items(self.db, [item("비트코인 현물 ETF 5억달러 순유출, 6월 이후 최대", pid=21)])
        d = store.add_draft(self.db, "insight", "ETF 자금 이탈 이어짐", "",
                            [{"source": "coinnesskr", "post_id": 21, "url": "u"}])
        store.update_draft(self.db, d, status="published")
        store.upsert_items(self.db, [item("비트코인 현물 ETF 5억달러 순유출… 6월 이후 최대 규모", src="news", pid=22)])
        heads = [x["text"] for x in pipeline.candidates(self.db, CFG)]
        self.assertFalse(any("순유출" in h for h in heads))


if __name__ == "__main__":
    unittest.main()
