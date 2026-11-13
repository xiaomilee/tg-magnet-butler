"""SQLite 持久化：检索会话、投递记录、事件流水。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    rid       TEXT PRIMARY KEY,
    ts        REAL NOT NULL,
    chat_id   INTEGER,
    query     TEXT,
    rule_id   TEXT,
    payload   TEXT
);
CREATE TABLE IF NOT EXISTS downloads (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    infohash  TEXT,
    title     TEXT,
    magnet    TEXT,
    backend   TEXT,
    task_id   TEXT,
    status    TEXT,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_dl_infohash ON downloads(infohash);
CREATE INDEX IF NOT EXISTS idx_dl_ts ON downloads(ts);
CREATE TABLE IF NOT EXISTS events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    REAL NOT NULL,
    kind  TEXT,
    text  TEXT
);

-- 历史字典：见过的文件夹/文件名。扫描只对比、不删行；
-- 文件消失时写 deleted_ts（发现时间），记录永久保留，供关键词匹配用。
CREATE TABLE IF NOT EXISTS library_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    root          TEXT NOT NULL,
    path          TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL,
    name          TEXT NOT NULL,
    name_key      TEXT NOT NULL,
    base_key      TEXT NOT NULL,
    depth         INTEGER DEFAULT 0,
    size          INTEGER DEFAULT 0,
    mtime         REAL DEFAULT 0,
    first_seen    REAL DEFAULT 0,
    last_seen     REAL DEFAULT 0,
    status        TEXT DEFAULT 'present',
    deleted_ts    REAL DEFAULT 0,
    deleted_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_lib_name ON library_items(name_key);
CREATE INDEX IF NOT EXISTS idx_lib_base ON library_items(base_key);
CREATE INDEX IF NOT EXISTS idx_lib_status ON library_items(status);
CREATE INDEX IF NOT EXISTS idx_lib_root ON library_items(root);
CREATE TABLE IF NOT EXISTS library_scans (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    roots    TEXT,
    scanned  INTEGER DEFAULT 0,
    added    INTEGER DEFAULT 0,
    removed  INTEGER DEFAULT 0,
    restored INTEGER DEFAULT 0,
    present  INTEGER DEFAULT 0,
    deleted  INTEGER DEFAULT 0,
    duration REAL DEFAULT 0,
    detail   TEXT
);
"""

_LIB_FIELDS = ("root", "path", "kind", "name", "name_key", "base_key", "depth",
               "size", "mtime", "first_seen", "last_seen", "status",
               "deleted_ts", "deleted_count")


class Store(object):
    def __init__(self, path: str):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()

    # ---------- 检索会话 ----------
    def save_search(self, rid, chat_id, query, rule_id, payload):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO searches(rid,ts,chat_id,query,rule_id,payload)"
                " VALUES(?,?,?,?,?,?)",
                (rid, time.time(), chat_id, query, rule_id,
                 json.dumps(payload, ensure_ascii=False)),
            )
            self._conn.commit()

    def get_search(self, rid):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM searches WHERE rid=?", (rid,)).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["payload"] = json.loads(out.get("payload") or "{}")
        except ValueError:
            out["payload"] = {}
        return out

    # ---------- 投递记录 ----------
    def add_download(self, infohash, title, magnet, backend, task_id, status, detail=""):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO downloads(ts,infohash,title,magnet,backend,task_id,status,detail)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), infohash or "", title or "", magnet or "", backend or "",
                 str(task_id or ""), status, detail or ""),
            )
            self._conn.commit()
            return cur.lastrowid

    def seen_recently(self, infohash, days):
        if not infohash or days <= 0:
            return False
        cutoff = time.time() - days * 86400
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM downloads WHERE infohash=? AND ts>? AND status IN"
                " ('ok','queued','skipped_dup') LIMIT 1",
                (infohash, cutoff)).fetchone()
        return bool(row)

    def recent_downloads(self, limit=30):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM downloads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 事件 ----------
    def add_event(self, kind, text):
        with self._lock:
            self._conn.execute("INSERT INTO events(ts,kind,text) VALUES(?,?,?)",
                               (time.time(), kind, str(text)[:2000]))
            self._conn.commit()

    def recent_events(self, limit=50):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self):
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) c FROM downloads").fetchone()["c"]
            ok = self._conn.execute(
                "SELECT COUNT(*) c FROM downloads WHERE status IN ('ok','queued')"
            ).fetchone()["c"]
            today0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
            today = self._conn.execute(
                "SELECT COUNT(*) c FROM downloads WHERE ts>=? AND status IN ('ok','queued')",
                (today0,)).fetchone()["c"]
            last = self._conn.execute(
                "SELECT ts FROM downloads ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "total": total,
            "success": ok,
            "today": today,
            "last_ts": last["ts"] if last else 0,
        }

    # ---------- 历史字典 ----------
    def library_save(self, item):
        self.library_save_many([item])

    def library_save_many(self, items):
        rows = []
        for it in items:
            row = []
            for k in _LIB_FIELDS:
                if k in ("depth", "size", "deleted_count"):
                    row.append(int(it.get(k) or 0))
                elif k in ("first_seen", "last_seen", "deleted_ts", "mtime"):
                    row.append(float(it.get(k) or 0))
                elif k == "status":
                    row.append(it.get(k) or "present")
                else:
                    row.append(str(it.get(k) or ""))
            rows.append(tuple(row))
        # INSERT OR REPLACE 比 UPSERT 兼容性更好（老 NAS 上的 sqlite 可能偏旧）
        sql = "INSERT OR REPLACE INTO library_items(%s) VALUES(%s)" % (
            ",".join(_LIB_FIELDS), ",".join("?" * len(_LIB_FIELDS)))
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def library_map(self, roots=None):
        """path -> 记录。只取当前监控目录下的，避免删掉配置里的目录后误标删除。"""
        sql = "SELECT * FROM library_items"
        args = ()
        if roots:
            sql += " WHERE root IN (%s)" % ",".join("?" * len(roots))
            args = tuple(roots)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return {r["path"]: dict(r) for r in rows}

    def library_mark_deleted(self, path, ts):
        self.library_mark_deleted_many([path], ts)

    def library_mark_deleted_many(self, paths, ts):
        """批量标记：一次事务，避免删掉几百个文件时逐个 commit 拖慢机械盘。"""
        rows = [(ts, p) for p in paths]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE library_items SET status='deleted', deleted_ts=?, "
                "deleted_count=COALESCE(deleted_count,0)+1 WHERE path=?", rows)
            self._conn.commit()

    def library_like(self, fragment, limit=50):
        """按归一化名做子串检索；存在的排在已删除之前。"""
        if not fragment:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM library_items WHERE name_key LIKE ? "
                "ORDER BY (status='present') DESC, length(name_key) DESC, last_seen DESC "
                "LIMIT ?", ("%" + fragment + "%", int(limit))).fetchall()
        return [dict(r) for r in rows]

    def library_present(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM library_items WHERE status='present' "
                "ORDER BY size DESC").fetchall()
        return [dict(r) for r in rows]

    def library_counts(self, roots=None):
        sql = "SELECT status, COUNT(*) c FROM library_items"
        args = ()
        if roots:
            sql += " WHERE root IN (%s)" % ",".join("?" * len(roots))
            args = tuple(roots)
        with self._lock:
            rows = self._conn.execute(sql + " GROUP BY status", args).fetchall()
        out = {"present": 0, "deleted": 0, "total": 0}
        for r in rows:
            n = int(r["c"] or 0)
            out["total"] += n
            if r["status"] == "deleted":
                out["deleted"] += n
            else:
                out["present"] += n
        return out

    def library_recent(self, limit=60, status=""):
        sql = "SELECT * FROM library_items"
        args = []
        if status in ("present", "deleted"):
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY last_seen DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def add_library_scan(self, rep):
        with self._lock:
            self._conn.execute(
                "INSERT INTO library_scans(ts,roots,scanned,added,removed,restored,"
                "present,deleted,duration,detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rep.get("ts") or time.time(), json.dumps(rep.get("roots") or [],
                                                          ensure_ascii=False),
                 int(rep.get("scanned") or 0), int(rep.get("added") or 0),
                 int(rep.get("removed") or 0), int(rep.get("restored") or 0),
                 int(rep.get("present") or 0), int(rep.get("deleted") or 0),
                 float(rep.get("duration") or 0),
                 json.dumps(rep.get("warnings") or [], ensure_ascii=False)))
            self._conn.commit()

    def recent_library_scans(self, limit=10):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM library_scans ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def cleanup(self, days=90):
        cutoff = time.time() - days * 86400
        with self._lock:
            self._conn.execute("DELETE FROM searches WHERE ts<?", (cutoff,))
            self._conn.execute("DELETE FROM events WHERE ts<?", (cutoff,))
            self._conn.commit()

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
