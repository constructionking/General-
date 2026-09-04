"""SQLite store (WAL mode so report/download can run while a scan is in progress)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS districts (
  label TEXT PRIMARY KEY,          -- exact ng-select label e.g. "Amroha (अमरोहा)"
  name_en TEXT, name_hi TEXT, pos INTEGER
);
CREATE TABLE IF NOT EXISTS tehsils (
  label TEXT, district TEXT, name_en TEXT, name_hi TEXT, pos INTEGER,
  PRIMARY KEY (district, label)
);
CREATE TABLE IF NOT EXISTS villages (
  code TEXT PRIMARY KEY,           -- 6-digit village code from the label
  label TEXT, district TEXT, tehsil TEXT, name_en TEXT, name_hi TEXT,
  status TEXT DEFAULT 'pending',   -- pending | done | error
  attempts INTEGER DEFAULT 0, error TEXT, scanned_at REAL, priority INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_villages_status ON villages(status, priority);
CREATE INDEX IF NOT EXISTS ix_villages_dt ON villages(district, tehsil);
CREATE TABLE IF NOT EXISTS rows (
  id INTEGER PRIMARY KEY,
  village_code TEXT, prefix TEXT, fasli TEXT,
  khata TEXT, khatedar TEXT, father TEXT, unique_code TEXT, area REAL, raw TEXT,
  scraped_at REAL,
  UNIQUE (village_code, fasli, unique_code, khatedar, father)
);
CREATE INDEX IF NOT EXISTS ix_rows_village ON rows(village_code);
CREATE TABLE IF NOT EXISTS hits (
  id INTEGER PRIMARY KEY,
  row_id INTEGER UNIQUE,
  target TEXT, name_score REAL, father_score REAL, score REAL,
  category TEXT, reasoning TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS extracts (
  id INTEGER PRIMARY KEY,
  hit_id INTEGER, fasli TEXT, pdf_path TEXT, png_path TEXT, html_path TEXT, downloaded_at REAL,
  UNIQUE (hit_id, fasli)
);
CREATE TABLE IF NOT EXISTS events (
  ts REAL, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class Village:
    code: str
    label: str
    district: str
    tehsil: str
    name_en: str
    name_hi: str


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- catalog -------------------------------------------------------
    def upsert_districts(self, labels: list[str]):
        with self._lock:
            for i, lb in enumerate(labels):
                en, hi = split_label(lb)
                self.conn.execute(
                    "INSERT INTO districts(label,name_en,name_hi,pos) VALUES(?,?,?,?) "
                    "ON CONFLICT(label) DO UPDATE SET pos=excluded.pos", (lb, en, hi, i))
            self.conn.commit()

    def upsert_tehsils(self, district: str, labels: list[str]):
        with self._lock:
            for i, lb in enumerate(labels):
                en, hi = split_label(lb)
                self.conn.execute(
                    "INSERT INTO tehsils(label,district,name_en,name_hi,pos) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(district,label) DO UPDATE SET pos=excluded.pos", (lb, district, en, hi, i))
            self.conn.commit()

    def upsert_villages(self, district: str, tehsil: str, labels: list[str]):
        with self._lock:
            for lb in labels:
                code = village_code(lb)
                if not code:
                    continue
                en, hi = split_label(lb.rsplit(" - ", 1)[0])
                self.conn.execute(
                    "INSERT INTO villages(code,label,district,tehsil,name_en,name_hi) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET label=excluded.label, district=excluded.district, "
                    "tehsil=excluded.tehsil, name_en=excluded.name_en, name_hi=excluded.name_hi",
                    (code, lb, district, tehsil, en, hi))
            self.conn.commit()

    def districts(self) -> list[str]:
        return [r["label"] for r in self.conn.execute("SELECT label FROM districts ORDER BY pos")]

    def tehsils(self, district: str) -> list[str]:
        return [r["label"] for r in self.conn.execute(
            "SELECT label FROM tehsils WHERE district=? ORDER BY pos", (district,))]

    def village_count(self, district: Optional[str] = None) -> int:
        if district:
            return self.conn.execute("SELECT COUNT(*) FROM villages WHERE district=?", (district,)).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM villages").fetchone()[0]

    def catalog_done(self, district: str) -> bool:
        return self.village_count(district) > 0

    # ---- queue ---------------------------------------------------------
    def set_priorities(self, ordered_codes: list[str]):
        """Lower number = earlier. Only pending villages are re-ordered."""
        with self._lock:
            self.conn.executemany(
                "UPDATE villages SET priority=? WHERE code=? AND status!='done'",
                [(i, c) for i, c in enumerate(ordered_codes)])
            self.conn.commit()

    def boost(self, district: str, tehsil: Optional[str] = None, amount: int = -1_000_000):
        with self._lock:
            if tehsil:
                self.conn.execute("UPDATE villages SET priority=priority+? WHERE district=? AND tehsil=? AND status!='done'",
                                  (amount, district, tehsil))
            else:
                self.conn.execute("UPDATE villages SET priority=priority+? WHERE district=? AND status!='done'",
                                  (amount, district))
            self.conn.commit()

    def next_pending(self, districts: Optional[list[str]], limit: int, max_attempts: int,
                     district: Optional[str] = None, tehsil: Optional[str] = None) -> list[Village]:
        q = "SELECT code,label,district,tehsil,name_en,name_hi FROM villages WHERE status!='done' AND attempts<?"
        args: list = [max_attempts]
        if districts:
            q += " AND district IN (%s)" % ",".join("?" * len(districts))
            args += districts
        if district and tehsil:
            q += " AND district=? AND tehsil=?"
            args += [district, tehsil]
        q += " ORDER BY priority, district, tehsil, code LIMIT ?"
        args.append(limit)
        return [Village(*r) for r in self.conn.execute(q, args)]

    def mark_started(self, code: str):
        with self._lock:
            self.conn.execute("UPDATE villages SET attempts=attempts+1 WHERE code=?", (code,))
            self.conn.commit()

    def mark_done(self, code: str):
        with self._lock:
            self.conn.execute("UPDATE villages SET status='done', error=NULL, scanned_at=? WHERE code=?",
                              (time.time(), code))
            self.conn.commit()

    def mark_error(self, code: str, err: str):
        with self._lock:
            self.conn.execute("UPDATE villages SET status='error', error=? WHERE code=?", (err[:500], code))
            self.conn.commit()

    def reset_errors(self, districts: Optional[list[str]] = None):
        with self._lock:
            if districts:
                self.conn.execute("UPDATE villages SET status='pending', attempts=0 WHERE status='error' AND district IN (%s)"
                                  % ",".join("?" * len(districts)), districts)
            else:
                self.conn.execute("UPDATE villages SET status='pending', attempts=0 WHERE status='error'")
            self.conn.commit()

    # ---- rows / hits ---------------------------------------------------
    def add_rows(self, village_code: str, prefix: str, fasli: str, rows: Iterable[dict]) -> list[int]:
        ids = []
        with self._lock:
            for r in rows:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO rows(village_code,prefix,fasli,khata,khatedar,father,unique_code,area,raw,scraped_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (village_code, prefix, fasli, r["khata"], r["khatedar"], r["father"], r["unique_code"],
                     r.get("area"), r.get("raw"), time.time()))
                if cur.lastrowid and cur.rowcount:
                    ids.append(cur.lastrowid)
                else:
                    row = self.conn.execute(
                        "SELECT id FROM rows WHERE village_code=? AND fasli=? AND unique_code=? AND khatedar=? AND father=?",
                        (village_code, fasli, r["unique_code"], r["khatedar"], r["father"])).fetchone()
                    if row:
                        ids.append(row["id"])
            self.conn.commit()
        return ids

    def add_hit(self, row_id: int, target: str, name_score: float, father_score: float, score: float,
                category: str, reasoning: str):
        with self._lock:
            self.conn.execute(
                "INSERT INTO hits(row_id,target,name_score,father_score,score,category,reasoning,created_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(row_id) DO UPDATE SET category=excluded.category, "
                "reasoning=excluded.reasoning, score=excluded.score",
                (row_id, target, name_score, father_score, score, category, reasoning, time.time()))
            self.conn.commit()

    def all_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT r.id, r.khatedar, r.father, r.village_code, v.district, v.tehsil, v.name_en "
            "FROM rows r JOIN villages v ON v.code=r.village_code ORDER BY r.village_code"))

    def replace_hits(self, hits: list[tuple]):
        """Atomically replace the hits table. hits = [(row_id, target, name_score, father_score, score, category, reasoning)].
        Uses BEGIN IMMEDIATE so the busy timeout applies while other processes are writing."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DELETE FROM hits")
                self.conn.executemany(
                    "INSERT INTO hits(row_id,target,name_score,father_score,score,category,reasoning,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)", [(*h, time.time()) for h in hits])
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def hits(self, category: Optional[str] = None) -> list[sqlite3.Row]:
        q = ("SELECT h.*, r.village_code, r.prefix, r.fasli, r.khata, r.khatedar, r.father, r.unique_code, r.area, "
             "v.label AS village_label, v.district, v.tehsil, v.name_en AS village_en, v.name_hi AS village_hi, "
             "e.pdf_path, e.png_path "
             "FROM hits h JOIN rows r ON r.id=h.row_id JOIN villages v ON v.code=r.village_code "
             "LEFT JOIN extracts e ON e.hit_id=h.id AND e.fasli=r.fasli ")
        args: list = []
        if category:
            q += "WHERE h.category=? "
            args.append(category)
        q += "ORDER BY h.category='probable' DESC, h.score DESC, v.district, v.tehsil"
        return list(self.conn.execute(q, args))

    def family_hits_in_tehsil(self, district: str, tehsil: str, target_ids: tuple[str, ...]) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM hits h JOIN rows r ON r.id=h.row_id JOIN villages v ON v.code=r.village_code "
            "WHERE v.district=? AND v.tehsil=? AND h.target IN (%s)" % ",".join("?" * len(target_ids)),
            (district, tehsil, *target_ids)).fetchone()[0]

    def pair_hits_in_village(self, village_code: str, target: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM hits h JOIN rows r ON r.id=h.row_id WHERE r.village_code=? AND h.target=?",
            (village_code, target)).fetchone()[0]

    def add_extract(self, hit_id: int, fasli: str, pdf: str, png: str, html: str):
        with self._lock:
            self.conn.execute(
                "INSERT INTO extracts(hit_id,fasli,pdf_path,png_path,html_path,downloaded_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(hit_id,fasli) DO UPDATE SET pdf_path=excluded.pdf_path, png_path=excluded.png_path, "
                "html_path=excluded.html_path, downloaded_at=excluded.downloaded_at",
                (hit_id, fasli, pdf, png, html, time.time()))
            self.conn.commit()

    # ---- stats / events ------------------------------------------------
    def event(self, kind: str, detail: str = ""):
        with self._lock:
            self.conn.execute("INSERT INTO events(ts,kind,detail) VALUES(?,?,?)", (time.time(), kind, detail))
            self.conn.commit()

    def coverage(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT district, COUNT(*) AS total, SUM(status='done') AS done, SUM(status='error') AS errors, "
            "SUM(status='pending') AS pending FROM villages GROUP BY district ORDER BY district"))

    def coverage_tehsil(self, district: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT tehsil, COUNT(*) AS total, SUM(status='done') AS done, SUM(status='error') AS errors "
            "FROM villages WHERE district=? GROUP BY tehsil ORDER BY tehsil", (district,)))

    def totals(self) -> dict:
        r = self.conn.execute(
            "SELECT COUNT(*) AS total, SUM(status='done') AS done, SUM(status='error') AS errors FROM villages").fetchone()
        rows = self.conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
        hp = self.conn.execute("SELECT COUNT(*) FROM hits WHERE category='probable'").fetchone()[0]
        hl = self.conn.execute("SELECT COUNT(*) FROM hits WHERE category='less_probable'").fetchone()[0]
        ex = self.conn.execute("SELECT COUNT(*) FROM extracts").fetchone()[0]
        return {"villages": r["total"] or 0, "done": r["done"] or 0, "errors": r["errors"] or 0,
                "rows": rows, "probable": hp, "less_probable": hl, "extracts": ex}

    def timing_summary(self, window_s: float = 1800.0) -> dict:
        """Median seconds per step from 'timing' events (detail = 'code k=v k=v …'), plus timeout URLs."""
        import statistics
        now = time.time()
        per: dict = {}
        n = 0
        for (detail,) in self.conn.execute("SELECT detail FROM events WHERE kind='timing' AND ts>?", (now - window_s,)):
            n += 1
            for tok in detail.split()[1:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    try:
                        per.setdefault(k, []).append(float(v))
                    except ValueError:
                        pass
        out = {k: (round(statistics.median(v), 2), len(v)) for k, v in per.items()}
        timeouts: dict = {}
        for (detail,) in self.conn.execute("SELECT detail FROM events WHERE kind='error' AND ts>?", (now - window_s,)):
            key = "timeout " + detail.split("timeout waiting for", 1)[1].split()[0] if "timeout waiting for" in detail \
                else detail.split(" ", 1)[1][:50] if " " in detail else detail[:50]
            timeouts[key] = timeouts.get(key, 0) + 1
        return {"villages": n, "steps": out, "errors": dict(sorted(timeouts.items(), key=lambda kv: -kv[1])[:6])}

    def recent_rate(self, window_s: float = 120.0) -> tuple[float, float]:
        """(villages/s, errors/s) over the last window."""
        now = time.time()
        done = self.conn.execute("SELECT COUNT(*) FROM villages WHERE scanned_at>?", (now - window_s,)).fetchone()[0]
        errs = self.conn.execute("SELECT COUNT(*) FROM events WHERE kind='error' AND ts>?", (now - window_s,)).fetchone()[0]
        return done / window_s, errs / window_s

    def set_meta(self, key: str, value):
        with self._lock:
            self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, json.dumps(value, ensure_ascii=False)))
            self.conn.commit()

    def get_meta(self, key: str, default=None):
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default


def split_label(label: str) -> tuple[str, str]:
    """'Amroha (अमरोहा)' -> ('Amroha', 'अमरोहा')"""
    if "(" in label and label.rstrip().endswith(")"):
        en, hi = label.rsplit("(", 1)
        return en.strip(), hi.rstrip(")").strip()
    return label.strip(), ""


def village_code(label: str) -> Optional[str]:
    """'Akbarpur Sakinya (अकबरपुर सकैनिया) - 117944' -> '117944'"""
    tail = label.rsplit("-", 1)[-1].strip()
    return tail if tail.isdigit() else None
