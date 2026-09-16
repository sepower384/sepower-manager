# -*- coding: utf-8 -*-
"""python -m unittest discover tests  (네트워크·LLM 없이 돈다)"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from manager import filters, news, pipeline, store, telegram, tme, writer  # noqa: E402
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

    def test_pick_filters_bad_refs(self):
        orig = writer.run_claude
        writer.run_claude = lambda p, timeout=0: '[{"refs":[0,99],"text":"글","why":"이유"},{"refs":[],"text":"x"}]'
        try:
            out = writer.pick_and_write([item("후보")], [], 2)
        finally:
            writer.run_claude = orig
        self.assertEqual(out, [{"refs": [0], "text": "글", "why": "이유"}])


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.db = store.connect(":memory:")
        self.tg = FakeTG()
        telegram._post = self.tg
        os.environ.update(TELEGRAM_BOT_TOKEN_MANAGER="t", TELEGRAM_ADMIN_CHAT_ID="1",
                          TELEGRAM_TARGET_CHAT_ID="@sepower")
        store.kv_set(self.db, "mode", "approve")
        self.did = store.add_draft(self.db, "insight", "⚖️ 헤드라인\n본문임\n→ 관점\n출처: 코인니스", "",
                                   [{"source": "coinnesskr", "post_id": 1, "url": "u"}])

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
        self.assertNotIn("초안", sent[0][1]["text"])       # 관리용 머리글은 채널에 안 나감
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
        d2 = store.add_draft(self.db, "insight", "두번째", "", [])
        pipeline.deliver(self.db, CFG, [self.did, d2])
        t = store.now_kst().replace(hour=10, minute=0)
        self.assertTrue(pipeline.flush_queue(self.db, CFG, t))
        self.assertFalse(pipeline.flush_queue(self.db, CFG, t + timedelta(minutes=21)))   # 최소 22분
        st = [store.get_draft(self.db, i)["status"] for i in (self.did, d2)]
        self.assertEqual(st, ["published", "queued"])
        gap = datetime.fromisoformat(store.kv_get(self.db, "next_pub_at")) - t
        self.assertTrue(timedelta(minutes=22) <= gap <= timedelta(minutes=40))
        self.assertFalse(pipeline.flush_queue(self.db, CFG, t.replace(hour=3) + timedelta(days=1)))  # 새벽 금지
        self.assertTrue(pipeline.flush_queue(self.db, CFG, t + timedelta(minutes=41)))
        # 자동발행 알림에 삭제 버튼 → 누르면 채널에서 지움
        admin_mid = store.get_draft(self.db, self.did)["admin_msg_id"]
        self.assertTrue(admin_mid)
        pipeline.on_callback(self.db, CFG, self.cb("del:%d" % self.did, admin_mid))
        self.assertEqual(store.get_draft(self.db, self.did)["status"], "deleted")
        self.assertIn("deleteMessage", self.tg.methods())

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

    def test_pause_command(self):
        pipeline.on_message(self.db, CFG, {"text": "/pause", "chat": {"id": 1}}, None)
        self.assertEqual(store.kv_get(self.db, "paused"), "1")

    def test_candidates_skip_recent_duplicates(self):
        store.upsert_items(self.db, [
            item("[CFTC 위원장 클래리티 법안 좌초에도 암호화폐 시장 규칙 추진] 셀리그 위원장", pid=1),
            item("CFTC 위원장 클래리티 법안 좌초에도 암호화폐 시장 규칙 추진 — 셀리그", src="talkevergreen", pid=2, kind="insight"),
            item("엔비디아 실적 앞두고 반도체 AI 사이클 점검, 데이터센터 전력 수요 급증", pid=3),
        ])
        c = pipeline.candidates(self.db, CFG)
        self.assertEqual(len(c), 2)


if __name__ == "__main__":
    unittest.main()
