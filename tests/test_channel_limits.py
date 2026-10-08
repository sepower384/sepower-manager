# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, timezone

from manager import pipeline

KST = timezone(timedelta(hours=9))
CH = "-1001852979246"
CFG = {"channel_limits": {CH: {"gap_minutes": [210, 270], "daily_cap": 6, "urgent_gap_minutes": 60,
                               "kinds": ["insight", "brief"]}}}


def led(*hours_ago, now):
    return {"posts": [{"at": (now - timedelta(hours=h)).isoformat(), "chats": [CH]} for h in hours_ago]}


def test_other_channel_unlimited():
    now = datetime(2026, 10, 8, 15, tzinfo=KST)
    assert pipeline.channel_hold("-1004462359818", {"kind": "whale"}, CFG, led(0.1, now=now), now) is None


def test_gap_and_kinds():
    now = datetime(2026, 10, 8, 15, tzinfo=KST)
    assert pipeline.channel_hold(CH, {"kind": "whale"}, CFG, {"posts": []}, now).startswith("종류")
    assert pipeline.channel_hold(CH, {"kind": "insight"}, CFG, led(2, now=now), now).startswith("간격")
    assert pipeline.channel_hold(CH, {"kind": "insight"}, CFG, led(4.6, now=now), now) is None
    assert pipeline.channel_hold(CH, {"kind": "insight", "urgent": 1}, CFG, led(1.5, now=now), now) is None
    assert pipeline.channel_hold(CH, {"kind": "insight"}, CFG, {"posts": []}, now) is None


def test_daily_cap():
    now = datetime(2026, 10, 8, 23, tzinfo=KST)
    assert pipeline.channel_hold(CH, {"kind": "insight"}, CFG, led(5, 6, 7, 8, 9, 10, now=now), now) == "하루 상한"
