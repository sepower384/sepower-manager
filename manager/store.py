# -*- coding: utf-8 -*-
"""SQLite 저장소 — 수집글(items), 초안(drafts), 키-값(kv)."""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "manager.db")
KST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  source TEXT, post_id INTEGER, url TEXT, date TEXT, text TEXT,
  links TEXT, photos TEXT, views INTEGER, publisher TEXT,
  preview_title TEXT, kind TEXT, fp TEXT, fetched_at TEXT,
  status TEXT DEFAULT 'new', note TEXT DEFAULT '',
  PRIMARY KEY(source, post_id));
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE INDEX IF NOT EXISTS items_fp ON items(fp);
CREATE TABLE IF NOT EXISTS drafts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created TEXT, kind TEXT, text TEXT, photo TEXT, refs TEXT,
  status TEXT DEFAULT 'pending', admin_msg_id INTEGER, channel_msg_id INTEGER,
  published_at TEXT, reason TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def now_kst():
    return datetime.now(KST)


def connect(path=None):
    path = path or DB_PATH
    if path != ":memory:":
        os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(drafts)")}
    if "ptype" not in cols:   # 글 유형(A~K) — 다양하게 섞기용
        db.execute("ALTER TABLE drafts ADD COLUMN ptype TEXT DEFAULT ''")
    return db


def recent_types(db, limit=6):
    return [r[0] for r in db.execute(
        "SELECT ptype FROM drafts WHERE kind='insight' AND status IN ('published','queued','pending') "
        "ORDER BY id DESC LIMIT ?", (limit,))]


def upsert_items(db, items):
    """새 글만 넣는다. 넣은 개수 반환."""
    n = 0
    for it in items:
        cur = db.execute(
            "INSERT OR IGNORE INTO items(source,post_id,url,date,text,links,photos,views,"
            "publisher,preview_title,kind,fp,fetched_at,status,note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (it["source"], it["post_id"], it["url"], it["date"], it["text"],
             json.dumps(it["links"], ensure_ascii=False), json.dumps(it["photos"]),
             it.get("views", 0), it.get("publisher", ""), it.get("preview_title", ""),
             it.get("kind", ""), it.get("fp", ""), now_kst().isoformat(),
             it.get("status", "new"), it.get("note", "")))
        n += cur.rowcount
        if not cur.rowcount and it.get("views"):
            db.execute("UPDATE items SET views=? WHERE source=? AND post_id=?",
                       (it["views"], it["source"], it["post_id"]))
    db.commit()
    return n


def row_item(r):
    d = dict(r)
    d["links"] = json.loads(d["links"] or "[]")
    d["photos"] = json.loads(d["photos"] or "[]")
    return d


def items_where(db, sql, args=()):
    return [row_item(r) for r in db.execute("SELECT * FROM items WHERE " + sql, args)]


def set_item_status(db, keys, status):
    for src, pid in keys:
        db.execute("UPDATE items SET status=? WHERE source=? AND post_id=?", (status, src, pid))
    db.commit()


def fp_seen(db, fp):
    return db.execute("SELECT 1 FROM items WHERE fp=? LIMIT 1", (fp,)).fetchone() is not None


def add_draft(db, kind, text, photo, refs, status="pending"):
    cur = db.execute("INSERT INTO drafts(created,kind,text,photo,refs,status) VALUES(?,?,?,?,?,?)",
                     (now_kst().isoformat(), kind, text, photo or "",
                      json.dumps(refs, ensure_ascii=False), status))
    db.commit()
    return cur.lastrowid


def get_draft(db, did):
    r = db.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["refs"] = json.loads(d["refs"] or "[]")
    return d


def update_draft(db, did, **kw):
    cols = ", ".join("%s=?" % k for k in kw)
    db.execute("UPDATE drafts SET %s WHERE id=?" % cols, (*kw.values(), did))
    db.commit()


def published_since(db, since):
    return [dict(r) for r in db.execute(
        "SELECT * FROM drafts WHERE status='published' AND published_at>=? ORDER BY published_at",
        (since.isoformat(),))]


def recent_texts(db, limit=15):
    return [r["text"] for r in db.execute(
        "SELECT text FROM drafts WHERE status IN ('published','pending') ORDER BY id DESC LIMIT ?",
        (limit,))]


def count_kind_today(db, kind):
    start = now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
    return db.execute("SELECT COUNT(*) FROM drafts WHERE kind=? AND created>=? AND status!='rejected'",
                      (kind, start.isoformat())).fetchone()[0]


def prune(db, item_days=3, draft_days=30):
    """클라우드 캐시로 오가는 DB 를 작게 유지."""
    now = now_kst()
    db.execute("DELETE FROM items WHERE fetched_at<?", ((now - timedelta(days=item_days)).isoformat(),))
    db.execute("DELETE FROM drafts WHERE created<?", ((now - timedelta(days=draft_days)).isoformat(),))
    db.commit()


def kv_get(db, k, default=None):
    r = db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(db, k, v):
    db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, str(v)))
    db.commit()
